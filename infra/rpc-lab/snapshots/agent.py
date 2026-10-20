#!/usr/bin/env python3
"""
swarm-llm Agent  -  Python version
Works on Windows, Linux, macOS wherever Python 3.8+ is available.

Usage (non-interactive):
    python agent.py --coordinator https://1.2.3.4:8443 --api-key abc123 --model devstral-small-2:24b

Usage (wizard):
    python agent.py

After the first successful configuration on Windows, the agent persists its
context below LOCALAPPDATA and installs a hidden current-user watchdog. No
administrator permission or manual relaunch is required.
"""

import argparse
import getpass
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote as urlquote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ---------------------------------------------------------------------------
# bootstrap requests if missing
# ---------------------------------------------------------------------------
try:
    import requests
except ImportError:
    print("[*] Installing requests...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "--quiet"])
    import requests

# ---------------------------------------------------------------------------
# global state  (set once during startup, read by threads)
# ---------------------------------------------------------------------------
_coordinator  = ""
_api_key      = ""
_model        = ""
_agent_id     = ""
_hw           = {}

# RPC cluster state (modified by main thread and rpc thread)
_rpc_active   = threading.Event()  # set while this machine is running an RPC role
_rpc_assigned = threading.Event()  # set the moment an assignment is read (before role starts)
_ollama_proc = None                # subprocess.Popen for local Ollama server
_ollama_port = 0                   # port Ollama is listening on
_ollama_exe  = ""                  # path to ollama binary

# ---------------------------------------------------------------------------
# crash logging
# ---------------------------------------------------------------------------
def _log_crash(exc: BaseException) -> None:
    import traceback as _tb
    ts  = time.strftime("%Y-%m-%d %H:%M:%S")
    tb  = _tb.format_exc()
    msg = f"\n{'='*60}\n{ts}  pid={os.getpid()}\n{tb}"
    try:
        crash_dir = Path.home() / ".swarm"
        crash_dir.mkdir(exist_ok=True)
        with open(crash_dir / "agent_crash.log", "a", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass
    print(msg, file=sys.stderr, flush=True)
    if _coordinator and _api_key:
        try:
            import requests as _req
            _req.post(
                f"{_coordinator}/agent-status",
                json={"phase": "RUNNING", "detail": f"CRASH: {exc}",
                      "agent_id": _agent_id, "hostname": socket.gethostname(),
                      "model": _model or "?"},
                headers={"X-API-Key": _api_key}, timeout=5,
            )
        except Exception:
            pass

# ---------------------------------------------------------------------------
# console helpers
# ---------------------------------------------------------------------------
CYAN    = "\033[96m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
GRAY    = "\033[90m"
RESET   = "\033[0m"

def _c(color, msg): return f"{color}{msg}{RESET}" if sys.stdout.isatty() else msg

def step(msg):  print(_c(CYAN,   f"[*] {msg}"))
def ok(msg):    print(_c(GREEN,  f"[+] {msg}"))
def err(msg):   print(_c(RED,    f"[!] {msg}"))

def _apply_update(new_code: str) -> None:
    """Write new_code to disk and re-exec this process with the same args.
    Never returns on success; raises on failure."""
    import tempfile as _tf
    current_file = Path(__file__).resolve()
    try:
        current_file.write_text(new_code, encoding="utf-8")
        target = current_file
    except Exception:
        tmp = Path(_tf.mktemp(suffix="_swarm_agent.py"))
        tmp.write_text(new_code, encoding="utf-8")
        target = tmp
    print(_c("\033[33m", f"  [update] restarting as {target}..."))
    if platform.system() != "Windows":
        os.execv(sys.executable, [sys.executable, str(target)] + sys.argv[1:])
    else:
        # Windows: spawn a new visible process then exit this one.
        # No DETACHED_PROCESS — new process inherits the console so output
        # continues in the same window.
        subprocess.Popen([sys.executable, str(target)] + sys.argv[1:])
        os._exit(0)

def _check_for_update(coordinator: str, headers: dict) -> None:
    """Download agent.py from coordinator; call _apply_update if it differs."""
    try:
        base = coordinator.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        # Warm up the NAT path with a small request before fetching the large file.
        try:
            requests.get(f"{coordinator}/healthz", headers=headers, timeout=5)
            time.sleep(1)
        except Exception:
            pass
        r = requests.get(f"{base}/agent/agent.py", headers=headers, timeout=30)
        if r.status_code != 200:
            return
        new_code = r.text
        current_code = Path(__file__).resolve().read_text(encoding="utf-8", errors="replace")
        if new_code.strip() != current_code.strip():
            print(_c("\033[33m", "  [update] new agent.py detected — applying update"))
            _apply_update(new_code)
    except Exception as _ue:
        pass  # network blip — try again next cycle

def write_status(phase, detail=""):
    ts = time.strftime("%H:%M:%S")
    msg = f"[{ts}] {phase}  -  {detail}" if detail else f"[{ts}] {phase}"
    print(_c("\033[36m", f"  {msg}"))
    try:
        Path(tempfile.gettempdir(), "swarm-agent-status.txt").write_text(msg, encoding="utf-8")
    except Exception:
        pass
    if _coordinator and _api_key:
        try:
            requests.post(
                f"{_coordinator}/agent-status",
                json={"phase": phase, "detail": detail,
                      "agent_id": _agent_id, "hostname": socket.gethostname(),
                      "model": _model},
                headers={"X-API-Key": _api_key},
                timeout=5,
            )
        except Exception:
            pass

def show_banner():
    print()
    print(_c(CYAN, "  +-------------------------------------------------------+"))
    print(_c(CYAN, "  |         swarm-llm Agent  -  Python Edition            |"))
    print(_c(CYAN, "  |         TLS ON  |  API Key Auth ON                    |"))
    print(_c(CYAN, "  +-------------------------------------------------------+"))
    print()

# ---------------------------------------------------------------------------
# hardware detection
# ---------------------------------------------------------------------------
def detect_hardware():
    hw = {"vram_gb": 0.0, "ram_gb": 0, "gpu_name": "none",
          "cpu_name": "unknown", "cpu_cores": None, "network_mbps": None,
          "os_version": platform.platform()}
    # GPU via nvidia-smi
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace"
        ).strip()
        parts = out.split(",")
        hw["gpu_name"] = parts[0].strip()
        hw["vram_gb"]  = round(float(parts[1].strip()) / 1024, 1)
    except Exception:
        pass
    # RAM + CPU
    try:
        import psutil
        hw["ram_gb"]   = round(psutil.virtual_memory().total / 1024**3)
        hw["cpu_cores"] = psutil.cpu_count(logical=True)
    except ImportError:
        pass
    try:
        hw["cpu_name"] = platform.processor() or "unknown"
    except Exception:
        pass
    return hw

def measure_network_speed():
    url = "https://speed.cloudflare.com/__down?bytes=5242880"
    try:
        t0 = time.time()
        r  = requests.get(url, stream=True, timeout=30)
        data = r.content
        elapsed = time.time() - t0
        if elapsed > 0 and len(data) > 0:
            return round(len(data) / elapsed / 1024**2, 2)
    except Exception:
        pass
    return None

def get_system_info(hw):
    info = dict(hw)
    info["hostname"]       = socket.gethostname()
    info["ollama_version"] = "?"
    try:
        ver = subprocess.check_output(
            ["ollama", "--version"],
            stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace"
        ).strip()
        info["ollama_version"] = re.sub(r"ollama version (is )?", "", ver).strip()
    except Exception:
        pass
    return info

# ---------------------------------------------------------------------------
# model catalog / VRAM helpers
# ---------------------------------------------------------------------------
_MODEL_VRAM = {
    "devstral-small-2:24b": 15.0,
    "qwen3.6:27b":          17.0,
    "qwen3:30b-a3b":        17.0,
    "qwen3:14b":             9.3,
    "gemma4:12b":            8.0,
    "qwen3:8b":              5.2,
}

_MODEL_CATALOG = [
    {"id": "qwen3:8b",             "vram": 5.2 },
    {"id": "gemma4:12b",           "vram": 8.0 },
    {"id": "qwen3:14b",            "vram": 9.3 },
    {"id": "devstral-small-2:24b", "vram": 15.0},
    {"id": "qwen3.6:27b",          "vram": 17.0},
    {"id": "qwen3:30b-a3b",        "vram": 17.0},
]

def model_vram(model):
    if model in _MODEL_VRAM:
        return _MODEL_VRAM[model]
    m = re.search(r":(\d+\.?\d*)b", model, re.I)
    if m:
        return round(float(m.group(1)) * 0.65, 1)
    return 5.0

def recommended_model_index(vram_gb):
    if   vram_gb >= 18: return 5
    elif vram_gb >= 16: return 3
    elif vram_gb >= 12: return 2
    elif vram_gb >=  8: return 1
    else:               return 0

def ollama_parallel(vram_gb, model):
    if vram_gb <= 0:
        return 1
    model_v = {
        "devstral-small-2:24b": 15, "qwen3.6:27b": 17, "qwen3:14b": 9,
        "gemma4:12b": 8, "qwen3:8b": 5,
    }.get(model, int(vram_gb * 0.65))
    free = vram_gb - model_v - 2
    return max(1, min(4, 1 + int(free // 4)))

# ---------------------------------------------------------------------------
# agent ID
# ---------------------------------------------------------------------------
def get_agent_id():
    id_file = Path(tempfile.gettempdir()) / "swarm-agent-id.txt"
    if id_file.exists():
        val = id_file.read_text().strip()
        if re.match(r"^[0-9a-f-]{36}$", val):
            return val
    val = str(uuid.uuid4())
    id_file.write_text(val, encoding="utf-8")
    return val

# ---------------------------------------------------------------------------
# Ollama management
# ---------------------------------------------------------------------------
def find_ollama():
    if shutil.which("ollama"):
        return shutil.which("ollama")
    candidates = []
    if platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        profile = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(local,   "Programs", "Ollama", "ollama.exe"),
            os.path.join(profile, "AppData", "Local", "Programs", "Ollama", "ollama.exe"),
            os.path.join(tempfile.gettempdir(), "ollama", "ollama.exe"),
        ]
    else:
        candidates = ["/usr/local/bin/ollama", "/usr/bin/ollama",
                      str(Path.home() / ".ollama" / "ollama")]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None

def install_ollama():
    if platform.system() == "Windows":
        return _install_ollama_windows()
    else:
        return _install_ollama_unix()

def _install_ollama_unix():
    step("Installing Ollama via install script...")
    write_status("INSTALLING", "running ollama install script")
    try:
        subprocess.run(
            "curl -fsSL https://ollama.ai/install.sh | sh",
            shell=True, check=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Ollama install failed: {e}")
    path = find_ollama()
    if not path:
        raise RuntimeError("Ollama not found after install")
    ok(f"Ollama ready at: {path}")
    return path

def _install_ollama_windows():
    local = os.environ.get("LOCALAPPDATA", tempfile.gettempdir())
    cache_dir   = os.path.join(local, "swarm-llm")
    install_dir = os.path.join(local, "Programs", "Ollama")
    os.makedirs(cache_dir,   exist_ok=True)
    os.makedirs(install_dir, exist_ok=True)
    zip_file = os.path.join(cache_dir, "ollama-windows-amd64.zip")

    # Try coordinator mirror first
    mirror_url   = f"{_coordinator}/ollama/ollama-windows-amd64.zip"
    official_url = "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"
    download_url = official_url
    try:
        r = requests.head(mirror_url, timeout=4)
        if r.status_code == 200:
            download_url = mirror_url
            step("Using coordinator mirror for Ollama download")
    except Exception:
        pass

    for attempt in range(3):
        try:
            step(f"Downloading Ollama from {download_url} ...")
            write_status("INSTALLING", "downloading Ollama...")
            with requests.get(download_url, stream=True, timeout=300) as r:
                r.raise_for_status()
                downloaded = 0
                with open(zip_file, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded % (5 * 1024**2) < 65536:
                            write_status("INSTALLING", f"downloading Ollama... {downloaded // 1024**2} MB")
            break
        except Exception as e:
            if attempt >= 2:
                raise RuntimeError(f"Download failed after 3 attempts: {e}")
            err(f"Retrying ({attempt+1}/3)...")
            time.sleep(2)

    step(f"Extracting to {install_dir} ...")
    write_status("INSTALLING", "extracting Ollama...")
    import zipfile
    with zipfile.ZipFile(zip_file) as z:
        z.extractall(install_dir)

    path = find_ollama()
    if not path:
        raise RuntimeError(f"Ollama not found after extract - check {install_dir}")
    ok(f"Ollama ready at: {path}")
    return path

def wait_ollama_ready(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/tags", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False

def pull_model(ollama_exe, model, port):
    step(f"Pulling '{model}' (instant if already cached)...")
    env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{port}")
    proc = subprocess.Popen(
        [ollama_exe, "pull", model],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env
    )
    last_report = 0
    for line in proc.stdout:
        line = line.rstrip()
        print(line)
        m = re.search(r"(\d+)%", line)
        if m and time.time() - last_report >= 5:
            speed = ""
            sm = re.search(r"([\d.]+ [MG]B/s)", line)
            if sm:
                speed = f"  {sm.group(1)}"
            write_status("DOWNLOADING", f"{m.group(1)}%{speed}  -  {model}")
            last_report = time.time()
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to pull '{model}'")
    ok(f"Model '{model}' ready.")

# ---------------------------------------------------------------------------
# coordinator comms
# ---------------------------------------------------------------------------
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ---------------------------------------------------------------------------
# llama.cpp binary management
# ---------------------------------------------------------------------------
_LLAMACPP_CACHE = Path(tempfile.gettempdir()) / "swarm-llamacpp"

def find_llamacpp():
    srv_name = "llama-server.exe"     if platform.system() == "Windows" else "llama-server"
    rpc_name = "llama-rpc-server.exe" if platform.system() == "Windows" else "llama-rpc-server"
    srv = shutil.which(srv_name)
    rpc = shutil.which(rpc_name)
    if srv and rpc:
        return {"llama_server": srv, "llama_rpc_server": rpc}
    cache_srv = _LLAMACPP_CACHE / srv_name
    cache_rpc = _LLAMACPP_CACHE / rpc_name
    if cache_srv.exists() and cache_rpc.exists():
        return {"llama_server": str(cache_srv), "llama_rpc_server": str(cache_rpc)}
    return None

def install_llamacpp():
    step("Fetching latest llama.cpp release from GitHub...")
    req = Request(
        "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest",
        headers={"User-Agent": "swarm-llm-agent"},
    )
    try:
        with urlopen(req, timeout=20) as resp:
            release = json.loads(resp.read())
    except Exception as e:
        raise RuntimeError(f"GitHub API failed: {e}")

    assets = release.get("assets", [])
    if platform.system() == "Windows":
        asset = next(
            (a for a in assets if re.search(r"win.*cuda.*x64.*\.zip", a["name"], re.I)), None
        ) or next(
            (a for a in assets
             if re.search(r"win.*x64.*\.zip", a["name"], re.I)
             and "openblas" not in a["name"].lower()), None
        )
    else:
        asset = next(
            (a for a in assets
             if re.search(r"(ubuntu|linux).*x64.*\.zip", a["name"], re.I)), None
        )
    if not asset:
        raise RuntimeError(f"No suitable llama.cpp asset for {platform.system()}")

    step(f"Downloading {asset['name']}  ({round(asset['size'] / 1024**2)} MB)...")
    _LLAMACPP_CACHE.mkdir(parents=True, exist_ok=True)
    zip_path = _LLAMACPP_CACHE / "llama-cpp.zip"
    try:
        with urlopen(asset["browser_download_url"], timeout=300) as resp:
            zip_path.write_bytes(resp.read())
    except Exception as e:
        raise RuntimeError(f"Download failed: {e}")

    step(f"Extracting to {_LLAMACPP_CACHE} ...")
    with zipfile.ZipFile(str(zip_path)) as zf:
        zf.extractall(str(_LLAMACPP_CACHE))
    zip_path.unlink(missing_ok=True)

    srv_name = "llama-server.exe"     if platform.system() == "Windows" else "llama-server"
    rpc_name = "llama-rpc-server.exe" if platform.system() == "Windows" else "llama-rpc-server"
    # Flatten if binaries landed in a subdirectory
    for sub in _LLAMACPP_CACHE.iterdir():
        if sub.is_dir() and (sub / srv_name).exists():
            for f in sub.iterdir():
                dest = _LLAMACPP_CACHE / f.name
                if not dest.exists():
                    f.rename(dest)
            try:
                sub.rmdir()
            except OSError:
                pass
            break

    srv_path = _LLAMACPP_CACHE / srv_name
    rpc_path = _LLAMACPP_CACHE / rpc_name
    if not srv_path.exists():
        raise RuntimeError(f"{srv_name} not found after extraction")
    if platform.system() != "Windows":
        srv_path.chmod(0o755)
        if rpc_path.exists():
            rpc_path.chmod(0o755)
    ok(f"llama.cpp installed at {_LLAMACPP_CACHE}")
    return {"llama_server": str(srv_path), "llama_rpc_server": str(rpc_path)}

def ensure_llamacpp():
    found = find_llamacpp()
    if found:
        ok(f"llama.cpp found: {found['llama_server']}")
        return found
    return install_llamacpp()

# ---------------------------------------------------------------------------
# GGUF resolution from Ollama blob cache
# ---------------------------------------------------------------------------
def _ollama_models_dir():
    if platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        return Path(local) / "Ollama" / "models"
    return Path.home() / ".ollama" / "models"

def resolve_gguf(model):
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    models_dir = _ollama_models_dir()
    for mp in [
        models_dir / "manifests" / "registry.ollama.ai" / "library" / name / tag,
        models_dir / "manifests" / "registry.ollama.ai" / name / tag,
    ]:
        if not mp.exists():
            continue
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
            layer = next(
                (l for l in manifest.get("layers", [])
                 if l.get("mediaType") == "application/vnd.ollama.image.model"),
                None,
            )
            if layer:
                hash_part = layer["digest"].split(":")[-1]
                blob = models_dir / "blobs" / f"sha256-{hash_part}"
                if blob.exists():
                    ok(f"Reusing Ollama blob cache: {blob}")
                    return str(blob)
        except Exception:
            continue
    return None

# ---------------------------------------------------------------------------
# RPC cluster execution helpers
# ---------------------------------------------------------------------------
def _wait_tcp(host, port, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            return True
        except OSError:
            time.sleep(1)
    return False

def _wait_llama_ready(port, timeout=120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(2)
    return False

def _rpc_poll_assignment(coordinator, api_key):
    try:
        r = requests.get(
            f"{coordinator}/agent/rpc/assignment",
            params={"agent_id": _agent_id},
            headers={"X-API-Key": api_key},
            timeout=8,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def rpc_infer(job_body, llama_port):
    opts = job_body.get("options", {})
    # The coordinator converts tool_call arguments string→dict for Ollama native format.
    # llama-server (OpenAI format) needs them back as a JSON string.
    messages = []
    for msg in job_body.get("messages", []):
        msg = dict(msg)
        if msg.get("tool_calls"):
            msg["tool_calls"] = [dict(tc) for tc in msg["tool_calls"]]
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                if isinstance(fn.get("arguments"), dict):
                    tc["function"] = dict(fn, arguments=json.dumps(fn["arguments"]))
        messages.append(msg)
    openai_body = {
        "model":    job_body.get("model", ""),
        "messages": messages,
        "stream":   False,
    }
    if opts.get("temperature") is not None:
        openai_body["temperature"] = opts["temperature"]
    predict = opts.get("num_predict", -1)
    if predict and predict != -1:
        openai_body["max_tokens"] = predict
    if job_body.get("tools"):
        openai_body["tools"] = job_body["tools"]
    if job_body.get("format"):
        openai_body["response_format"] = {"type": job_body["format"]}
    r = requests.post(
        f"http://127.0.0.1:{llama_port}/v1/chat/completions",
        json=openai_body,
        timeout=1800,
    )
    r.raise_for_status()
    return r.json()

def _stop_ollama_for_rpc():
    global _ollama_proc
    if _ollama_proc and _ollama_proc.poll() is None:
        step("Stopping Ollama to free GPU for RPC cluster...")
        _ollama_proc.terminate()
        try:
            _ollama_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _ollama_proc.kill()
    _ollama_proc = None

def _restart_ollama_after_rpc():
    global _ollama_proc
    if not _ollama_exe or not _ollama_port:
        return
    # If another process already owns the port, adopt it instead of fighting
    if wait_ollama_ready(_ollama_port, timeout=2):
        step(f"Ollama already responding on port {_ollama_port} — adopting it")
        _ollama_proc = None
        return
    step(f"Restarting Ollama on port {_ollama_port}...")
    parallel = ollama_parallel(_hw.get("vram_gb", 0), _model)
    env = dict(os.environ,
               OLLAMA_HOST=f"0.0.0.0:{_ollama_port}",
               OLLAMA_FLASH_ATTENTION="1",
               OLLAMA_KV_CACHE_TYPE="q8_0",
               OLLAMA_NUM_PARALLEL=str(parallel))
    _olog = Path.home() / ".swarm" / "ollama.log"
    _olog.parent.mkdir(exist_ok=True)
    _olog_fh = open(_olog, "a", encoding="utf-8", errors="replace")
    pkw = dict(env=env, stdout=_olog_fh, stderr=_olog_fh)
    if platform.system() == "Windows":
        pkw["creationflags"] = subprocess.CREATE_NO_WINDOW
    _ollama_proc = subprocess.Popen([_ollama_exe, "serve"], **pkw)
    time.sleep(2)  # brief pause — if port was in use proc will die within 1s
    if _ollama_proc.poll() is not None:
        # Start failed (likely port conflict that appeared after our check)
        if wait_ollama_ready(_ollama_port, timeout=5):
            step(f"Ollama port {_ollama_port} taken by another process — adopting it")
            _ollama_proc = None
            return
        err(f"Ollama failed to start (exit {_ollama_proc.returncode})")
        return
    if wait_ollama_ready(_ollama_port, 60):
        ok(f"Ollama restarted (PID {_ollama_proc.pid})")
    else:
        err("Ollama did not restart within 60s")

def run_rpc_worker(assignment, coordinator, api_key):
    cluster_id = assignment["cluster_id"]
    rpc_port   = assignment.get("rpc_port", 50052)
    bins = ensure_llamacpp()
    _stop_ollama_for_rpc()

    step(f"Starting llama-rpc-server on port {rpc_port}...")
    pkw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() == "Windows":
        pkw["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [bins["llama_rpc_server"], "--host", "0.0.0.0", "--port", str(rpc_port)],
        **pkw,
    )

    if not _wait_tcp("127.0.0.1", rpc_port, 30):
        proc.kill()
        err(f"llama-rpc-server did not listen on :{rpc_port}")
        _restart_ollama_after_rpc()
        return
    ok(f"llama-rpc-server ready on :{rpc_port} (PID {proc.pid})")
    write_status("RPC_WORKER", f"cluster {cluster_id[:8]}  port {rpc_port}")

    headers = {"X-API-Key": api_key}
    while True:
        asgn = _rpc_poll_assignment(coordinator, api_key)
        if not asgn or asgn.get("dissolve"):
            step(f"Dissolve signal — shutting down worker (cluster {cluster_id[:8]})")
            break
        if proc.poll() is not None:
            err("llama-rpc-server exited unexpectedly")
            break
        time.sleep(5)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    try:
        requests.post(
            f"{coordinator}/agent/rpc/done",
            json={"agent_id": _agent_id, "cluster_id": cluster_id, "role": "worker"},
            headers=headers, timeout=8,
        )
    except Exception:
        pass
    ok(f"Worker RPC duty done (cluster {cluster_id[:8]})")
    _restart_ollama_after_rpc()

def run_rpc_primary(assignment, coordinator, api_key):
    cluster_id = assignment["cluster_id"]
    model      = assignment["model"]
    workers    = assignment.get("workers", [])
    llama_port = assignment.get("llama_port", 8080)
    ctx_size   = assignment.get("ctx_size", 16384)

    bins      = ensure_llamacpp()
    gguf_path = resolve_gguf(model)
    if not gguf_path:
        err(f"GGUF for '{model}' not in Ollama cache — pull the model with Ollama first.")
        # Ollama was never stopped. Notify coordinator so it dissolves the cluster
        # immediately instead of waiting for the 30-minute form timeout.
        try:
            requests.post(
                f"{coordinator}/agent/rpc/done",
                json={"agent_id": _agent_id, "cluster_id": cluster_id, "role": "primary"},
                headers={"X-API-Key": api_key}, timeout=8,
            )
        except Exception:
            pass
        return

    _stop_ollama_for_rpc()

    cmd = [
        bins["llama_server"],
        "-m", gguf_path,
        "--host", "0.0.0.0",
        "--port", str(llama_port),
        "--ctx-size", str(ctx_size),
        "-ngl", "999",
        "--no-mmap",
    ]
    if workers:
        cmd += ["--rpc", ",".join(workers)]
        step(f"Waiting for {len(workers)} worker RPC server(s) to come up...")
        for endpoint in workers:
            host, _, port_str = endpoint.rpartition(":")
            if not _wait_tcp(host or "127.0.0.1", int(port_str), timeout=120):
                err(f"Worker {endpoint} did not come up within 120s — aborting cluster")
                _restart_ollama_after_rpc()
                return
            ok(f"Worker {endpoint} ready")
        step(f"Starting llama-server with RPC workers: {workers}")
    else:
        step("Starting llama-server (single-machine mode)")

    pkw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if platform.system() == "Windows":
        pkw["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, **pkw)

    step("Waiting for llama-server to be ready (model loading)...")
    if not _wait_llama_ready(llama_port, 300):
        proc.kill()
        err("llama-server did not become ready in 5 minutes")
        _restart_ollama_after_rpc()
        return
    ok(f"llama-server ready on :{llama_port} (PID {proc.pid})")
    write_status("RPC_PRIMARY", f"cluster {cluster_id[:8]}  model {model}")

    headers = {"X-API-Key": api_key}
    try:
        requests.post(
            f"{coordinator}/agent/rpc/ready",
            json={"agent_id": _agent_id, "cluster_id": cluster_id, "role": "primary"},
            headers=headers, timeout=8,
        )
    except Exception as e:
        err(f"Failed to signal RPC ready: {e}")

    model_enc = urlquote(model)
    agent_enc = urlquote(_agent_id)
    while True:
        asgn = _rpc_poll_assignment(coordinator, api_key)
        if not asgn or asgn.get("dissolve"):
            step(f"Dissolve signal — shutting down primary (cluster {cluster_id[:8]})")
            break
        if proc.poll() is not None:
            err("llama-server exited unexpectedly")
            break

        try:
            r = requests.get(
                f"{coordinator}/agent/jobs/next?model={model_enc}&agent_id={agent_enc}",
                headers=headers, timeout=10,
            )
            if r.status_code != 200:
                time.sleep(2)
                continue

            job    = r.json()
            job_id = job["id"]
            body   = json.loads(job["body_json"])
            ts = time.strftime("%H:%M:%S")
            print(_c("\033[36m", f"[{ts}] RPC job {job_id}  model={model}"))

            t0 = time.time()
            try:
                result = rpc_infer(body, llama_port)
                ms     = round((time.time() - t0) * 1000, 1)
                requests.post(
                    f"{coordinator}/agent/jobs/{job_id}/result",
                    json={"result": result, "elapsed_ms": ms},
                    headers=headers, timeout=30,
                )
                ok(f"RPC job {job_id} done in {ms/1000:.1f}s")
            except Exception as e:
                err(f"RPC job {job_id} failed: {e}")
                try:
                    requests.post(
                        f"{coordinator}/agent/jobs/{job_id}/result",
                        json={"error": str(e)},
                        headers=headers, timeout=10,
                    )
                except Exception:
                    pass
        except Exception:
            time.sleep(2)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    try:
        requests.post(
            f"{coordinator}/agent/rpc/done",
            json={"agent_id": _agent_id, "cluster_id": cluster_id, "role": "primary"},
            headers=headers, timeout=8,
        )
    except Exception:
        pass
    ok(f"Primary RPC duty done (cluster {cluster_id[:8]})")
    _restart_ollama_after_rpc()

def _rpc_thread_func(coordinator, api_key):
    step("[rpc] Polling for RPC assignments...")
    while True:
        assignment = _rpc_poll_assignment(coordinator, api_key)
        if assignment and not assignment.get("dissolve"):
            role = assignment.get("role")
            cid  = assignment.get("cluster_id", "")[:8]
            ok(f"[rpc] Assignment received: role={role}  cluster={cid}")
            # Signal work_loop to stop accepting new Ollama jobs immediately,
            # before we even start the role (closes the 5s race window).
            _rpc_assigned.set()
            _rpc_active.set()
            try:
                if role == "worker":
                    run_rpc_worker(assignment, coordinator, api_key)
                elif role == "primary":
                    run_rpc_primary(assignment, coordinator, api_key)
                else:
                    err(f"[rpc] Unknown role: {role}")
            except Exception as e:
                err(f"[rpc] Role execution error: {e}")
            finally:
                _rpc_active.clear()
                _rpc_assigned.clear()
        time.sleep(5)

def register(coordinator, api_key, port, model, hw):
    info = get_system_info(hw)
    body = {
        "agent_id":       _agent_id,
        "port":           port,
        "model":          model,
        "hostname":       info.get("hostname"),
        "cpu_name":       info.get("cpu_name"),
        "cpu_cores":      info.get("cpu_cores"),
        "ram_total_gb":   info.get("ram_gb"),
        "ram_free_gb":    None,
        "gpu_name":       info.get("gpu_name") if info.get("gpu_name") != "none" else None,
        "gpu_vram_gb":    info.get("vram_gb") if info.get("vram_gb", 0) > 0 else None,
        "network_mbps":   info.get("network_mbps"),
        "ollama_version": info.get("ollama_version"),
        "os_version":     info.get("os_version"),
        "lan_ip":         get_local_ip(),
    }
    r = requests.post(
        f"{coordinator}/register", json=body,
        headers={"X-API-Key": api_key}, timeout=15
    )
    r.raise_for_status()
    data = r.json()
    ok(f"Registered. Total machines: {data.get('machines_total', '?')}")

def test_coordinator(coordinator, api_key):
    step(f"Connecting to coordinator at {coordinator} ...")
    try:
        r = requests.get(f"{coordinator}/healthz", timeout=8)
        if r.status_code != 200:
            raise RuntimeError(f"unexpected status {r.status_code}")
    except Exception as e:
        err(f"Cannot reach coordinator at {coordinator}")
        err(f"Check the URL and make sure the server is running. ({e})")
        return False
    step("Validating API key ...")
    try:
        r = requests.post(
            f"{coordinator}/agent-status",
            json={"phase": "CONNECTING", "detail": "", "agent_id": _agent_id,
                  "hostname": socket.gethostname(), "model": _model},
            headers={"X-API-Key": api_key}, timeout=8
        )
        if r.status_code == 401:
            err("API key rejected (401)  -  check the key and try again.")
            return False
        r.raise_for_status()
    except requests.HTTPError:
        return False
    except Exception as e:
        err(f"Key validation failed: {e}")
        return False
    ok("Connected  -  key valid.")
    return True

# ---------------------------------------------------------------------------
# inference
# ---------------------------------------------------------------------------
def infer(port, body_json, job_id="", coordinator="", api_key=""):
    EYE_FRAMES   = ["o . o", "@ . @", "o . o", "> . <", "o . o", "^ . ^", "o . o", "* . *"]
    MOUTH_FRAMES = [" ___ ", " --- ", " ~~~ ", " ... ", " ### "]
    SPINNERS     = ["-", "\\", "|", "/"]
    THOUGHTS     = [
        "crunching tokens   ", "hot take incoming  ", "brb, doing math    ",
        "yes this is fast   ", "GPU go brrrr       ", "almost there...    ",
        "big brain moment   ",
    ]
    FRAME_SEC      = 0.15
    THOUGHT_FRAMES = int(5.0 / FRAME_SEC)  # advance phrase every ~5 seconds

    result = [None]
    error  = [None]

    def _run():
        try:
            r = requests.post(
                f"http://127.0.0.1:{port}/api/chat",
                data=body_json.encode(),
                headers={"Content-Type": "application/json"},
                timeout=1800,
            )
            r.raise_for_status()
            result[0] = r.text
        except Exception as e:
            error[0] = str(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    frame       = 0
    last_cancel = 0.0
    t0          = time.time()

    # Reserve 5 lines for the mascot box
    for _ in range(5):
        print()

    def _draw(eye, mouth, spin, elapsed, think):
        sys.stdout.write(
            "\033[5A"
            f"\r   .-----------.   \033[K\n"
            f"\r   |  ({eye})  |   \033[K\n"
            f"\r   |   {mouth}   |   {spin}  {elapsed}s\033[K\n"
            f"\r   '-----------'   \033[K\n"
            f"\r  [{think}]\033[K\n"
        )
        sys.stdout.flush()

    def _clear():
        sys.stdout.write("\033[5A" + (" " * 60 + "\n") * 5 + "\033[5A")
        sys.stdout.flush()

    while t.is_alive():
        elapsed = int(time.time() - t0)
        _draw(
            eye   = EYE_FRAMES[frame   % len(EYE_FRAMES)],
            mouth = MOUTH_FRAMES[frame % len(MOUTH_FRAMES)],
            spin  = SPINNERS[frame     % len(SPINNERS)],
            elapsed = elapsed,
            think = THOUGHTS[(frame // THOUGHT_FRAMES) % len(THOUGHTS)],
        )

        # Cancel check every ~5s
        if job_id and coordinator and time.time() - last_cancel >= 5:
            last_cancel = time.time()
            try:
                cr = requests.get(
                    f"{coordinator}/agent/jobs/{job_id}/cancelled",
                    headers={"X-API-Key": api_key}, timeout=3
                )
                if cr.status_code == 200 and cr.json().get("cancelled"):
                    t.join(timeout=2)
                    _clear()
                    raise RuntimeError(f"Job {job_id} cancelled by coordinator")
            except RuntimeError:
                raise
            except Exception:
                pass

        frame += 1
        time.sleep(FRAME_SEC)

    _clear()

    if error[0]:
        raise RuntimeError(error[0])
    return result[0]

# ---------------------------------------------------------------------------
# media work loop  (runs in a background thread)
# ---------------------------------------------------------------------------
MEDIA_INFER_PY = r'''
import argparse, os, sys, time, warnings
warnings.filterwarnings("ignore", category=FutureWarning)

def _download_model(model_id, token=None):
    """Pre-download model to HF cache, emitting DOWNLOAD {mb}/0 lines to stdout."""
    import huggingface_hub as _hf
    try:
        _hf.snapshot_download(model_id, local_files_only=True, token=token or None)
        return  # already cached
    except Exception:
        pass
    # Model is not cached — download it while emitting periodic progress via a monitor thread.
    import threading, pathlib, os as _os
    _cache_root = pathlib.Path(
        _os.environ.get("HF_HUB_CACHE") or
        _os.environ.get("HUGGINGFACE_HUB_CACHE") or
        pathlib.Path.home() / ".cache" / "huggingface" / "hub"
    )
    _model_dir = _cache_root / ("models--" + model_id.replace("/", "--"))
    _stop = threading.Event()
    def _emit():
        while not _stop.wait(8):
            try:
                sz = sum(f.stat().st_size for f in _model_dir.rglob("*") if f.is_file())
                print(f"DOWNLOAD {sz // 1024 // 1024}/0", flush=True)
            except Exception:
                print("DOWNLOAD 0/0", flush=True)
    t = threading.Thread(target=_emit, daemon=True)
    t.start()
    try:
        _hf.snapshot_download(model_id, token=token or None)
    finally:
        _stop.set()
        t.join(timeout=5)

def _load(model_id, pipeline_cls, dtype, token=None, **kw):
    """Load pipeline fully onto GPU. Use when model fits in VRAM."""
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(
            f"CUDA unavailable (torch {torch.__version__} — CPU-only build). "
            "Re-run the agent to reinstall torch with CUDA support."
        )
    _download_model(model_id, token)
    return pipeline_cls.from_pretrained(
        model_id, dtype=dtype, token=token or None, local_files_only=True, **kw
    ).to("cuda")

def _load_offload(model_id, pipeline_cls, dtype, token=None, **kw):
    """Load pipeline with CPU offload. Mutually exclusive with .to('cuda')."""
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(
            f"CUDA unavailable (torch {torch.__version__} — CPU-only build). "
            "Re-run the agent to reinstall torch with CUDA support."
        )
    _download_model(model_id, token)
    pipe = pipeline_cls.from_pretrained(
        model_id, dtype=dtype, token=token or None, local_files_only=True, **kw
    )
    pipe.enable_model_cpu_offload()
    return pipe

def run_image(model, prompt, neg, out_dir, n, size, quality, token=None):
    import torch
    w, h = map(int, size.split("x"))
    os.makedirs(out_dir, exist_ok=True)

    if model == "sdxl":
        from diffusers import StableDiffusionXLPipeline, StableDiffusionXLImg2ImgPipeline
        # SDXL requires base + refiner; base alone produces low-quality images.
        # enable_model_cpu_offload handles VRAM — do NOT call .to("cuda") with it.
        base = _load_offload(
            "stabilityai/stable-diffusion-xl-base-1.0",
            StableDiffusionXLPipeline, torch.float16, token,
            use_safetensors=True, variant="fp16",
        )
        refiner = _load_offload(
            "stabilityai/stable-diffusion-xl-refiner-1.0",
            StableDiffusionXLImg2ImgPipeline, torch.float16, token,
            use_safetensors=True, variant="fp16",
        )
        default_neg = ("worst quality, low quality, blurry, watermark, "
                       "ugly, distorted, deformed, noise")
        neg_prompt = neg if neg else default_neg
        steps, guidance, denoise_frac = 40, 7.5, 0.8

        images = []
        for _ in range(n):
            # Base generates low-frequency structure as a latent
            latent = base(
                prompt=prompt, negative_prompt=neg_prompt,
                num_inference_steps=steps, guidance_scale=guidance,
                denoising_end=denoise_frac,
                output_type="latent",
                width=w, height=h,
            ).images
            # Refiner adds fine detail
            img = refiner(
                prompt=prompt, negative_prompt=neg_prompt,
                num_inference_steps=steps, guidance_scale=guidance,
                denoising_start=denoise_frac,
                image=latent,
            ).images[0]
            images.append(img)

    else:
        sys.exit(f"Unknown image model: {model}")

    for i, img in enumerate(images):
        fp = os.path.join(out_dir, f"img_{i}_{int(time.time())}.png")
        img.save(fp)
        print(fp, flush=True)

def _progress_cb(total_steps):
    """Return a diffusers callback that prints PROGRESS lines to stdout."""
    def cb(pipe, step, timestep, kwargs):
        print(f"PROGRESS {step+1}/{total_steps}", flush=True)
        return kwargs
    return cb

# frames-per-second for each model — used to compute num_frames from duration
_MODEL_NATIVE_FPS = {"wan-2.1-t2v-1.3b": 16}

def run_video(model, prompt, neg, out_file, duration, width, height,
              guidance_scale=5.0, num_inference_steps=50, fps=8, token=None):
    import torch
    cb = _progress_cb(num_inference_steps)
    native_fps = _MODEL_NATIVE_FPS.get(model, fps)

    if model == "ltx-video":
        from diffusers import LTXPipeline
        pipe = _load_offload("Lightricks/LTX-Video", LTXPipeline, torch.bfloat16, token)
        result = pipe(prompt=prompt, negative_prompt=neg or None,
                      width=width, height=height,
                      num_frames=duration * native_fps + 1,
                      num_inference_steps=num_inference_steps,
                      guidance_scale=guidance_scale,
                      callback_on_step_end=cb)
    elif model == "cogvideox-2b":
        from diffusers import CogVideoXPipeline
        pipe = _load("THUDM/CogVideoX-2b", CogVideoXPipeline, torch.bfloat16, token)
        # CogVideoX is trained at 720x480, 49 frames. Resolution is not adjustable.
        result = pipe(prompt=prompt,
                      num_frames=49, width=720, height=480,
                      num_inference_steps=num_inference_steps,
                      guidance_scale=guidance_scale,
                      callback_on_step_end=cb)
    elif model == "wan-2.1-t2v-1.3b":
        from diffusers import WanPipeline
        pipe = _load("Wan-AI/Wan2.1-T2V-1.3B-Diffusers", WanPipeline, torch.bfloat16, token)
        # Wan requires num_frames = 4k+1
        result = pipe(prompt=prompt, negative_prompt=neg or None,
                      height=height, width=width,
                      num_frames=duration * native_fps + 1,
                      num_inference_steps=num_inference_steps,
                      guidance_scale=guidance_scale,
                      callback_on_step_end=cb)
    else:
        sys.exit(f"Unknown video model: {model}")
    frames = result.frames[0]
    import numpy as np, imageio
    frames_np = [np.array(f) for f in frames]
    os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    _MODEL_FPS = {"wan-2.1-t2v-1.3b": 16}
    fps = _MODEL_FPS.get(model, 8)
    imageio.mimwrite(out_file, frames_np, fps=fps, quality=8)
    print(out_file, flush=True)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--type",      required=True, choices=["image", "video"])
    p.add_argument("--model",     required=True)
    p.add_argument("--prompt",    required=True)
    p.add_argument("--neg",       default="")
    p.add_argument("--hf-token",  default="")
    p.add_argument("--out-dir",   default=".")
    p.add_argument("--out-file",  default="out.mp4")
    p.add_argument("--n",         type=int, default=1)
    p.add_argument("--size",      default="1024x1024")
    p.add_argument("--quality",   default="standard")
    p.add_argument("--duration",         type=int,   default=5)
    p.add_argument("--width",            type=int,   default=704)
    p.add_argument("--height",           type=int,   default=480)
    p.add_argument("--guidance-scale",   type=float, default=5.0)
    p.add_argument("--steps",            type=int,   default=50)
    p.add_argument("--fps",              type=int,   default=8)
    a = p.parse_args()
    tok = a.hf_token or os.environ.get("HF_TOKEN", "")
    if a.type == "image":
        run_image(a.model, a.prompt, a.neg, a.out_dir, a.n, a.size, a.quality, tok)
    else:
        run_video(a.model, a.prompt, a.neg, a.out_file, a.duration, a.width, a.height,
                  guidance_scale=a.guidance_scale, num_inference_steps=a.steps,
                  fps=a.fps, token=tok)
'''

def _cuda_install_candidates():
    """Return ordered list of (description, pip_extra_args) to try for a CUDA torch build."""
    candidates = []
    # Try nvidia-smi on PATH first, then common Windows install locations.
    smi_cmds = ["nvidia-smi"]
    if platform.system() == "Windows":
        smi_cmds += [
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ]
    driver = None
    for smi in smi_cmds:
        try:
            out = subprocess.check_output(
                [smi, "--query-gpu=driver_version", "--format=csv,noheader"],
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace"
            ).strip().split("\n")[0]
            driver = float(out.split(".")[0])
            break
        except Exception:
            continue

    if driver is not None:
        # driver → CUDA toolkit version mapping (newest compat first)
        if driver >= 570:
            tags = ["cu128", "cu126", "cu124"]
        elif driver >= 560:
            tags = ["cu126", "cu124"]
        elif driver >= 550:
            tags = ["cu124", "cu126"]
        elif driver >= 525:
            tags = ["cu121", "cu124"]
        elif driver >= 450:
            tags = ["cu118"]
        else:
            tags = ["cu128", "cu126", "cu124", "cu121", "cu118"]
    else:
        # nvidia-smi not found; try common CUDA versions newest-first (stable only to save time)
        tags = ["cu128", "cu126", "cu124", "cu121", "cu118"]

    for tag in tags:
        stable  = f"https://download.pytorch.org/whl/{tag}"
        nightly = f"https://download.pytorch.org/whl/nightly/{tag}"
        candidates.append((f"stable/{tag}",  ["--index-url", stable]))
        candidates.append((f"nightly/{tag}", ["--index-url", nightly, "--pre"]))
    return candidates

def _torch_has_cuda(python_exe=None):
    """Spawn a fresh subprocess to check torch.cuda.is_available() (avoids import cache)."""
    exe = python_exe or sys.executable
    try:
        r = subprocess.run(
            [exe, "-c",
             "import torch; print('1' if torch.cuda.is_available() else '0')"],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace"
        )
        return r.stdout.strip() == "1"
    except Exception:
        return False

def _find_cuda_python():
    """Return the Python executable that has CUDA-enabled torch.

    On Python 3.14+ Windows, PyTorch has no CUDA wheels — try the Windows
    Python Launcher (py -3.12 / py -3.13 / py -3.11) which likely has an
    older Python where CUDA wheels exist.
    """
    if _torch_has_cuda():
        return sys.executable
    if platform.system() == "Windows":
        for ver in ["3.12", "3.13", "3.11", "3.10"]:
            try:
                r = subprocess.run(
                    ["py", f"-{ver}", "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace"
                )
                if r.returncode != 0 or not r.stdout.strip():
                    continue
                exe = r.stdout.strip()
                if _torch_has_cuda(exe):
                    step(f"  py -{ver} has CUDA torch — using {exe} for inference")
                    return exe
            except Exception:
                pass
    return sys.executable  # no better option found

def _pip_install(pkgs, extra_args=None, python_exe=None):
    exe = python_exe or sys.executable
    cmd = [exe, "-m", "pip", "install", "--quiet"] + pkgs
    if extra_args:
        cmd += extra_args
    subprocess.check_call(cmd, stderr=subprocess.STDOUT)

def install_media_deps(python_exe=None):
    exe = python_exe or sys.executable
    step(f"Checking media deps for {exe} ...")

    if _torch_has_cuda(exe):
        step("  torch already has CUDA — skipping reinstall")
    else:
        torch_ok = False
        for desc, extra in _cuda_install_candidates():
            step(f"  torch: trying {desc} ...")
            try:
                _pip_install(["torch", "torchvision"],
                             extra + ["--force-reinstall", "--no-deps"],
                             python_exe=exe)
                if _torch_has_cuda(exe):
                    step(f"  CUDA torch installed via {desc}")
                    torch_ok = True
                    break
                step(f"  {desc}: still CPU-only, trying next index...")
            except subprocess.CalledProcessError:
                step(f"  {desc}: pip failed, trying next index...")

        if not torch_ok:
            step("  torch: all CUDA indexes failed, falling back to PyPI...")
            try:
                _pip_install(["torch", "torchvision"],
                             ["--force-reinstall", "--no-deps"],
                             python_exe=exe)
            except subprocess.CalledProcessError:
                err("torch install failed  -  media inference disabled")

    for group in [
        ["diffusers", "transformers", "accelerate", "safetensors", "huggingface_hub"],
        ["imageio[ffmpeg]", "sentencepiece", "protobuf"],
    ]:
        step(f"pip install {group[0]} ...")
        try:
            _pip_install(group, ["--upgrade"], python_exe=exe)
        except subprocess.CalledProcessError as e:
            err(f"pip install {group[0]} failed: {e}")

    ok("Media deps ready.")

def _run_infer(cmd, on_line, silence_timeout=1800, max_job_time=7200):
    """Run an infer.py subprocess, calling on_line(line) for each stdout line.

    on_line must return True for lines that represent real progress (PROGRESS /
    DOWNLOAD).  The silence clock is reset ONLY on those lines — verbose
    diffusers/torch noise that doesn't indicate forward progress is ignored for
    timeout purposes.  Kills the process and raises RuntimeError if no
    meaningful progress line is seen for silence_timeout seconds.
    Stderr is drained concurrently to prevent pipe-buffer deadlock.
    Returns (returncode, stderr_text).
    """
    import queue as _queue
    _env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", env=_env)

    stdout_q = _queue.Queue()
    stderr_q = _queue.Queue()

    def _drain(stream, q):
        for ln in stream:
            q.put(ln)
        q.put(None)

    threading.Thread(target=_drain, args=(proc.stdout, stdout_q), daemon=True).start()
    threading.Thread(target=_drain, args=(proc.stderr, stderr_q), daemon=True).start()

    last_progress = time.time()
    job_start     = time.time()
    while True:
        try:
            line = stdout_q.get(timeout=30)
        except _queue.Empty:
            elapsed = time.time() - job_start
            if time.time() - last_progress > silence_timeout:
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"no inference progress for {silence_timeout//60}m — killed (hung model load or CUDA init)"
                )
            if elapsed > max_job_time:
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"job exceeded {max_job_time//3600}h hard limit — killed"
                )
            continue
        if line is None:
            break
        result = on_line(line.rstrip("\n"))
        if result == "abort":
            proc.kill()
            proc.wait()
            raise RuntimeError("coordinator aborted job")
        elif result:
            last_progress = time.time()
        if time.time() - job_start > max_job_time:
            proc.kill()
            proc.wait()
            raise RuntimeError(
                f"job exceeded {max_job_time//3600}h hard limit — killed"
            )

    proc.wait()
    stderr_lines = []
    while True:
        try:
            ln = stderr_q.get_nowait()
        except _queue.Empty:
            break
        if ln is None:
            break
        stderr_lines.append(ln)
    return proc.returncode, "".join(stderr_lines)


def _media_loop(coordinator, api_key, agent_id, vram_gb, infer_py_path, hf_token=""):
    headers         = {"X-API-Key": api_key}
    deps_installed  = False
    infer_python    = sys.executable   # may be updated to py -3.12 etc. after deps check
    aid_enc         = urlquote(agent_id)
    last_status_log = 0
    STATUS_INTERVAL = 60

    print(_c(CYAN, f"  [media] thread started  -  {vram_gb} GB VRAM  -  polling {coordinator}"))

    def _report(phase, model=None):
        try:
            url = f"{coordinator}/agent/media/status?agent_id={aid_enc}&phase={phase}"
            if model:
                url += f"&model={urlquote(model)}"
            requests.post(url, headers=headers, timeout=3)
        except Exception:
            pass

    _report("IDLE")
    _last_update_check = 0.0

    while True:
        # ── periodic self-update check ────────────────────────────────────────
        if time.time() - _last_update_check >= 300:
            _last_update_check = time.time()
            _check_for_update(coordinator, headers)

        # ── poll coordinator ──────────────────────────────────────────────────
        try:
            r = requests.get(
                f"{coordinator}/agent/media/jobs/next?vram_gb={vram_gb}&agent_id={aid_enc}",
                headers=headers, timeout=8
            )
        except Exception as e:
            ts = time.strftime("%H:%M:%S")
            print(_c(YELLOW, f"  [media] [{ts}] coordinator unreachable: {e}  -  retrying in 5s"))
            time.sleep(5)
            continue

        if r.status_code == 204:
            if time.time() - last_status_log >= STATUS_INTERVAL:
                ts = time.strftime("%H:%M:%S")
                print(f"  [media] [{ts}] idle  -  no jobs queued for {vram_gb} GB VRAM")
                last_status_log = time.time()
            time.sleep(3)
            continue
        if r.status_code != 200:
            print(_c(YELLOW, f"  [media] unexpected status {r.status_code}, retrying..."))
            time.sleep(5)
            continue

        # ── got a job ─────────────────────────────────────────────────────────
        last_status_log = time.time()
        job      = r.json()
        job_id   = job["id"]
        job_type = job["type"]
        model    = job["model"]
        body     = json.loads(job["body_json"])
        print(_c(CYAN, f"  [media] {job_type} job {job_id[:8]}  -  model={model}"))

        # ── lazy deps install ─────────────────────────────────────────────────
        if not deps_installed:
            print(_c(YELLOW, "  [media] installing deps (first job)..."))
            _report("DOWNLOADING_DEPS", model)
            try:
                install_media_deps()                   # install for sys.executable first
                infer_python = _find_cuda_python()     # may fall back to py -3.12 etc.
                if infer_python != sys.executable:
                    install_media_deps(infer_python)   # ensure deps present in that env
                if not _torch_has_cuda(infer_python):
                    raise RuntimeError(
                        f"no CUDA torch found in any available Python — "
                        "install torch with CUDA support manually"
                    )
                deps_installed = True
                print(_c(GREEN, f"  [media] deps ready  python={infer_python}"))
            except Exception as deps_err:
                print(_c(RED, f"  [media] deps failed: {deps_err}"))
                try:
                    requests.post(
                        f"{coordinator}/agent/media/jobs/{job_id}/error?job_type={job_type}",
                        json={"error": str(deps_err)}, headers=headers, timeout=10
                    )
                except Exception:
                    pass
                # Self-update: restart if coordinator has a newer agent.py
                _check_for_update(coordinator, headers)
                time.sleep(5)
                continue  # back to polling; next job will retry install

        # ── run inference ─────────────────────────────────────────────────────
        _report("RUNNING", model)
        tmp_dir = Path(tempfile.gettempdir()) / "swarm-media"
        tmp_dir.mkdir(exist_ok=True)
        t0 = time.time()

        try:
            if job_type == "image":
                out_dir = tmp_dir / job_id
                out_dir.mkdir(exist_ok=True)
                cmd = [infer_python, str(infer_py_path),
                       "--type", "image", "--model", model,
                       "--prompt", body["prompt"],
                       "--n", str(body.get("n", 1)),
                       "--size", body.get("size", "1024x1024"),
                       "--quality", body.get("quality", "standard"),
                       "--out-dir", str(out_dir)]
                if body.get("negative_prompt"):
                    cmd += ["--neg", body["negative_prompt"]]
                if hf_token:
                    cmd += ["--hf-token", hf_token]
                stdout_lines = []
                _img_dl_stall = [0.0, time.time()]  # [last_mb, last_advance_time]
                def _on_image_line(line):
                    line = line.strip()
                    if line.startswith("DOWNLOAD "):
                        try:
                            dl_mb, total_mb = map(float, line.split()[1].split("/"))
                            pct = int(dl_mb / total_mb * 100) if total_mb else 0
                            if dl_mb > _img_dl_stall[0]:
                                _img_dl_stall[0] = dl_mb
                                _img_dl_stall[1] = time.time()
                            elif time.time() - _img_dl_stall[1] > 120:
                                raise RuntimeError(f"download stalled at {dl_mb:.0f} MB for 2min — killed")
                            resp = requests.post(
                                f"{coordinator}/agent/media/jobs/{job_id}/progress?job_type=image",
                                json={"phase": "downloading", "downloaded_mb": dl_mb, "total_mb": total_mb, "pct": pct},
                                headers=headers, timeout=5
                            )
                            if resp.ok and resp.json().get("abort"):
                                return "abort"
                            print(f"  [media] image {job_id[:8]} download {dl_mb:.0f}/{total_mb:.0f} MB ({pct}%)")
                        except RuntimeError:
                            raise
                        except Exception:
                            pass
                        return True
                    else:
                        stdout_lines.append(line)
                    return False
                rc, stderr_out = _run_infer(cmd, _on_image_line, silence_timeout=600, max_job_time=3600)
                if rc != 0:
                    raise RuntimeError(f"inference error: {stderr_out}")
                out_paths = [l for l in stdout_lines if l and os.path.isfile(l)]
                if not out_paths:
                    raise RuntimeError("no output files produced")
                out_file = out_paths[0]
                ms  = int((time.time() - t0) * 1000)
                ext = Path(out_file).suffix.lstrip(".")
                data = open(out_file, "rb").read()
                requests.post(
                    f"{coordinator}/agent/media/jobs/{job_id}/result"
                    f"?job_type=image&ext={ext}&elapsed_ms={ms}",
                    data=data, headers={**headers, "Content-Type": "application/octet-stream"},
                    timeout=120
                ).raise_for_status()
                print(f"  [media] image done {ms/1000:.1f}s  {len(data)/1024**2:.1f} MB")
            else:
                out_file = str(tmp_dir / f"{job_id}.mp4")
                cmd = [infer_python, str(infer_py_path),
                       "--type",           "video",
                       "--model",          model,
                       "--prompt",         body["prompt"],
                       "--duration",       str(body.get("duration", 5)),
                       "--width",          str(body.get("width",  704)),
                       "--height",         str(body.get("height", 480)),
                       "--guidance-scale", str(body.get("guidance_scale", 5.0)),
                       "--steps",          str(body.get("num_inference_steps", 50)),
                       "--fps",            str(body.get("fps", 8)),
                       "--out-file",       out_file]
                if body.get("negative_prompt"):
                    cmd += ["--neg", body["negative_prompt"]]
                if hf_token:
                    cmd += ["--hf-token", hf_token]
                _vid_dl_stall = [0.0, time.time()]  # [last_mb, last_advance_time]
                def _on_video_line(line):
                    line = line.strip()
                    if line.startswith("PROGRESS "):
                        try:
                            step, total = map(int, line.split()[1].split("/"))
                            pct = int(step / total * 100)
                            resp = requests.post(
                                f"{coordinator}/agent/media/jobs/{job_id}/progress?job_type=video",
                                json={"phase": "generating", "step": step, "total": total, "pct": pct},
                                headers=headers, timeout=5
                            )
                            if resp.ok and resp.json().get("abort"):
                                return "abort"
                            print(f"  [media] video {job_id[:8]} step {step}/{total} ({pct}%)")
                        except Exception:
                            pass
                        return True
                    elif line.startswith("DOWNLOAD "):
                        try:
                            dl_mb, total_mb = map(float, line.split()[1].split("/"))
                            pct = int(dl_mb / total_mb * 100) if total_mb else 0
                            if dl_mb > _vid_dl_stall[0]:
                                _vid_dl_stall[0] = dl_mb
                                _vid_dl_stall[1] = time.time()
                            elif time.time() - _vid_dl_stall[1] > 120:
                                raise RuntimeError(f"download stalled at {dl_mb:.0f} MB for 2min — killed")
                            resp = requests.post(
                                f"{coordinator}/agent/media/jobs/{job_id}/progress?job_type=video",
                                json={"phase": "downloading", "downloaded_mb": dl_mb, "total_mb": total_mb, "pct": pct},
                                headers=headers, timeout=5
                            )
                            if resp.ok and resp.json().get("abort"):
                                return "abort"
                            print(f"  [media] video {job_id[:8]} download {dl_mb:.0f}/{total_mb:.0f} MB ({pct}%)")
                        except RuntimeError:
                            raise
                        except Exception:
                            pass
                        return True
                    return False
                rc, stderr_out = _run_infer(cmd, _on_video_line, silence_timeout=3600)
                if rc != 0:
                    raise RuntimeError(f"inference error: {stderr_out}")
                ms   = int((time.time() - t0) * 1000)
                data = open(out_file, "rb").read()
                requests.post(
                    f"{coordinator}/agent/media/jobs/{job_id}/result"
                    f"?job_type=video&ext=mp4&elapsed_ms={ms}",
                    data=data, headers={**headers, "Content-Type": "application/octet-stream"},
                    timeout=600
                ).raise_for_status()
                print(f"  [media] video done {ms/1000:.1f}s  {len(data)/1024**2:.1f} MB")
        except Exception as e:
            err_msg = str(e)
            print(_c(RED, f"  [media] job {job_id[:8]} failed: {err_msg}"))
            try:
                requests.post(
                    f"{coordinator}/agent/media/jobs/{job_id}/error?job_type={job_type}",
                    json={"error": err_msg}, headers=headers, timeout=10
                )
            except Exception:
                pass
        finally:
            _report("IDLE")

def start_media_loop(coordinator, api_key, agent_id, vram_gb, hf_token=""):
    tmp_dir    = Path(tempfile.gettempdir()) / "swarm-media"
    tmp_dir.mkdir(exist_ok=True)
    infer_path = tmp_dir / "infer.py"
    infer_path.write_text(MEDIA_INFER_PY, encoding="utf-8")

    t = threading.Thread(
        target=_media_loop,
        args=(coordinator, api_key, agent_id, vram_gb, infer_path, hf_token),
        daemon=True,
    )
    t.start()
    return t

# ---------------------------------------------------------------------------
# main work loop
# ---------------------------------------------------------------------------
def _heartbeat_thread(coordinator, api_key, port):
    """Dedicated thread: sends heartbeats every 15s regardless of inference state.
    The main work_loop blocks during infer() (up to minutes), so heartbeats MUST
    live in their own thread or the coordinator marks the machine dead mid-job."""
    headers = {"X-API-Key": api_key}
    while True:
        try:
            r = requests.post(
                f"{coordinator}/heartbeat",
                json={"port": port, "agent_id": _agent_id},
                headers=headers, timeout=5,
            )
            if r.status_code == 404:
                print(_c(YELLOW, f"[{time.strftime('%H:%M:%S')}] Coordinator restarted, re-registering..."))
                try:
                    register(coordinator, api_key, port, _model, _hw)
                except Exception:
                    pass
            time.sleep(15)
        except Exception:
            # Network blip — retry quickly so we don't exceed the dead threshold
            time.sleep(3)

def work_loop(ollama_exe, coordinator, api_key, port, vram_gb):
    global _model, _hw, _ollama_proc
    headers    = {"X-API-Key": api_key}
    vram_limit = max(6.0, vram_gb - 1.0)

    # Heartbeat runs in its own thread — infer() can block for minutes and
    # must not starve heartbeats (coordinator marks machine dead after 45s).
    threading.Thread(
        target=_heartbeat_thread, args=(coordinator, api_key, port),
        daemon=True, name="heartbeat",
    ).start()

    # Pre-populate pulled models from local ollama list
    pulled           = set()
    failed_pulls: dict = {}      # model → time.time() when it last failed (5-min cooldown)
    _skip_logged: dict = {}      # model → time.time() when we last printed the skip message
    _last_status_write = 0.0     # throttle periodic write_status("RUNNING") re-broadcasts
    try:
        env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{port}")
        out = subprocess.check_output(
            [ollama_exe, "list"], stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", env=env
        )
        for line in out.splitlines()[1:]:
            name = line.split()[0] if line.split() else ""
            if name:
                pulled.add(name)
        if pulled:
            step(f"Already on disk: {', '.join(pulled)}")
    except Exception:
        pass

    write_status("RUNNING", f"accepting any model that fits in {vram_gb} GB VRAM  -  Ctrl+C to quit")

    _wl_update_check = 0.0
    while True:
      try:
        # Pause while this machine has a pending or active RPC assignment
        if _rpc_assigned.is_set() or _rpc_active.is_set():
            time.sleep(2)
            continue

        # Restart Ollama if it died unexpectedly.
        if _ollama_proc and _ollama_proc.poll() is not None:
            rc = _ollama_proc.returncode
            # Exit code 1 on startup almost always means port already in use.
            # Check before logging a crash — it's not a real crash.
            if rc == 1 and wait_ollama_ready(_ollama_port, timeout=3):
                step(f"Ollama exit 1 but port {_ollama_port} is live — another process owns it, adopting")
                _ollama_proc = None
                continue
            _log_crash(RuntimeError(f"Ollama exited (code {rc})"))
            err(f"Ollama died (exit {rc}) — restarting...")
            try:
                _olog = Path.home() / ".swarm" / "ollama.log"
                lines = _olog.read_text(encoding="utf-8", errors="replace").splitlines()
                for ln in lines[-5:]:
                    err(f"  ollama: {ln}")
            except Exception:
                pass
            _restart_ollama_after_rpc()
            time.sleep(3)
            continue
        elif _ollama_proc is None and _ollama_port and not wait_ollama_ready(_ollama_port, timeout=1):
            # Adopted Ollama (managed by another process) stopped responding.
            err(f"Adopted Ollama on port {_ollama_port} is not responding — restarting...")
            _restart_ollama_after_rpc()
            time.sleep(3)
            continue

        # Re-broadcast RUNNING status every 60s — also warms the NAT path
        # before the update check so the large agent.py download succeeds.
        if time.time() - _last_status_write >= 60:
            _last_status_write = time.time()
            write_status("RUNNING", f"accepting any model that fits in {vram_gb} GB VRAM")

        # Periodic self-update check (every 5 min) — runs AFTER write_status
        # so the HTTPS path is warm when fetching the large agent.py file.
        if time.time() - _wl_update_check >= 300:
            _wl_update_check = time.time()
            _check_for_update(coordinator, headers)

        # Ask coordinator which models have queued jobs
        candidates = []
        try:
            r = requests.get(
                f"{coordinator}/agent/jobs/queued-models",
                headers=headers, timeout=8
            )
            r.raise_for_status()
            candidates = [m for m in r.json().get("models", [])
                          if model_vram(m) <= vram_limit]
        except Exception:
            pass

        served = False
        for model in candidates:
            model_enc = urlquote(model)

            # Pull on demand — skip models that recently failed (5-min cooldown)
            if model not in pulled:
                _PULL_COOLDOWN = 300
                last_fail = failed_pulls.get(model, 0)
                if time.time() - last_fail < _PULL_COOLDOWN:
                    # Only log once per minute so the terminal isn't spammed
                    if time.time() - _skip_logged.get(model, 0) >= 60:
                        remaining = int(_PULL_COOLDOWN - (time.time() - last_fail))
                        err(f"Skipping {model} — pull failed, retry in {remaining}s")
                        _skip_logged[model] = time.time()
                    continue
                write_status("DOWNLOADING", f"demand detected  -  pulling {model}")
                try:
                    pull_model(ollama_exe, model, port)
                    pulled.add(model)
                    failed_pulls.pop(model, None)
                    _skip_logged.pop(model, None)
                    _model = model
                    try:
                        register(coordinator, api_key, port, model, _hw)
                    except Exception:
                        pass
                except Exception as e:
                    failed_pulls[model] = time.time()
                    _skip_logged.pop(model, None)  # log the failure immediately next iteration
                    err_msg = f"Pull failed for {model}: {e} — will retry in 5 min"
                    err(err_msg)
                    try:
                        requests.post(
                            f"{coordinator}/agent-status",
                            json={"phase": "RUNNING", "detail": err_msg,
                                  "agent_id": _agent_id, "hostname": socket.gethostname(),
                                  "model": model},
                            headers=headers, timeout=5
                        )
                    except Exception:
                        pass
                    continue

            # Pick up a job
            try:
                agent_id_enc = urlquote(_agent_id)
                r = requests.get(
                    f"{coordinator}/agent/jobs/next?model={model_enc}&agent_id={agent_id_enc}",
                    headers=headers, timeout=10
                )
                if r.status_code != 200:
                    continue

                job          = r.json()
                job_id       = job["id"]
                job_body_json = job["body_json"]
                ts = time.strftime("%H:%M:%S")
                print(_c("\033[36m", f"[{ts}] Job {job_id}  model={model}"))

                t0 = time.time()
                _infer_exc = None
                try:
                    raw_result = infer(port, job_body_json, job_id, coordinator, api_key)
                except Exception as _e:
                    _infer_exc = _e

                if _infer_exc is None:
                    ms        = round((time.time() - t0) * 1000, 1)
                    post_body = f'{{"result":{raw_result},"elapsed_ms":{ms}}}'
                    # For fast jobs (<5s) no cancel-check fires during inference, leaving
                    # the shared NAT path "cold".  A warm-up GET refreshes the NAT
                    # state so the result POST isn't reset.
                    try:
                        requests.get(
                            f"{coordinator}/agent/jobs/{job_id}/cancelled",
                            headers=headers, timeout=5,
                        )
                        time.sleep(2)
                    except Exception:
                        time.sleep(5)  # GET failed — still wait for NAT to settle
                    _conn_err = None
                    for _attempt in range(6):
                        try:
                            requests.post(
                                f"{coordinator}/agent/jobs/{job_id}/result",
                                data=post_body.encode(),
                                headers={**headers, "Content-Type": "application/json"},
                                timeout=30
                            )
                            _conn_err = None
                            break
                        except requests.exceptions.ConnectionError as _ce:
                            _conn_err = _ce
                            time.sleep(5 * (_attempt + 1))  # 5s, 10s, 15s, 20s, 25s
                    if _conn_err is None:
                        ok(f"Job {job_id} done in {ms/1000:.1f}s")
                    else:
                        # Connection reset after coordinator confirmed receipt (WinError 10054).
                        # The coordinator's idempotency guard means this is safe to ignore.
                        err(f"Job {job_id} result POST failed ({_conn_err}) — coordinator likely has the result")
                else:
                    err(f"Job {job_id} inference failed: {_infer_exc}")
                    for _attempt in range(3):
                        try:
                            requests.post(
                                f"{coordinator}/agent/jobs/{job_id}/result",
                                json={"error": str(_infer_exc)},
                                headers=headers, timeout=10
                            )
                            break
                        except requests.exceptions.ConnectionError:
                            if _attempt < 2:
                                time.sleep(2)
                            else:
                                pass  # give up silently — coordinator will time out the job

                served = True
                break
            except Exception:
                pass  # poll error, skip

        if not served:
            time.sleep(2)

      except KeyboardInterrupt:
          raise
      except Exception as _loop_exc:
          _log_crash(_loop_exc)
          err(f"Unexpected error in work loop: {_loop_exc} — continuing in 5s")
          time.sleep(5)

# ---------------------------------------------------------------------------
# interactive wizard
# ---------------------------------------------------------------------------
def _context_paths():
    """Return durable per-user paths without requiring administrator access."""
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "CodeSwarm" / "SwarmLLM"
    appdata = Path(os.environ.get("APPDATA") or root)
    startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return {
        "root": root,
        "agent": root / "agent.py",
        "config": root / "agent-context.json",
        "launcher": root / "start-agent.cmd",
        "log": root / "agent.log",
        "startup": startup / "CodeSwarm LLM Agent.vbs",
    }


def _load_context(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_context(path, *, coordinator, api_key, model, port):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "coordinator": coordinator.rstrip("/"),
        "api_key": api_key,
        "model": model,
        "port": int(port),
    }, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(target)


def _install_windows_watchdog(config_path):
    """Install a hidden, restarting launcher for this standard Windows user."""
    if platform.system() != "Windows":
        return
    paths = _context_paths()
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["startup"].parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != paths["agent"].resolve():
        shutil.copy2(source, paths["agent"])
    child = subprocess.list2cmdline([
        sys.executable, str(paths["agent"]),
        "--config", str(config_path), "--supervised",
    ])
    paths["launcher"].write_text(
        "@echo off\r\n"
        "set SWARM_LLM_SUPERVISED=1\r\n"
        ":restart\r\n"
        f"{child} >> \"{paths['log']}\" 2>&1\r\n"
        "timeout /t 5 /nobreak >nul 2>&1\r\n"
        "goto restart\r\n",
        encoding="utf-8",
    )
    escaped_launcher = str(paths["launcher"]).replace('"', '""')
    paths["startup"].write_text(
        'Set shell = CreateObject("WScript.Shell")\r\n'
        f'shell.Run Chr(34) & "{escaped_launcher}" & Chr(34), 0, False\r\n',
        encoding="utf-8",
    )


def _launch_windows_watchdog():
    """Start the per-user watchdog invisibly and detach from this bootstrap."""
    if platform.system() != "Windows":
        return False
    startup = _context_paths()["startup"]
    subprocess.Popen(
        ["wscript.exe", "//B", str(startup)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=(subprocess.DETACHED_PROCESS |
                       subprocess.CREATE_NEW_PROCESS_GROUP |
                       subprocess.CREATE_NO_WINDOW),
    )
    return True


def run_wizard():
    show_banner()
    step("Detecting hardware...")
    hw = detect_hardware()

    print()
    print(_c(YELLOW, "  Detected hardware:"))
    print(f"    CPU  : {hw['cpu_name']}")
    print(f"    RAM  : {hw['ram_gb']} GB")
    if hw["vram_gb"] > 0:
        print(_c(GREEN, f"    GPU  : {hw['gpu_name']} ({hw['vram_gb']} GB VRAM)"))
    else:
        print(_c(YELLOW, "    GPU  : none detected  (CPU inference)"))
    print()

    rec_idx   = recommended_model_index(hw["vram_gb"])
    chosen_id = _MODEL_CATALOG[rec_idx]["id"]
    print(_c(GREEN,  f"  Default model  : {chosen_id}  (auto-selected for {hw['vram_gb']} GB VRAM)"))
    print(_c(GRAY,   "  Other models   : pulled on demand when a job requests them"))
    print()

    coord_url = input(_c(YELLOW, "  Coordinator URL (e.g. https://1.2.3.4:8443): ")).strip().rstrip("/")
    api_key   = getpass.getpass(_c(YELLOW, "  API Key: "))

    print()
    print("  Local Ollama port:")
    port_choices = [11434, 11435, 11436, 11437]
    for i, p in enumerate(port_choices):
        suffix = "  (default)" if i == 0 else ""
        print(f"    [{i+1}] {p}{suffix}")
    port_in = input("  Choice [1]: ").strip()
    try:
        port = port_choices[int(port_in) - 1] if port_in else 11434
    except (ValueError, IndexError):
        port = 11434

    return {"model": chosen_id, "coordinator": coord_url,
            "api_key": api_key, "port": port, "hw": hw}

# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main():
    global _coordinator, _api_key, _model, _agent_id, _hw
    global _ollama_proc, _ollama_port, _ollama_exe

    parser = argparse.ArgumentParser(description="swarm-llm agent")
    parser.add_argument("--coordinator",     default="")
    parser.add_argument("--api-key",         default="")
    parser.add_argument("--model",           default="",
                        help="Ollama model to serve. Omit to auto-select based on VRAM.")
    parser.add_argument("--port",            type=int, default=11434)
    parser.add_argument("--skip-model-pull", action="store_true")
    parser.add_argument("--hf-token",        default="",
                        help="HuggingFace token for gated models (FLUX, SD3). "
                             "Also read from HF_TOKEN env var.")
    default_context = _context_paths()["config"]
    parser.add_argument("--config", type=Path, default=default_context,
                        help="Persisted coordinator/model context.")
    parser.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    _agent_id = get_agent_id()

    # A downloaded script can be run again with no arguments. Reuse the
    # existing context and repair the watchdog instead of reopening the wizard.
    saved = _load_context(args.config)
    args.coordinator = args.coordinator or str(saved.get("coordinator") or "")
    args.api_key = args.api_key or str(saved.get("api_key") or "")
    args.model = args.model or str(saved.get("model") or "")
    if args.port == 11434 and saved.get("port") is not None:
        try:
            args.port = int(saved["port"])
        except (TypeError, ValueError):
            pass

    # Wizard only if coordinator/key are not supplied on CLI
    interactive = not (args.coordinator and args.api_key)
    if interactive:
        cfg              = run_wizard()
        args.coordinator = cfg["coordinator"]
        args.api_key     = cfg["api_key"]
        if not args.model:
            args.model   = cfg["model"]
        args.port        = cfg["port"]
        hw               = cfg["hw"]
    else:
        hw = detect_hardware()
        if not args.model:
            args.model = _MODEL_CATALOG[recommended_model_index(hw["vram_gb"])]["id"]
            step(f"Auto-selected model: {args.model}  (VRAM: {hw['vram_gb']} GB)")
        show_banner()
        print(_c(YELLOW, f"  Model: {args.model}  |  Coordinator: {args.coordinator}"))
        print()

    _save_context(
        args.config,
        coordinator=args.coordinator,
        api_key=args.api_key,
        model=args.model,
        port=args.port,
    )
    _install_windows_watchdog(args.config)
    # When coordinator+key are given explicitly the user wants to see output in
    # this terminal. Skip the hidden-launch so the window stays open. The
    # startup script written above will still restart the agent on next reboot.
    if not args.supervised and not (args.coordinator and args.api_key):
        if _launch_windows_watchdog():
            ok("Agent installed and started in the background. This window can close.")
            return

    _coordinator = args.coordinator
    _api_key     = args.api_key
    _model       = args.model
    _hw          = hw

    # 1. Verify coordinator
    if not test_coordinator(args.coordinator, args.api_key):
        sys.exit(1)

    # 2. Ensure Ollama
    ollama_exe = find_ollama()
    if not ollama_exe:
        ollama_exe = install_ollama()
    else:
        ok(f"Ollama found at: {ollama_exe}")

    # 3. Kill any existing Ollama on this port
    if platform.system() != "Windows":
        try:
            out = subprocess.check_output(
                ["lsof", "-ti", f"tcp:{args.port}"],
                stderr=subprocess.DEVNULL, text=True,
                encoding="utf-8", errors="replace"
            ).strip()
            if out:
                step(f"Stopping existing Ollama on port {args.port} ...")
                subprocess.run(["kill", "-9"] + out.split(), check=False)
                time.sleep(2)
        except Exception:
            pass
    else:
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="replace"
            )
            for line in out.splitlines():
                if f":{args.port}" in line and "LISTENING" in line:
                    pid = line.split()[-1]
                    subprocess.run(["taskkill", "/PID", pid, "/F"],
                                   check=False, capture_output=True)
            time.sleep(2)
        except Exception:
            pass

    # 4. Start Ollama server (skip if already running — avoids port conflict
    #    when a watchdog instance is already using this port)
    _ollama_port = args.port
    _ollama_exe  = ollama_exe
    if wait_ollama_ready(args.port, timeout=3):
        step(f"Ollama already running on port {args.port} — reusing it")
        _ollama_proc = None  # not managed by us; liveness check skipped
    else:
        write_status("STARTING", f"Ollama server on port {args.port}")
        parallel = ollama_parallel(hw["vram_gb"], args.model)
        env = dict(os.environ,
                   OLLAMA_HOST=f"0.0.0.0:{args.port}",
                   OLLAMA_FLASH_ATTENTION="1",
                   OLLAMA_KV_CACHE_TYPE="q8_0",
                   OLLAMA_NUM_PARALLEL=str(parallel))
        step(f"Parallel slots: {parallel} (VRAM: {hw['vram_gb']} GB, model: {args.model})")
        _ollama_log = Path.home() / ".swarm" / "ollama.log"
        _ollama_log.parent.mkdir(exist_ok=True)
        _ollama_log_fh = open(_ollama_log, "a", encoding="utf-8", errors="replace")
        popen_kwargs = dict(env=env, stdout=_ollama_log_fh, stderr=_ollama_log_fh)
        if platform.system() == "Windows":
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        step(f"Ollama log: {_ollama_log}")
        server_proc = subprocess.Popen([ollama_exe, "serve"], **popen_kwargs)
        _ollama_proc = server_proc
        step("Waiting for Ollama to be ready...")
        if not wait_ollama_ready(args.port, 60):
            # Read the last few lines of the log to show why it failed
            try:
                lines = _ollama_log.read_text(encoding="utf-8", errors="replace").splitlines()
                for ln in lines[-10:]:
                    err(f"  ollama: {ln}")
            except Exception:
                pass
            raise RuntimeError("Ollama did not start within 60 seconds")
        ok(f"Ollama up (PID {server_proc.pid})")

    # 5. Network speed test
    step("Measuring network speed (5 MB test)...")
    hw["network_mbps"] = measure_network_speed()
    if hw["network_mbps"]:
        ok(f"Network speed: {hw['network_mbps']} MB/s")
    else:
        step("Speed test failed  -  skipping")

    # 6. Pull default model
    if not args.skip_model_pull:
        speed_tag = f"{hw['network_mbps']} MB/s  |  " if hw["network_mbps"] else ""
        write_status("DOWNLOADING", f"{speed_tag}starting pull  -  {args.model}")
        pull_model(ollama_exe, args.model, args.port)

    # 7. Register
    write_status("REGISTERING", "")
    register(args.coordinator, args.api_key, args.port, args.model, hw)

    # 7b. Heartbeat thread — start immediately after registration so the
    # coordinator never marks us dead during slow setup steps (media loop
    # install, RPC thread start, etc.).  work_loop also starts one; the
    # duplicate is harmless since both are daemon threads.
    threading.Thread(
        target=_heartbeat_thread,
        args=(args.coordinator, args.api_key, args.port),
        daemon=True, name="heartbeat-early",
    ).start()

    # 8. Media loop (always on; deps installed lazily on first job; skipped if no GPU)
    hf_token = args.hf_token or os.environ.get("HF_TOKEN", "")
    if hw["vram_gb"] > 0:
        try:
            start_media_loop(args.coordinator, args.api_key, _agent_id, hw["vram_gb"], hf_token)
            ok(f"Media loop ready  ({hw['vram_gb']} GB VRAM)  -  deps install on first job")
        except Exception as _me:
            err(f"Media loop failed to start: {_me}  (continuing without media)")

    # 9. RPC background thread — polls coordinator for cluster assignments
    rpc_t = threading.Thread(
        target=_rpc_thread_func,
        args=(args.coordinator, args.api_key),
        daemon=True,
        name="rpc-poller",
    )
    rpc_t.start()
    ok("RPC poller started — will auto-join clusters when assigned")

    # 10. Work loop — restarts automatically on unexpected crash
    while True:
        try:
            work_loop(ollama_exe, args.coordinator, args.api_key, args.port, hw["vram_gb"])
            break  # clean exit (only on KeyboardInterrupt propagating through)
        except KeyboardInterrupt:
            step("Shutting down (Ctrl+C)...")
            break
        except Exception as e:
            _log_crash(e)
            err(f"Agent crashed: {e}")
            err("Restarting in 10 seconds...")
            if _ollama_proc and _ollama_proc.poll() is not None:
                err("Ollama also died — restarting Ollama first...")
                _restart_ollama_after_rpc()
            time.sleep(10)


if __name__ == "__main__":
    main()
