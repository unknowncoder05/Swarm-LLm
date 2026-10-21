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
import hmac
import hashlib
import ipaddress
import json
import os
import platform
import queue
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import quote as urlquote
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

def _configure_stdio():
    """Ollama uses Unicode spinners, including when Windows redirects output."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace", line_buffering=True)
            except (OSError, ValueError):
                pass


_configure_stdio()
try:
    _AGENT_REVISION = hashlib.sha256(Path(__file__).read_text(encoding="utf-8").encode()).hexdigest()
except OSError:
    _AGENT_REVISION = ""

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
_instance_lock_handle = None
_update_lock = threading.Lock()
_lan_probe_active = threading.Event()

# RPC cluster state (modified by main thread and rpc thread)
_rpc_active   = threading.Event()  # set while this machine is running an RPC role
_rpc_assigned = threading.Event()  # set the moment an assignment is read (before role starts)
_chat_claim = (None, None)  # atomic snapshot for heartbeat and safe update gating
_claim_requests = {}
_claim_request_lock = threading.Lock()
_media_job_id = None
_ollama_proc = None                # subprocess.Popen for local Ollama server
_ollama_port = 0                   # port Ollama is listening on
_ollama_exe  = ""                  # path to ollama binary
_rpc_proc = None
_rpc_log_path = None
_rpc_relay_listeners = []
_rpc_relay_bridges = set()
_rpc_relay_lock = threading.Lock()
_rpc_assignment_cache = None
_rpc_assignment_failure_since = None

# ---------------------------------------------------------------------------
# crash logging
# ---------------------------------------------------------------------------
def _redact_report(text):
    text = str(text)
    for secret in (_api_key, os.environ.get("HF_TOKEN", "")):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", text)
    return text


def _report_outbox():
    return _context_paths()["root"] / "reports"


def _flush_reports():
    """Retry durable reports; the coordinator deduplicates by report_id."""
    if not _coordinator or not _api_key:
        return
    try:
        paths = sorted(_report_outbox().glob("*.json"))
        for path in paths[:3]:
            payload = json.loads(path.read_text(encoding="utf-8"))
            response = requests.post(
                f"{_coordinator}/agent-status", json=payload,
                headers={"X-API-Key": _api_key}, timeout=5,
            )
            response.raise_for_status()
            path.unlink()
    except Exception:
        pass  # keep the report for the next status update/restart


def _report_failure(exc, traceback_text=""):
    model_dir = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
    probe = model_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    diagnostics = {"platform": platform.platform(), "port": _ollama_port,
                   "model_directory": str(model_dir), "ollama_executable": _ollama_exe,
                   "gpu_name": _hw.get("gpu_name"), "vram_gb": _hw.get("vram_gb"),
                   "agent_pid": os.getpid(), "parent_pid": os.getppid(),
                   "supervised": _is_supervised(),
                   "ollama_pid": _ollama_proc.pid if _ollama_proc else None,
                   "rpc_active": _rpc_active.is_set()}
    diagnostics["agent_revision"] = _AGENT_REVISION
    try:
        diagnostics["disk_free_gb"] = round(shutil.disk_usage(probe).free / 1024**3, 2)
    except OSError:
        pass
    logs = getattr(exc, "log_tail", "")
    ollama_log = Path.home() / ".swarm" / "ollama.log"
    try:
        # Read only the end, even if the server log has grown very large.
        with ollama_log.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 8192))
            logs += "\nOllama server log:\n" + stream.read().decode("utf-8", errors="replace")
    except OSError:
        pass
    if _rpc_log_path:
        try:
            with _rpc_log_path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 12000))
                logs += "\nRPC native log:\n" + stream.read().decode("utf-8", errors="replace")
        except OSError:
            pass
    payload = {"report_id": str(uuid.uuid4()), "phase": "ERROR",
               "detail": _redact_report(f"{type(exc).__name__}: {exc}")[:4096],
               "agent_id": _agent_id, "hostname": socket.gethostname(),
               "model": getattr(exc, "model", _model) or "?",
               "traceback": _redact_report(traceback_text)[-16384:],
               "log_tail": _redact_report(logs)[-16384:],
               "exit_code": getattr(exc, "exit_code", None),
               "diagnostics": {k: _redact_report(v) if isinstance(v, str) else v
                               for k, v in diagnostics.items()},
               "occurred_at": time.time()}
    try:
        outbox = _report_outbox()
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"{time.time_ns()}-{payload['report_id']}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)
        for old in sorted(outbox.glob("*.json"))[:-20]:
            old.unlink()
    except OSError:
        # Still attempt delivery when local storage is full or read-only.
        try:
            requests.post(f"{_coordinator}/agent-status", json=payload,
                          headers={"X-API-Key": _api_key}, timeout=5).raise_for_status()
        except Exception:
            pass
    _flush_reports()


def _log_crash(exc: BaseException) -> None:
    import traceback as _tb
    ts  = time.strftime("%Y-%m-%d %H:%M:%S")
    tb  = _redact_report("".join(_tb.format_exception(type(exc), exc, exc.__traceback__)))
    msg = f"\n{'='*60}\n{ts}  pid={os.getpid()}\n{tb}"
    try:
        crash_dir = Path.home() / ".swarm"
        crash_dir.mkdir(exist_ok=True)
        with open(crash_dir / "agent_crash.log", "a", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass
    print(msg, file=sys.stderr, flush=True)
    _report_failure(exc, tb)

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

def _is_supervised():
    return os.environ.get("SWARM_LLM_SUPERVISED") == "1" or "--supervised" in sys.argv[1:]


def _acquire_instance_lock(port):
    """Hold an OS lock for this user's Ollama port until this process exits."""
    global _instance_lock_handle
    root = _context_paths()["root"]
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / f"agent-{port}.lock").open("a+b")
    try:
        if platform.system() == "Windows":
            import msvcrt
            # Windows locks a byte range, so create the first byte once.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _instance_lock_handle = handle
    return True


def _release_instance_lock():
    global _instance_lock_handle
    if _instance_lock_handle:
        _instance_lock_handle.close()
        _instance_lock_handle = None


def _apply_update(new_code: str) -> None:
    """Write new_code to disk and re-exec this process with the same args.
    Never returns on success; raises on failure."""
    import tempfile as _tf
    current_file = Path(__file__).resolve()
    try:
        current_file.write_text(new_code, encoding="utf-8")
        target = current_file
    except Exception:
        if platform.system() == "Windows" and _is_supervised():
            raise RuntimeError("Cannot update the supervised agent's durable script")
        tmp = Path(_tf.mktemp(suffix="_swarm_agent.py"))
        tmp.write_text(new_code, encoding="utf-8")
        target = tmp
    print(_c("\033[33m", f"  [update] restarting as {target}..."))
    if platform.system() != "Windows":
        os.execv(sys.executable, [sys.executable, str(target)] + sys.argv[1:])
    else:
        if _is_supervised():
            # The watchdog owns the next child. Spawning here as well makes
            # two agents compete for the same Ollama server on every update.
            os._exit(75)
        _release_instance_lock()
        subprocess.Popen([sys.executable, str(target)] + sys.argv[1:])
        os._exit(0)

def _check_for_update(coordinator: str, headers: dict) -> None:
    """Download agent.py from coordinator; call _apply_update if it differs."""
    if _rpc_active.is_set() or _rpc_assigned.is_set() or _chat_claim[0] or _media_job_id or _lan_probe_active.is_set() or not _update_lock.acquire(blocking=False):
        return
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
        if _rpc_active.is_set() or _rpc_assigned.is_set() or _chat_claim[0] or _media_job_id or _lan_probe_active.is_set():
            return  # work may have started during the network fetch
        if hashlib.sha256(new_code.replace("\r\n", "\n").encode()).hexdigest() != _AGENT_REVISION:
            print(_c("\033[33m", "  [update] new agent.py detected — applying update"))
            _apply_update(new_code)
    except Exception as _ue:
        pass  # network blip — try again next cycle
    finally:
        _update_lock.release()

def write_status(phase, detail=""):
    _flush_reports()
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
        response = requests.get(f"http://127.0.0.1:{_ollama_port or 11434}/api/version", timeout=3)
        response.raise_for_status()
        info["ollama_version"] = response.json().get("version") or "?"
    except Exception:
        pass
    return info

# ---------------------------------------------------------------------------
# model catalog / VRAM helpers
# ---------------------------------------------------------------------------
_MODEL_VRAM = {
    "qwen3.8:27b-q4_K_M":  17.0,
    "qwen2.5-coder:1.5b":   1.2,
    "devstral-small-2:24b": 15.0,
    "qwen3.6:27b":          17.0,
    "qwen3:30b-a3b":        17.0,
    "qwen3:14b":             9.3,
    "gemma4:12b":            8.0,
    "qwen3:8b":              5.2,
}

_MODEL_CATALOG = [
    {"id": "qwen2.5-coder:1.5b",   "vram": 1.2 },
    {"id": "qwen3:8b",             "vram": 5.2 },
    {"id": "gemma4:12b",           "vram": 8.0 },
    {"id": "qwen3:14b",            "vram": 9.3 },
    {"id": "devstral-small-2:24b", "vram": 15.0},
    {"id": "qwen3.6:27b",          "vram": 17.0},
    {"id": "qwen3.8:27b-q4_K_M",  "vram": 17.0},
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
    # Use the same operating headroom as demand filtering. RPC model capacity
    # includes its native context/compute overhead as well as the model weights.
    candidates = [(item["vram"], index) for index, item in enumerate(_MODEL_CATALOG)
                  if (19.0 if item["id"] == "qwen3:30b-a3b" else
                      19.0 if item["id"] in ("qwen3.6:27b", "qwen3.8:27b-q4_K_M") else item["vram"] + 1.0) <= vram_gb]
    return max(candidates)[1] if candidates else 0

def ollama_parallel(vram_gb, model):
    if vram_gb <= 0:
        return 1
    model_v = {
        "devstral-small-2:24b": 15, "qwen3.6:27b": 17, "qwen3.8:27b-q4_K_M": 17, "qwen3:14b": 9,
        "gemma4:12b": 8, "qwen3:8b": 5,
    }.get(model, int(vram_gb * 0.65))
    free = vram_gb - model_v - 2
    return max(1, min(4, 1 + int(free // 4)))

# ---------------------------------------------------------------------------
# agent ID
# ---------------------------------------------------------------------------
def get_agent_id():
    # TEMP can change across interactive sessions and hidden startup. Identity
    # belongs with durable user context, with migration from existing installs.
    root = _context_paths()["root"]
    id_file = root / "agent-id.txt"
    for candidate in (id_file, Path(tempfile.gettempdir()) / "swarm-agent-id.txt"):
        try:
            value = str(uuid.UUID(candidate.read_text(encoding="utf-8").strip()))
        except (OSError, ValueError):
            continue
        if candidate == id_file:
            return value
        break
    else:
        value = str(uuid.uuid4())
    root.mkdir(parents=True, exist_ok=True)
    temporary = id_file.with_suffix(".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(id_file)
    return value

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
    output_tail = deque(maxlen=40)
    for line in proc.stdout:
        line = _redact_report(line.rstrip())
        output_tail.append(line[-1024:])
        print(line, flush=True)
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
        failure = RuntimeError(f"Failed to pull '{model}' (exit {proc.returncode}):\n"
                               + "\n".join(output_tail)[-8192:])
        failure.exit_code = proc.returncode
        failure.log_tail = "\n".join(output_tail)[-8192:]
        failure.model = model
        raise failure
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
_LLAMACPP_RELEASE = "b11612"
_LLAMACPP_CACHE = Path(tempfile.gettempdir()) / "swarm-llamacpp" / _LLAMACPP_RELEASE

def find_llamacpp():
    srv_name = "llama-server.exe"     if platform.system() == "Windows" else "llama-server"
    rpc_name = "ggml-rpc-server.exe" if platform.system() == "Windows" else "ggml-rpc-server"
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
    step(f"Fetching llama.cpp {_LLAMACPP_RELEASE} from GitHub...")
    write_status("RPC_PREPARING", f"installing CUDA RPC runtime {_LLAMACPP_RELEASE}")
    if platform.system() == "Windows":
        # Shared NATs share GitHub's unauthenticated API quota. These
        # pinned downloads need no release API request on the worker.
        assets = [{"name": name, "size": size, "browser_download_url":
                   f"https://github.com/ggml-org/llama.cpp/releases/download/{_LLAMACPP_RELEASE}/{name}"}
                  for name, size in [
                      (f"llama-{_LLAMACPP_RELEASE}-bin-win-cuda-12.4-x64.zip", 265262750),
                      ("cudart-llama-bin-win-cuda-12.4-x64.zip", 391443627)]]
    else:
        req = Request(
            f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{_LLAMACPP_RELEASE}",
            headers={"User-Agent": "swarm-llm-agent"},
        )
        try:
            with urlopen(req, timeout=20) as resp:
                release = json.loads(resp.read())
        except Exception as e:
            raise RuntimeError(f"GitHub API failed: {e}")
        assets = release.get("assets", [])
    if platform.system() == "Windows":
        asset = next((a for a in assets if a["name"] ==
                      f"llama-{_LLAMACPP_RELEASE}-bin-win-cuda-12.4-x64.zip"), None)
        runtime = next((a for a in assets if a["name"] ==
                        "cudart-llama-bin-win-cuda-12.4-x64.zip"), None)
        if not runtime:
            raise RuntimeError("CUDA runtime archive missing from pinned llama.cpp release")
    else:
        asset = next(
            (a for a in assets
             if re.search(r"(ubuntu|linux).*x64.*\.zip", a["name"], re.I)), None
        )
    if not asset:
        raise RuntimeError(f"No suitable llama.cpp asset for {platform.system()}")

    _LLAMACPP_CACHE.mkdir(parents=True, exist_ok=True)
    selected = [asset, runtime] if platform.system() == "Windows" else [asset]
    for archive in selected:
        step(f"Downloading {archive['name']} ({round(archive['size'] / 1024**2)} MB)...")
        write_status("RPC_PREPARING", f"downloading {archive['name']}")
        zip_path = _LLAMACPP_CACHE / archive["name"]
        digest = hashlib.sha256()
        try:
            with urlopen(archive["browser_download_url"], timeout=300) as resp, zip_path.open("wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
            expected = archive.get("digest")
            if expected and expected != f"sha256:{digest.hexdigest()}":
                raise RuntimeError(f"Checksum mismatch for {archive['name']}")
            with zipfile.ZipFile(str(zip_path)) as zf:
                for name in zf.namelist():
                    (_LLAMACPP_CACHE / name).resolve().relative_to(_LLAMACPP_CACHE.resolve())
                zf.extractall(str(_LLAMACPP_CACHE))
        except Exception as e:
            raise RuntimeError(f"Download/extraction failed for {archive['name']}: {e}") from e
        finally:
            zip_path.unlink(missing_ok=True)

    srv_name = "llama-server.exe"     if platform.system() == "Windows" else "llama-server"
    rpc_name = "ggml-rpc-server.exe" if platform.system() == "Windows" else "ggml-rpc-server"
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
    if not srv_path.exists() or not rpc_path.exists():
        raise RuntimeError("llama-server or ggml-rpc-server missing after extraction")
    if platform.system() == "Windows" and not (_LLAMACPP_CACHE / "cublas64_12.dll").exists():
        raise RuntimeError("CUDA DLLs missing after runtime extraction")
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
    if os.environ.get("OLLAMA_MODELS"):
        return Path(os.environ["OLLAMA_MODELS"])
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


# Ollama's Devstral blob embeds vision tensors that upstream llama.cpp's text
# loader rejects. Use an immutable, checksum-verified text GGUF for native RPC.
# The ordinary Ollama path continues to use the library package unchanged.
_NATIVE_MODEL_ARTIFACTS = {
    "devstral-small-2:24b": {
        "repository": "unsloth/Devstral-Small-2-24B-Instruct-2512-GGUF",
        "revision": "6e458b8add42681bfd023de5eab93637694aaf82",
        "filename": "Devstral-Small-2-24B-Instruct-2512-Q4_K_M.gguf",
        "size": 14334446752,
        "sha256": "d14ba9edee1bb4c4996a726deb81e49ae81800a3216f0774634238c380aee496",
    },
}

_NATIVE_CHAT_TEMPLATES = {
    "devstral-small-2:24b": {
        "repository": "mistralai/Devstral-Small-2-24B-Instruct-2512",
        "revision": "55c5b41e98c2dbd21b0c8afffc540dcfc9eb5128",
        "filename": "chat_template.jinja",
        "size": 11362,
        "sha256": "a0c0ed8a45c2c75c85fd756316a10419b90eb4f3dbe64d00f5c0b8ad4de5209a",
    },
}


def resolve_native_gguf(model):
    artifact = _NATIVE_MODEL_ARTIFACTS.get(model)
    if not artifact:
        return resolve_gguf(model)
    return _cached_native_artifact(artifact, model)


def _cached_native_artifact(artifact, model):
    cache = _context_paths()["root"] / "models"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (artifact["sha256"] + Path(artifact["filename"]).suffix)
    if target.exists() and target.stat().st_size == artifact["size"]:
        return str(target)  # Only a completely verified download is promoted here.
    partial = target.with_suffix(".partial")
    partial.unlink(missing_ok=True)
    if shutil.disk_usage(cache).free < artifact["size"] + 256 * 1024**2:
        raise RuntimeError("Not enough disk space for the native model: " + model)
    url = "https://huggingface.co/{repository}/resolve/{revision}/{filename}".format(**artifact)
    digest, downloaded, last_report = hashlib.sha256(), 0, 0.0
    write_status("RPC_PREPARING", "downloading native artifact " + model)
    try:
        with requests.get(url, stream=True, timeout=(15, 120)) as response, partial.open("wb") as output:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > artifact["size"]:
                    raise RuntimeError("Native model download exceeded its expected size")
                output.write(chunk)
                digest.update(chunk)
                if time.time() - last_report >= 5:
                    write_status("RPC_PREPARING", f"{downloaded * 100 // artifact['size']}% - native {model}")
                    last_report = time.time()
        if downloaded != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
            raise RuntimeError("Native model checksum/size mismatch: " + model)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return str(target)

# ---------------------------------------------------------------------------
# RPC cluster execution helpers
# ---------------------------------------------------------------------------
_RPC_RELAY_FRAME = 768 * 1024


class _RpcRelayBridge:
    """Carry a local RPC TCP connection through authenticated outbound HTTP."""
    def __init__(self, sock, coordinator, api_key, cluster_id, worker_id, connection_id, side, model):
        self.sock = sock
        self.base = (f"{coordinator.rstrip('/')}/agent/rpc/relay/"
                     f"{cluster_id}/{worker_id}/{connection_id}")
        self.side = side
        self.model = model
        self.headers = {"X-API-Key": api_key, "X-Swarm-Agent-ID": _agent_id}
        self.stopped = threading.Event()
        # Native RPC sends short commands and replies as well as large tensors.
        # Do not let Nagle buffering add a delayed-ACK wait to each local hop.
        if self.sock.family in (socket.AF_INET, socket.AF_INET6):
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(35)

    def request(self, session, method, suffix, **kwargs):
        for attempt in range(6):
            if self.stopped.is_set():
                return None
            try:
                response = session.request(method, self.base + suffix, headers=self.headers,
                                           timeout=(5, 35), **kwargs)
                if response.status_code == 410:
                    return None
                if response.status_code >= 500:
                    raise requests.exceptions.ConnectionError(f"Relay HTTP {response.status_code}")
                response.raise_for_status()
                return response
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt == 5:
                    raise
                self.stopped.wait(min(3, .25 * (attempt + 1)))
        return None

    def send(self):
        sequence = 0
        try:
            with requests.Session() as session:
                while not self.stopped.is_set():
                    try:
                        data = self.sock.recv(_RPC_RELAY_FRAME)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    chunks = [data]
                    size = len(data)
                    deadline = time.monotonic() + .005
                    # Batch bulk tensor data without delaying small RPC commands.
                    while size < _RPC_RELAY_FRAME and time.monotonic() < deadline:
                        if not select.select([self.sock], [], [], .001)[0]:
                            break
                        chunk = self.sock.recv(_RPC_RELAY_FRAME - size)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    if self.request(session, "POST", f"/{self.side}/send",
                                    params={"seq": sequence}, data=b"".join(chunks)) is None:
                        break
                    sequence += 1
        except Exception as exc:
            if not self.stopped.is_set():
                exc.model = self.model
                _log_crash(exc)
        finally:
            self.close()

    def run(self):
        with _rpc_relay_lock:
            _rpc_relay_bridges.add(self)
        sender = None
        try:
            with requests.Session() as session:
                if self.side == "primary" and self.request(session, "POST", "") is None:
                    return
                sender = threading.Thread(target=self.send, daemon=True, name="rpc-relay-send")
                sender.start()
                sequence = -1
                while not self.stopped.is_set():
                    response = self.request(session, "GET", f"/{self.side}/recv", params={"ack": sequence})
                    if response is None:
                        break
                    if response.status_code == 204:
                        continue
                    received = int(response.headers["X-Relay-Sequence"])
                    if received <= sequence:
                        continue
                    if received != sequence + 1:
                        raise RuntimeError("RPC relay frame order mismatch")
                    self.sock.sendall(response.content)
                    sequence = received
        except Exception as exc:
            if not self.stopped.is_set():
                exc.model = self.model
                _log_crash(exc)
        finally:
            self.close()
            with _rpc_relay_lock:
                _rpc_relay_bridges.discard(self)

    def close(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        try:
            requests.post(self.base + f"/{self.side}/close", headers=self.headers, timeout=5)
        except Exception:
            pass


def _start_rpc_relay_listener(assignment, worker, coordinator, api_key):
    listener = socket.socket()
    try:
        # The endpoint is private to this primary. Fixed coordinator hints may
        # fall in Windows excluded port ranges; let the OS choose and retain
        # the bound socket until teardown so another process cannot take it.
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(1)
    except BaseException:
        listener.close()
        raise
    _rpc_relay_listeners.append(listener)
    def accept():
        while _rpc_active.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            bridge = _RpcRelayBridge(client, assignment.get("relay_url") or coordinator, api_key, assignment["cluster_id"],
                worker["agent_id"], str(uuid.uuid4()), "primary", assignment["model"])
            threading.Thread(target=bridge.run, daemon=True, name="rpc-relay-primary").start()
    threading.Thread(target=accept, daemon=True, name="rpc-relay-listener").start()
    return f"127.0.0.1:{listener.getsockname()[1]}"


def _start_rpc_relay_worker(assignment, connection_id, coordinator, api_key):
    def connect():
        try:
            client = socket.create_connection(("127.0.0.1", assignment["rpc_port"]), timeout=10)
            bridge = _RpcRelayBridge(client, assignment.get("relay_url") or coordinator, api_key, assignment["cluster_id"],
                _agent_id, connection_id, "worker", assignment["model"])
            bridge.run()
        except Exception as exc:
            exc.model = assignment["model"]
            _log_crash(exc)
    threading.Thread(target=connect, daemon=True, name="rpc-relay-worker").start()


_rpc_quic_tunnels = []
_QUIC_MODULE_SHA256 = "cba1de540348721c8dfba8a4aad1cc9243662e525fbd978beadb30136b4a8fc9"


def _start_rpc_quic_tunnel(assignment, coordinator, api_key, native_port=None):
    """Install optional QUIC support privately; never modify system Python."""
    import importlib.util
    if sys.version_info < (3, 11):
        raise RuntimeError("LAN QUIC requires Python 3.11 or newer")
    root = _context_paths()["root"] / "quic-v1"
    deps = root / "deps-1.7.0"
    root.mkdir(parents=True, exist_ok=True)
    deps.mkdir(exist_ok=True)
    if str(deps) not in sys.path:
        sys.path.insert(0, str(deps))
    if not (deps / ".installed").exists():
        step("Preparing private QUIC runtime (no administrator access required)...")
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True,
                  "encoding": "utf-8", "errors": "replace", "timeout": 300}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        installed = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "--only-binary=:all:", "--upgrade", "--target", str(deps), "aioquic==1.7.0"], **kwargs)
        if installed.returncode:
            raise RuntimeError("Private QUIC dependency installation failed: " + _redact_report(installed.stdout[-1500:]))
        (deps / ".installed").write_text("aioquic==1.7.0", encoding="utf-8")
        importlib.invalidate_caches()
    helper = root / (_QUIC_MODULE_SHA256 + ".py")
    if not helper.exists() or hashlib.sha256(helper.read_bytes()).hexdigest() != _QUIC_MODULE_SHA256:
        base = coordinator.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        response = requests.get(base + "/agent/quic_transport.py", headers={"X-API-Key": api_key}, timeout=30)
        response.raise_for_status()
        data = response.content.replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != _QUIC_MODULE_SHA256:
            raise RuntimeError("QUIC transport module checksum mismatch")
        temporary = helper.with_suffix(".tmp")
        temporary.write_bytes(data); temporary.replace(helper)
    spec = importlib.util.spec_from_file_location("swarm_quic_" + _QUIC_MODULE_SHA256, helper)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    def control(payload):
        response = requests.post(coordinator.rstrip("/") + "/agent/rpc/quic/endpoint", json=payload,
            headers={"X-API-Key": api_key, "X-Swarm-Agent-ID": _agent_id}, timeout=5)
        response.raise_for_status()
        return response.json()
    tunnel = module.QuicTunnel(assignment, _agent_id, control, native_port=native_port)
    _rpc_quic_tunnels.append(tunnel)
    return tunnel.start()


def _check_rpc_quic():
    for tunnel in _rpc_quic_tunnels:
        tunnel.check()


def _stop_rpc_relays():
    for tunnel in _rpc_quic_tunnels:
        tunnel.close()
    _rpc_quic_tunnels.clear()
    for listener in _rpc_relay_listeners:
        listener.close()
    _rpc_relay_listeners.clear()
    with _rpc_relay_lock:
        bridges = list(_rpc_relay_bridges)
    for bridge in bridges:
        bridge.close()


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

def _wait_llama_ready(port, timeout=120.0, proc=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(2)
    return False

def _rpc_poll_assignment(coordinator, api_key):
    global _rpc_assignment_cache, _rpc_assignment_failure_since
    try:
        r = requests.get(
            f"{coordinator}/agent/rpc/assignment",
            params={"agent_id": _agent_id},
            headers={"X-API-Key": api_key},
            timeout=8,
        )
        if r.status_code == 200:
            assignment = r.json()
            if not isinstance(assignment, dict) or not assignment.get("cluster_id"):
                raise ValueError("Invalid RPC assignment response")
            _rpc_assignment_cache = assignment
            _rpc_assignment_failure_since = None
            return assignment
        if r.status_code == 204:
            _rpc_assignment_cache = None
            _rpc_assignment_failure_since = None
            return None
        r.raise_for_status()
        raise RuntimeError(f"Unexpected RPC assignment status {r.status_code}")
    except Exception as exc:
        if _rpc_active.is_set() and _rpc_assignment_cache:
            now = time.monotonic()
            if _rpc_assignment_failure_since is None:
                _rpc_assignment_failure_since = now
            if now - _rpc_assignment_failure_since < 120:
                return _rpc_assignment_cache
            raise RuntimeError("RPC assignment could not be confirmed for 120 seconds") from exc
    return None

def _pending_chat_models(coordinator):
    with _claim_request_lock:
        return [model for endpoint, model in _claim_requests if endpoint == coordinator]


def _post_agent_receipt(url, api_key, *, payload=None, body_json=None,
                        terminal_statuses=(200,), label="Work completion"):
    """Retain the physical lease until a terminal result receipt is confirmed.

    Network errors and transient server failures are ambiguous. Retrying the
    same claim is safe; starting new work or dropping its result is not.
    """
    headers = {"X-API-Key": api_key}
    kwargs = {"json": payload} if body_json is None else {
        "data": body_json.encode(), "headers": {**headers, "Content-Type": "application/json"}}
    kwargs.setdefault("headers", headers)
    attempts = 0
    while True:
        try:
            response = requests.post(url, timeout=30, **kwargs)
        except requests.RequestException as exc:
            failure = exc
        else:
            if response.status_code in terminal_statuses:
                return  # completed, cancelled, or superseded attempt
            if response.status_code >= 500 or response.status_code in (408, 429):
                failure = RuntimeError(f"{label} returned HTTP {response.status_code}")
            else:
                response.raise_for_status()
                raise RuntimeError(f"Unexpected result acknowledgment HTTP {response.status_code}")
        if attempts == 0:
            _log_crash(failure)
            err(f"{label} receipt is unconfirmed; retaining its slot and retrying")
        attempts += 1
        time.sleep(min(30, 2 ** min(attempts, 5)))


def _post_chat_result(coordinator, api_key, job_id, *, payload=None, body_json=None):
    _post_agent_receipt(f"{coordinator}/agent/jobs/{job_id}/result", api_key,
        payload=payload, body_json=body_json, terminal_statuses=(200, 404, 409), label=f"Result for {job_id}")


def _confirm_rpc_ready(coordinator, api_key, cluster_id, proc):
    """Retry readiness only while the native process and assignment remain valid."""
    started = time.monotonic()
    while True:
        if proc.poll() is not None:
            failure = RuntimeError(f"llama-server exited before readiness was acknowledged (exit {proc.returncode})")
            failure.exit_code = proc.returncode
            raise failure
        assignment = _rpc_poll_assignment(coordinator, api_key)
        if not assignment or assignment.get("cluster_id") != cluster_id or assignment.get("dissolve"):
            return False
        try:
            response = requests.post(f"{coordinator}/agent/rpc/ready",
                json={"agent_id": _agent_id, "cluster_id": cluster_id, "role": "primary"},
                headers={"X-API-Key": api_key}, timeout=8)
        except requests.RequestException:
            pass
        else:
            if response.status_code == 200:
                return True
            if response.status_code in (404, 409):
                return False
            if response.status_code < 500 and response.status_code not in (408, 429):
                response.raise_for_status()
                raise RuntimeError(f"Unexpected RPC readiness HTTP {response.status_code}")
        if time.monotonic() - started >= 120:
            raise RuntimeError("RPC readiness could not be confirmed for 120 seconds")
        time.sleep(2)


def _claim_chat_job(coordinator, api_key, model):
    """Recover an offer after a lost HTTP response without claiming it twice."""
    global _chat_claim
    key = (coordinator, model)
    with _claim_request_lock:
        request_id = _claim_requests.setdefault(key, str(uuid.uuid4()))
        response = requests.get(f"{coordinator}/agent/jobs/next",
            params={"model": model, "agent_id": _agent_id, "claim_request_id": request_id},
            headers={"X-API-Key": api_key}, timeout=10)
        if response.status_code == 204:
            _claim_requests.pop(key, None)
            return None
        response.raise_for_status()
        if response.status_code != 200:
            raise RuntimeError(f"Unexpected claim reply HTTP {response.status_code}")
        job = response.json()
        if not isinstance(job, dict) or not job.get("id") or not isinstance(job.get("body_json"), str):
            raise ValueError("Invalid chat claim reply")
        if job.get("cancelled"):
            _chat_claim = (job["id"], job.get("claim_token"))
            try:
                _post_chat_result(coordinator, api_key, job["id"], payload={
                    "error": "Claim cancelled before execution", "claim_token": job.get("claim_token")})
            finally:
                _chat_claim = (None, None)
            _claim_requests.pop(key, None)
            return None
        _claim_requests.pop(key, None)
        return job


class RpcStreamCancelled(RuntimeError):
    pass


class RpcEventPublisher:
    """Batch transport independently of decoding, with bounded backpressure."""
    def __init__(self, coordinator, api_key, job_id, token):
        self.url = coordinator.rstrip("/") + "/agent/jobs/" + job_id + "/events"
        self.headers = {"X-API-Key": api_key}
        self.token = token
        self.queue = queue.Queue(maxsize=8)
        self.error = None
        self.stopped = threading.Event()
        self.pending = []
        self.pending_bytes = 0
        self.last_flush = time.monotonic()
        self.sequence = 0
        self.thread = threading.Thread(target=self._run, daemon=True, name="rpc-events")
        self.thread.start()

    def _put(self, item):
        until = time.monotonic() + 20
        while True:
            if self.error:
                raise self.error
            try:
                self.queue.put(item, timeout=.1)
                return
            except queue.Full:
                if time.monotonic() > until:
                    raise RuntimeError("RPC stream backpressure timed out")

    def emit(self, chunk):
        if self.error:
            raise self.error
        size = len(json.dumps(chunk).encode())
        if size > 48 * 1024:
            raise RuntimeError("Native stream event exceeds its size limit")
        if self.pending_bytes + size > 48 * 1024 or len(self.pending) >= 32:
            self.flush()
        self.pending.append(chunk)
        self.pending_bytes += size
        if self.sequence == 0 or time.monotonic() - self.last_flush >= .1:
            self.flush()

    def flush(self):
        if self.pending:
            self._put({"agent_id": _agent_id, "claim_token": self.token,
                       "sequence": self.sequence, "chunks": self.pending})
            self.sequence += 1
            self.pending = []
            self.pending_bytes = 0
            self.last_flush = time.monotonic()

    def _run(self):
        try:
            with requests.Session() as session:
                while not self.stopped.is_set():
                    try:
                        item = self.queue.get(timeout=.2)
                    except queue.Empty:
                        continue
                    if item is None:
                        return
                    for attempt in range(3):
                        if self.stopped.is_set():
                            return
                        try:
                            response = session.post(self.url, json=item, headers=self.headers, timeout=15)
                            if response.status_code in (404, 409, 410):
                                raise RpcStreamCancelled("Streaming claim is no longer available")
                            response.raise_for_status()
                            if response.json().get("next_sequence") != item["sequence"] + 1:
                                raise RuntimeError("Invalid stream acknowledgement")
                            break
                        except requests.RequestException:
                            if attempt == 2:
                                raise
                            time.sleep(.2 * (attempt + 1))
        except Exception as exc:
            self.error = exc

    def close(self):
        self.flush()
        self._put(None)
        self.thread.join(timeout=50)
        if self.thread.is_alive():
            raise RuntimeError("Stream upload did not finish")
        if self.error:
            raise self.error

    def abort(self):
        self.stopped.set()
        self.thread.join(timeout=1)


def _read_rpc_stream(response, emit):
    message = {"role": "assistant", "content": ""}
    tools = {}
    result = {"choices": [{"index": 0, "message": message, "finish_reason": None}]}
    total_bytes = 0
    finished = False
    for raw in response.iter_lines(chunk_size=None):
        if not raw or raw.startswith(b":"):
            continue
        if not raw.startswith(b"data:"):
            raise RuntimeError("Invalid native SSE event")
        data = raw[5:].strip()
        if data == b"[DONE]":
            finished = True
            break
        total_bytes += len(data)
        if len(data) > 48 * 1024 or total_bytes > 16 * 1024 * 1024:
            raise RuntimeError("Native stream exceeds bounded response size")
        chunk = json.loads(data)
        if chunk.get("error"):
            raise RuntimeError("Native inference reported a stream error")
        emit(chunk)
        for name in ("id", "model", "created"):
            if name in chunk:
                result[name] = chunk[name]
        if chunk.get("usage"):
            result["usage"] = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("finish_reason"):
                result["choices"][0]["finish_reason"] = choice["finish_reason"]
            delta = choice.get("delta") or {}
            for name in ("content", "reasoning_content"):
                if delta.get(name):
                    message[name] = message.get(name, "") + delta[name]
            for call in delta.get("tool_calls") or []:
                index = call["index"]
                if type(index) is not int or not 0 <= index < 64:
                    raise RuntimeError("Invalid native tool stream index")
                target = tools.setdefault(index, {"type": "function", "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    target["id"] = call["id"]
                for name in ("name", "arguments"):
                    if call.get("function", {}).get(name):
                        target["function"][name] += call["function"][name]
    if not finished or not result["choices"][0]["finish_reason"]:
        raise RuntimeError("Native stream ended before completion")
    if tools:
        message["tool_calls"] = [tools[index] for index in sorted(tools)]
    return result


class NativeRequestError(RuntimeError):
    def __init__(self, status_code, detail):
        super().__init__(f"Native inference HTTP {status_code}: {detail}")
        self.status_code = status_code


def _check_native_response(response):
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = _redact_report(response.text)
        if len(detail) > 4096:
            detail = detail[:2048] + "\n...\n" + detail[-2048:]
        raise NativeRequestError(response.status_code, detail) from exc


def rpc_infer(job_body, llama_port, emit=None):
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
        "stream":   emit is not None,
    }
    # Model-author defaults; preserve explicit caller overrides.
    defaults = {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0} if job_body.get("model") == "qwen3:30b-a3b" else {}
    if job_body.get("model") == "qwen3.8:27b-q4_K_M":
        defaults = {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                    "presence_penalty": 0.0, "repeat_penalty": 1.0}
    for name in ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty"):
        value = opts.get(name, defaults.get(name))
        if value is not None:
            openai_body[name] = value
    predict = opts.get("num_predict", -1)
    if predict and predict != -1:
        openai_body["max_tokens"] = predict
    if job_body.get("tools"):
        openai_body["tools"] = job_body["tools"]
    if job_body.get("format"):
        openai_body["response_format"] = {"type": job_body["format"]}
    if emit is not None:
        openai_body["stream_options"] = {"include_usage": True}
        with requests.post(f"http://127.0.0.1:{llama_port}/v1/chat/completions",
                json=openai_body, stream=True, timeout=1800) as response:
            _check_native_response(response)
            return _read_rpc_stream(response, emit)
    r = requests.post(
        f"http://127.0.0.1:{llama_port}/v1/chat/completions",
        json=openai_body,
        timeout=1800,
    )
    _check_native_response(r)
    return r.json()

def _unload_ollama_models():
    """Release resident chat weights before another GPU workload starts."""
    response = requests.get(f"http://127.0.0.1:{_ollama_port}/api/ps", timeout=5)
    response.raise_for_status()
    for model in response.json().get("models", []):
        requests.post(f"http://127.0.0.1:{_ollama_port}/api/generate",
                      json={"model": model["name"], "keep_alive": 0}, timeout=30).raise_for_status()


def _stop_ollama_for_rpc():
    global _ollama_proc
    if _ollama_proc and _ollama_proc.poll() is None:
        step("Stopping Ollama to free GPU for RPC cluster...")
        _ollama_proc.terminate()
        try:
            _ollama_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _ollama_proc.kill()
    if _ollama_proc is None:
        # A reused server belongs to another launcher. Unload its models
        # through Ollama's API instead of terminating that other process.
        _unload_ollama_models()
    _ollama_proc = None


_rpc_log_start = 0
_rpc_offload_info = None


def _read_rpc_offload_info():
    if not _rpc_log_path:
        return None
    try:
        with _rpc_log_path.open("rb") as stream:
            stream.seek(_rpc_log_start)
            text = stream.read(256 * 1024).decode("utf-8", errors="replace")
        match = re.search(r"offloaded\s+(\d+)/(\d+)\s+layers", text)
        return tuple(map(int, match.groups())) if match else None
    except OSError:
        return None


def _launch_rpc_process(command, role):
    global _rpc_proc, _rpc_log_path, _rpc_log_start, _rpc_offload_info
    _rpc_log_path = Path.home() / ".swarm" / f"llama-{role}.log"
    _rpc_log_path.parent.mkdir(exist_ok=True)
    _rpc_log_start = _rpc_log_path.stat().st_size if _rpc_log_path.exists() else 0
    _rpc_offload_info = None
    with _rpc_log_path.open("a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} Launching {' '.join(command)}\n")
        log.flush()
        kwargs = {"stdout": log, "stderr": log,
                  "env": dict(os.environ, GGML_RPC_NO_RDMA="1")}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        _rpc_proc = subprocess.Popen(command, **kwargs)
    return _rpc_proc


def _stop_rpc_process():
    global _rpc_proc
    if _rpc_proc is not None and _rpc_proc.poll() is None:
        _rpc_proc.terminate()
        try:
            _rpc_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _rpc_proc.kill()
            _rpc_proc.wait(timeout=10)
    _rpc_proc = None

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
               OLLAMA_HOST=f"127.0.0.1:{_ollama_port}",
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

def _available_loopback_port():
    # Native subprocesses need a port number rather than an inherited socket.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RpcPreflightError(RuntimeError):
    pass


def _rpc_preflight_status(assignment, coordinator, api_key, stage):
    response = requests.post(coordinator.rstrip("/") + "/agent/rpc/preflight", json={
        "agent_id": _agent_id, "cluster_id": assignment["cluster_id"],
        "role": assignment["role"], "stage": stage}, headers={"X-API-Key": api_key}, timeout=8)
    response.raise_for_status()
    return response.json()


def _rpc_preflight_worker(assignment, coordinator, api_key):
    """Echo a per-cluster challenge through the real route before GPU startup."""
    listener = socket.socket()
    listener.settimeout(.5)
    listener.bind((assignment["listen_host"], assignment["rpc_port"]))
    listener.listen(4)
    verified = threading.Event()
    stopped = threading.Event()
    token = ("SWARM_ROUTE " + assignment["preflight_token"] + "\n").encode()
    def serve():
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                try:
                    client.settimeout(5)
                    data = b""
                    while len(data) < len(token):
                        chunk = client.recv(len(token) - len(data))
                        if not chunk:
                            break
                        data += chunk
                    if hmac.compare_digest(data, token):
                        client.sendall(token)
                        verified.set()
                except OSError:
                    pass
    thread = threading.Thread(target=serve, daemon=True, name="rpc-preflight-echo")
    thread.start()
    connections = set()
    try:
        if assignment.get("transport") == "quic":
            _start_rpc_quic_tunnel(assignment, coordinator, api_key, native_port=assignment["rpc_port"])
        _rpc_preflight_status(assignment, coordinator, api_key, "checking")
        deadline = time.monotonic() + 90
        while not verified.wait(.5):
            _check_rpc_quic()
            current = _rpc_poll_assignment(coordinator, api_key)
            if not current or current.get("dissolve") or current["cluster_id"] != assignment["cluster_id"]:
                raise RpcPreflightError("Route check cancelled by coordinator")
            if assignment.get("transport") == "relay":
                for identity in current.get("relay_connections", []):
                    if identity not in connections:
                        connections.add(identity)
                        _start_rpc_relay_worker(assignment, identity, coordinator, api_key)
            if time.monotonic() >= deadline:
                raise RpcPreflightError("No verified route to the primary within 90 seconds")
        _rpc_preflight_status(assignment, coordinator, api_key, "verified")
    finally:
        stopped.set()
        listener.close()
        thread.join(timeout=6)


def _rpc_preflight_primary(assignment, workers, coordinator, api_key):
    token = ("SWARM_ROUTE " + assignment["preflight_token"] + "\n").encode()
    _rpc_preflight_status(assignment, coordinator, api_key, "checking")
    try:
        for endpoint in workers:
            host, _, port = endpoint.rpartition(":")
            with socket.create_connection((host, int(port)), timeout=15) as client:
                client.settimeout(60)
                client.sendall(token)
                received = b""
                while len(received) < len(token):
                    data = client.recv(len(token) - len(received))
                    if not data:
                        break
                    received += data
                if not hmac.compare_digest(received, token):
                    raise RpcPreflightError("Route challenge did not match")
        _rpc_preflight_status(assignment, coordinator, api_key, "verified")
    except Exception as exc:
        raise RpcPreflightError("Selected route failed its end-to-end connection check") from exc
    # The temporary echo listener must be replaced by the actual RPC server.
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        state = _rpc_preflight_status(assignment, coordinator, api_key, "verified")
        if state.get("workers_ready"):
            return
        current = _rpc_poll_assignment(coordinator, api_key)
        if not current or current.get("dissolve") or current["cluster_id"] != assignment["cluster_id"]:
            raise RpcPreflightError("Route setup cancelled by coordinator")
        _check_rpc_quic()
        time.sleep(1)
    raise RpcPreflightError("Verified worker did not become ready within five minutes")


def _rpc_infer_with_health(body, port, assignment, coordinator, api_key, emit=None):
    """Let the role owner stop native work promptly after a transport failure."""
    finished = threading.Event()
    output = []
    def infer():
        try:
            output.append((True, rpc_infer(body, port, emit) if emit else rpc_infer(body, port)))
        except Exception as exc:
            output.append((False, exc))
        finally:
            finished.set()
    threading.Thread(target=infer, daemon=True, name="rpc-inference").start()
    while not finished.wait(2):
        _check_rpc_quic()
        current = _rpc_poll_assignment(coordinator, api_key)
        if not current or current.get("dissolve") or current["cluster_id"] != assignment["cluster_id"]:
            raise RuntimeError("RPC route was withdrawn; stopping native inference")
        if emit is not None and _chat_claim[0]:
            response = requests.get(coordinator.rstrip("/") + "/agent/jobs/" + _chat_claim[0] + "/cancelled",
                                    headers={"X-API-Key": api_key}, timeout=5)
            response.raise_for_status()
            if response.json().get("cancelled"):
                # Return to the role owner, which tears down the native process.
                raise RpcStreamCancelled("Consumer cancelled native streaming inference")
    success, result = output[0]
    if not success:
        raise result
    return result


def run_rpc_worker(assignment, coordinator, api_key):
    if assignment.get("transport") in ("relay", "quic"):
        # The bridge and native server share this process-local assignment.
        # No other machine connects directly to the worker's native port.
        assignment = dict(assignment, rpc_port=_available_loopback_port(), listen_host="127.0.0.1")
    cluster_id = assignment["cluster_id"]
    rpc_port   = assignment.get("rpc_port", 50052)
    if assignment.get("preflight_required"):
        write_status("RPC_CHECKING", "testing route before model startup")
        _rpc_preflight_worker(assignment, coordinator, api_key)
        # The echo connection can leave its port in TIME_WAIT on Windows.
        # Keep the network bridge, but point subsequent streams at a fresh native port.
        rpc_port = _available_loopback_port()
        assignment["rpc_port"] = rpc_port
        for tunnel in _rpc_quic_tunnels:
            if tunnel.assignment["cluster_id"] == cluster_id:
                tunnel.native_port = rpc_port
    bins = ensure_llamacpp()
    _stop_ollama_for_rpc()

    listen_host = assignment.get("listen_host") or get_local_ip()
    step(f"Starting ggml-rpc-server on {listen_host}:{rpc_port}...")
    proc = _launch_rpc_process([bins["llama_rpc_server"], "--host", listen_host,
                               "--port", str(rpc_port), "--cache"], "worker")

    if not _wait_tcp(listen_host, rpc_port, 30):
        proc.kill()
        raise RuntimeError(f"ggml-rpc-server did not listen on {listen_host}:{rpc_port}")
    ok(f"llama-rpc-server ready on :{rpc_port} (PID {proc.pid})")
    if assignment.get("transport") == "quic" and not assignment.get("preflight_required"):
        _start_rpc_quic_tunnel(assignment, coordinator, api_key, native_port=rpc_port)
    write_status("RPC_WORKER", f"cluster {cluster_id[:8]}  port {rpc_port}")
    if assignment.get("preflight_required"):
        _rpc_preflight_status(assignment, coordinator, api_key, "native_ready")

    headers = {"X-API-Key": api_key}
    relay_connections = set()
    while True:
        _check_rpc_quic()
        asgn = _rpc_poll_assignment(coordinator, api_key)
        if not asgn or asgn.get("dissolve") or asgn.get("cluster_id") != cluster_id:
            step(f"Dissolve signal — shutting down worker (cluster {cluster_id[:8]})")
            break
        if assignment.get("transport") == "relay":
            for connection_id in asgn.get("relay_connections", []):
                if connection_id not in relay_connections:
                    relay_connections.add(connection_id)
                    _start_rpc_relay_worker(assignment, connection_id, coordinator, api_key)
        if proc.poll() is not None:
            failure = RuntimeError(f"ggml-rpc-server exited unexpectedly (exit {proc.returncode})")
            failure.exit_code = proc.returncode
            raise failure
        time.sleep(5)

def _rpc_runtime_arguments(profile):
    defaults = {"batch_size": 128, "ubatch_size": 64, "cache_ram": 8192}
    if not isinstance(profile, dict) or set(profile) - set(defaults):
        raise ValueError("Invalid assigned RPC runtime profile")
    settings = dict(defaults, **profile)
    for name, choices in (("batch_size", (128, 256, 512)), ("ubatch_size", (64, 128, 256)),
                          ("cache_ram", (0, 1024, 4096, 8192))):
        if type(settings[name]) is not int or settings[name] not in choices:
            raise ValueError("Invalid assigned RPC runtime setting: " + name)
    if settings["ubatch_size"] > settings["batch_size"]:
        raise ValueError("Invalid assigned RPC batch sizes")
    return ["--batch-size", str(settings["batch_size"]), "--ubatch-size", str(settings["ubatch_size"]),
            "--cache-ram", str(settings["cache_ram"])]


def run_rpc_primary(assignment, coordinator, api_key):
    global _chat_claim, _rpc_offload_info
    cluster_id = assignment["cluster_id"]
    model      = assignment["model"]
    workers    = assignment.get("workers", [])
    ctx_size   = assignment.get("ctx_size", 16384)
    runtime_args = _rpc_runtime_arguments(assignment.get("runtime_profile", {}))

    if assignment.get("preflight_required"):
        write_status("RPC_CHECKING", "testing route before model startup")
        try:
            if assignment.get("transport") == "quic":
                workers = [_start_rpc_quic_tunnel(assignment, coordinator, api_key).endpoint]
            elif assignment.get("relay_workers"):
                workers = [_start_rpc_relay_listener(assignment, worker, coordinator, api_key)
                           for worker in assignment["relay_workers"]]
            _rpc_preflight_primary(assignment, workers, coordinator, api_key)
        except Exception as exc:
            raise RpcPreflightError("Route preflight failed before model startup") from exc

    gguf_path = resolve_native_gguf(model)
    if not gguf_path:
        write_status("RPC_PREPARING", f"pulling distributed model {model}")
        pull_model(_ollama_exe, model, _ollama_port)
        gguf_path = resolve_gguf(model)
        if not gguf_path:
            raise RuntimeError(f"GGUF for '{model}' unavailable after successful pull")

    bins = ensure_llamacpp()

    # Windows may reserve fixed ports even when no process is listening.
    # This HTTP endpoint is used only by this agent, so ask the OS for a port.
    llama_port = _available_loopback_port()

    _stop_ollama_for_rpc()
    if assignment.get("transport") == "quic" and not assignment.get("preflight_required"):
        workers = [_start_rpc_quic_tunnel(assignment, coordinator, api_key).endpoint]
    relay_workers = assignment.get("relay_workers", [])
    if relay_workers and not assignment.get("preflight_required"):
        workers = [_start_rpc_relay_listener(assignment, worker, coordinator, api_key)
                   for worker in relay_workers]

    cmd = [
        bins["llama_server"],
        "-m", gguf_path,
        "--alias", model,
        "--host", "127.0.0.1",
        "--port", str(llama_port),
        "--ctx-size", str(ctx_size),
        "-ngl", "auto", "--load-mode", "mmap",
        "--parallel", "1", "--flash-attn", "on",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
        "--fit", "on", "--fit-target", "512", "--jinja", "-lv", "4",
    ]
    cmd += runtime_args
    if model in _NATIVE_CHAT_TEMPLATES:
        # The embedded template rejects adjacent user messages sent by coding
        # clients. The author's updated template preserves their tool format.
        cmd += ["--chat-template-file", _cached_native_artifact(_NATIVE_CHAT_TEMPLATES[model], model)]
    if workers:
        cmd += ["--rpc", ",".join(workers)]
        step(f"Waiting for {len(workers)} worker RPC server(s) to come up...")
        for endpoint in workers:
            host, _, port_str = endpoint.rpartition(":")
            if not _wait_tcp(host or "127.0.0.1", int(port_str), timeout=120):
                raise RuntimeError(f"Worker {endpoint} unreachable after 120s; check the private LAN and Windows firewall")
            ok(f"Worker {endpoint} ready")
        step(f"Starting llama-server with RPC workers: {workers}")
    else:
        step("Starting llama-server (single-machine mode)")

    proc = _launch_rpc_process(cmd, "primary")

    write_status("RPC_PREPARING", f"loading {model}; first worker cache fill can take several minutes")
    step("Waiting for llama-server to be ready (model loading)...")
    load_timeout = assignment.get("load_timeout", 900)
    if assignment.get("transport") == "quic":
        load_timeout = max(load_timeout, 1800)  # first worker-cache fill can exceed 15 minutes
    if not _wait_llama_ready(llama_port, load_timeout, proc=proc):
        exit_code = proc.poll()
        failure = RuntimeError(f"llama-server failed to become ready (exit {exit_code})")
        failure.exit_code = exit_code
        raise failure
    ok(f"llama-server ready on :{llama_port} (PID {proc.pid})")
    _rpc_offload_info = _read_rpc_offload_info()
    write_status("RPC_PRIMARY", f"cluster {cluster_id[:8]}  model {model}")

    headers = {"X-API-Key": api_key}
    if not _confirm_rpc_ready(coordinator, api_key, cluster_id, proc):
        return  # the role owner tears down before acknowledging completion

    while True:
        _check_rpc_quic()
        asgn = _rpc_poll_assignment(coordinator, api_key)
        if not asgn or asgn.get("dissolve") or asgn.get("cluster_id") != cluster_id:
            step(f"Dissolve signal — shutting down primary (cluster {cluster_id[:8]})")
            break
        if proc.poll() is not None:
            failure = RuntimeError(f"llama-server exited unexpectedly (exit {proc.returncode})")
            failure.exit_code = proc.returncode
            raise failure

        try:
            job = _claim_chat_job(coordinator, api_key, model)
            if job is None:
                time.sleep(2)
                continue

            job_id = job["id"]
            claim_token = job.get("claim_token")
            _chat_claim = (job_id, claim_token)
            body   = json.loads(job["body_json"])
            ts = time.strftime("%H:%M:%S")
            print(_c("\033[36m", f"[{ts}] RPC job {job_id}  model={model}"))

            t0 = time.time()
            publisher = None
            try:
                if body.get("stream"):
                    publisher = RpcEventPublisher(coordinator, api_key, job_id, claim_token)
                emit = publisher.emit if publisher else None
                result = (_rpc_infer_with_health(body, llama_port, assignment, coordinator, api_key, emit)
                          if assignment.get("preflight_required") or emit else rpc_infer(body, llama_port))
                if publisher:
                    publisher.close()
                ms     = round((time.time() - t0) * 1000, 1)
                _post_chat_result(coordinator, api_key, job_id,
                    payload={"result": result, "elapsed_ms": ms, "claim_token": claim_token})
                ok(f"RPC job {job_id} done in {ms/1000:.1f}s")
            except Exception as e:
                err(f"RPC job {job_id} failed: {e}")
                status = getattr(e, "status_code", None)
                if status is not None:
                    e.model = model
                    _log_crash(e)
                    write_status("RPC_PRIMARY", f"native request returned HTTP {status}")
                try:
                    _post_chat_result(coordinator, api_key, job_id,
                        payload={"error": str(e), "claim_token": claim_token,
                                 **({"upstream_status": status} if status is not None else {})})
                except Exception:
                    pass
                if isinstance(e, RpcStreamCancelled):
                    return  # the role owner closes the native process before releasing its GPU
            finally:
                if publisher:
                    publisher.abort()
                _chat_claim = (None, None)
        except Exception:
            time.sleep(2)

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
                if assignment.get("preflight_required"):
                    try:
                        _rpc_preflight_status(assignment, coordinator, api_key,
                                              "failed" if isinstance(e, RpcPreflightError) else "runtime_failed")
                    except Exception:
                        pass
                e.model = assignment.get("model", _model)
                _log_crash(e)
                err(f"[rpc] Role execution error: {e}")
            finally:
                _stop_rpc_relays()
                _stop_rpc_process()
                try:
                    _post_agent_receipt(f"{coordinator}/agent/rpc/done", api_key,
                        payload={"agent_id": _agent_id, "cluster_id": assignment["cluster_id"], "role": role},
                        label=f"RPC teardown for {assignment['cluster_id']}")
                    ok(f"RPC {role} duty done (cluster {cid})")
                except Exception as exc:
                    _log_crash(exc)
                    err(f"RPC teardown acknowledgment failed: {exc}")
                _restart_ollama_after_rpc()
                _rpc_active.clear()
                _rpc_assigned.clear()
        time.sleep(5)

def register(coordinator, api_key, port, model, hw):
    info = get_system_info(hw)
    body = {
        "agent_id":       _agent_id,
        "agent_pid":      os.getpid(),
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
    cancelled   = False
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
        if not cancelled and job_id and coordinator and time.time() - last_cancel >= 5:
            last_cancel = time.time()
            try:
                cr = requests.get(
                    f"{coordinator}/agent/jobs/{job_id}/cancelled",
                    headers={"X-API-Key": api_key}, timeout=3
                )
                if cr.status_code == 200 and cr.json().get("cancelled"):
                    # The non-streaming HTTP thread still owns an Ollama
                    # generation. Do not acknowledge/free this GPU slot early.
                    cancelled = True
            except RuntimeError:
                raise
            except Exception:
                pass

        frame += 1
        time.sleep(FRAME_SEC)

    _clear()

    if cancelled:
        raise RuntimeError(f"Job {job_id} cancelled by coordinator")
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
    global _media_job_id
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
        if _rpc_assigned.is_set() or _rpc_active.is_set():
            time.sleep(2)
            continue
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
        _media_job_id = job_id
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
                _media_job_id = None
                _report("IDLE")
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
            _unload_ollama_models()
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
            _media_job_id = None
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
def _validate_lan_probe(plan, agent_id, now=None):
    now = time.time() if now is None else now
    if not isinstance(plan, dict) or str(uuid.UUID(plan["run_id"])) != plan["run_id"]:
        raise ValueError("Invalid probe identity")
    if plan.get("transport", "tcp") not in ("tcp", "udp"):
        raise ValueError("Unsupported probe transport")
    if (not re.fullmatch(r"[a-f0-9]{32}", plan.get("token", ""))
            or not 0 < plan["expires_at"] - plan["created_at"] <= 180
            or plan["created_at"] > now + 30 or not now < plan["expires_at"] <= now + 210):
        raise ValueError("Invalid or expired probe lifetime")
    nodes = plan.get("agents", [])
    if len(nodes) != 2 or len({n["agent_id"] for n in nodes}) != 2 or agent_id not in {n["agent_id"] for n in nodes}:
        raise ValueError("Probe requires exactly two distinct selected agents")
    for node in nodes:
        ip = ipaddress.ip_address(node["ip"])
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise ValueError("Probe targets must be private LAN IPv4 addresses")
    return next(n for n in nodes if n["agent_id"] == agent_id), next(n for n in nodes if n["agent_id"] != agent_id)


def _lan_probe_manifest(coordinator):
    base = coordinator.rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    response = requests.get(base + "/agent/lan-probe.json", timeout=5)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _lan_probe_status(result):
    path = _context_paths()["root"] / "lan-probe-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result), encoding="utf-8")
    temporary.replace(path)
    # Status is transient telemetry; expected blocked connections are not crashes.
    # Never replace the live model job's phase/detail with diagnostic output.
    if not (_rpc_active.is_set() or _rpc_assigned.is_set() or _chat_claim[0] or _media_job_id):
        write_status("RUNNING", "LAN_PROBE " + json.dumps(result, separators=(",", ":")))


def _lan_probe_connect(ip, port, token, timeout=3):
    started = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout) as client:
            client.settimeout(timeout)
            client.sendall(("SWARM_LAN_PROBE " + token + "\n").encode())
            with client.makefile("rb") as reader:
                reply = reader.readline(128)
            if reply != b"SWARM_LAN_OK\n":
                raise RuntimeError("Probe handshake mismatch")
        return {"passed": True, "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}
    except (OSError, RuntimeError) as exc:
        return {"passed": False, "error": type(exc).__name__ + ": " + str(exc)[:250],
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2)}


_LAN_UDP_PROBE_SECONDS = 15
_LAN_UDP_POLL_SECONDS = 2
_LAN_UDP_MAX_PINGS = 24
_LAN_UDP_MAX_ACKS = 32
_LAN_UDP_MAX_RECEIVED = 128


def _run_lan_udp_probe(plan, coordinator, agent_id):
    """Test simultaneous peer UDP traffic; this is not an inference transport."""
    own, peer = _validate_lan_probe(plan, agent_id)
    deadline = time.monotonic() + min(180, plan["expires_at"] - time.time())
    result = {"run_id": plan["run_id"], "agent_id": agent_id, "stage": "listening",
              "transport": "udp", "ip": own["ip"], "peer_id": peer["agent_id"],
              "sent_probes": 0, "received_probes": 0, "received_acks": 0,
              "sent_acks": 0, "ignored_datagrams": 0, "rtt_ms": []}
    sock = None
    endpoint = None
    probe_until = None
    next_poll = next_send = 0
    received = 0
    challenges = {}
    ping_prefix = ("SWARM_LAN_UDP_PING " + plan["token"] + " ").encode()
    ack_prefix = ("SWARM_LAN_UDP_ACK " + plan["token"] + " ").encode()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((own["ip"], 0))
        sock.settimeout(.1)
        result["port"] = sock.getsockname()[1]
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_poll:
                _lan_probe_status(result)
                current = _lan_probe_manifest(coordinator)
                if not current or current.get("run_id") != plan["run_id"]:
                    break
                _validate_lan_probe(current, agent_id)
                if any(current.get(k) != plan.get(k) for k in ("token", "agents", "transport", "created_at", "expires_at")):
                    raise ValueError("Probe identity changed during test")
                candidate = current.get("endpoints", {}).get(peer["agent_id"], {})
                if (endpoint is None and candidate.get("ip") == peer["ip"]
                        and type(candidate.get("port")) is int and 1 <= candidate["port"] <= 65535):
                    endpoint = (peer["ip"], candidate["port"])
                    probe_until = min(deadline, time.monotonic() + _LAN_UDP_PROBE_SECONDS)
                    result["stage"] = "testing"
                next_poll = time.monotonic() + _LAN_UDP_POLL_SECONDS
            now = time.monotonic()
            if probe_until is not None and now >= probe_until and result["stage"] != "complete":
                result.update(stage="complete", passed=result["received_acks"] > 0)
                if not result["passed"]:
                    result["error"] = "No matching UDP reply; direct UDP reachability was not established"
                _lan_probe_status(result)
            if (endpoint and result["stage"] == "testing" and now >= next_send
                    and result["sent_probes"] < _LAN_UDP_MAX_PINGS):
                nonce = uuid.uuid4().hex.encode()
                challenges[nonce] = now
                sock.sendto(ping_prefix + nonce + b"\n", endpoint)
                result["sent_probes"] += 1
                next_send = now + .5
            if received >= _LAN_UDP_MAX_RECEIVED:
                time.sleep(.1)
                continue
            try:
                packet, address = sock.recvfrom(129)
            except socket.timeout:
                continue
            except OSError as exc:
                # Windows may surface an ICMP rejection as a UDP receive error.
                result["last_receive_error"] = type(exc).__name__ + ": " + str(exc)[:150]
                time.sleep(.1)
                continue
            received += 1
            if address != endpoint or len(packet) > 128:
                result["ignored_datagrams"] += 1
                continue
            if packet.startswith(ping_prefix):
                nonce = packet[len(ping_prefix):]
                if re.fullmatch(rb"[a-f0-9]{32}\n", nonce) and result["sent_acks"] < _LAN_UDP_MAX_ACKS:
                    result["received_probes"] += 1
                    sock.sendto(ack_prefix + nonce, endpoint)
                    result["sent_acks"] += 1
                    continue
            elif packet.startswith(ack_prefix) and packet.endswith(b"\n"):
                sent_at = challenges.pop(packet[len(ack_prefix):-1], None)
                if sent_at is not None:
                    result["received_acks"] += 1
                    result["rtt_ms"].append(round((time.monotonic() - sent_at) * 1000, 2))
                    continue
            result["ignored_datagrams"] += 1
        if result["stage"] != "complete":
            result.update(stage="complete", passed=result["received_acks"] > 0)
            if not result["passed"]:
                result["error"] = ("No matching UDP reply before probe ended" if endpoint else
                                   "Peer endpoint was not available before probe ended")
    except Exception as exc:
        if result["stage"] != "complete":
            result.update(stage="complete", passed=False, error=type(exc).__name__ + ": " + str(exc)[:250])
    finally:
        if sock is not None:
            sock.close()
        _lan_probe_status(result)
    return result


def _run_lan_probe(plan, coordinator, agent_id):
    if plan.get("transport", "tcp") == "udp":
        return _run_lan_udp_probe(plan, coordinator, agent_id)
    own, peer = _validate_lan_probe(plan, agent_id)
    deadline = time.monotonic() + min(180, plan["expires_at"] - time.time())
    stopped = threading.Event()
    result = {"run_id": plan["run_id"], "agent_id": agent_id, "stage": "listening",
              "ip": own["ip"], "peer_id": peer["agent_id"]}
    listener = socket.socket()
    server = None
    try:
        listener.bind((own["ip"], 0))
        listener.listen(4)
        listener.settimeout(.2)
        result["port"] = listener.getsockname()[1]
        def accept():
            accepted = 0
            while not stopped.is_set() and time.monotonic() < deadline and accepted < 16:
                try:
                    client, address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                accepted += 1
                with client:
                    if address[0] != peer["ip"]:
                        continue
                    client.settimeout(1)
                    try:
                        with client.makefile("rb") as reader:
                            if reader.readline(128) == ("SWARM_LAN_PROBE " + plan["token"] + "\n").encode():
                                client.sendall(b"SWARM_LAN_OK\n")
                    except OSError:
                        pass
        server = threading.Thread(target=accept, daemon=True, name="lan-probe-listener")
        server.start()
        while time.monotonic() < deadline:
            _lan_probe_status(result)
            current = _lan_probe_manifest(coordinator)
            if not current or current.get("run_id") != plan["run_id"]:
                break
            _validate_lan_probe(current, agent_id)
            endpoint = current.get("endpoints", {}).get(peer["agent_id"], {})
            if (result["stage"] == "listening" and endpoint.get("ip") == peer["ip"]
                    and type(endpoint.get("port")) is int and 1 <= endpoint["port"] <= 65535):
                attempts = []
                for _ in range(3):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    attempts.append(_lan_probe_connect(peer["ip"], endpoint["port"], plan["token"], min(3, remaining)))
                    if attempts[-1]["passed"]:
                        break
                result.update(stage="complete", attempts=attempts, passed=any(a["passed"] for a in attempts))
                _lan_probe_status(result)
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        if result["stage"] != "complete":
            result.update(stage="complete", passed=False, error="Peer endpoint was not available before probe expiry")
    except Exception as exc:
        if result["stage"] != "complete":
            result.update(stage="complete", passed=False, error=type(exc).__name__ + ": " + str(exc)[:250])
    finally:
        stopped.set()
        listener.close()
        if server:
            server.join(timeout=2)
        _lan_probe_status(result)
    return result


def _lan_probe_loop(coordinator, agent_id):
    while True:
        try:
            if not (_rpc_active.is_set() or _rpc_assigned.is_set() or _chat_claim[0] or _media_job_id):
                plan = _lan_probe_manifest(coordinator)
                if plan:
                    _validate_lan_probe(plan, agent_id)
                    path = _context_paths()["root"] / "lan-probe-result.json"
                    previous = json.loads(path.read_text()) if path.exists() else {}
                    if previous.get("run_id") != plan["run_id"] and _update_lock.acquire(blocking=False):
                        try:
                            _lan_probe_active.set()
                        finally:
                            _update_lock.release()
                        try:
                            _run_lan_probe(plan, coordinator, agent_id)
                        finally:
                            _lan_probe_active.clear()
        except Exception:
            pass  # missing/expired/unselected manifest is normal; no model state changes
        time.sleep(30)


def _last_rpc_log():
    try:
        candidates = [_rpc_log_path] if _rpc_log_path else [
            Path.home() / ".swarm" / "llama-primary.log",
            Path.home() / ".swarm" / "llama-worker.log"]
        candidates = [path for path in candidates if path and path.exists()]
        if not candidates:
            return None
        path = max(candidates, key=lambda value: value.stat().st_mtime)
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 4096))
            tail = _redact_report(stream.read().decode("utf-8", errors="replace"))[-4096:]
        return {"role": "primary" if path.name == "llama-primary.log" else "worker",
                "modified_at": path.stat().st_mtime, "tail": tail,
                "process_running": _rpc_proc is not None and _rpc_proc.poll() is None}
    except OSError:
        return None


def _heartbeat_payload(port):
    active_job_id, active_claim_token = _chat_claim
    payload = {"port": port, "agent_id": _agent_id, "agent_pid": os.getpid(),
            "active_job_id": active_job_id or _media_job_id,
            "active_claim_token": active_claim_token,
            "agent_revision": _AGENT_REVISION,
            "rpc_relay_external": True,
            "rpc_quic": sys.version_info >= (3, 11),
            "rpc_preflight": True,
            "rpc_tuning": True,
            "rpc_stream": True,
            "rpc_gpu_layers": _rpc_offload_info[0] if _rpc_offload_info else None,
            "rpc_total_layers": _rpc_offload_info[1] if _rpc_offload_info else None,
            "supervised": _is_supervised(),
            "rpc_log": _last_rpc_log(),
            "ollama_pid": _ollama_proc.pid if _ollama_proc else None,
            "rpc_pid": _rpc_proc.pid if _rpc_proc else None,
            "ollama_ready": None if _rpc_active.is_set() or _rpc_assigned.is_set()
                            else wait_ollama_ready(port, timeout=2)}
    if payload["ollama_ready"] is True:
        try:
            response = requests.get(f"http://127.0.0.1:{port}/api/ps", timeout=2)
            response.raise_for_status()
            payload["ollama_models"] = [
                {field: model.get(field) for field in ("name", "size", "size_vram", "context_length")}
                for model in response.json().get("models", [])[:8]]
        except Exception:
            pass
    if _rpc_active.is_set():
        try:
            output = subprocess.check_output([
                "nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL, timeout=5)
            payload["gpu_used_mb"] = float(output.splitlines()[0].strip())
        except Exception:
            pass
    return payload


def _heartbeat_thread(coordinator, api_key, port):
    """Dedicated thread: sends heartbeats every 15s regardless of inference state.
    The main work_loop blocks during infer() (up to minutes), so heartbeats MUST
    live in their own thread or the coordinator marks the machine dead mid-job."""
    headers = {"X-API-Key": api_key}
    while True:
        try:
            r = requests.post(
                f"{coordinator}/heartbeat",
                json=_heartbeat_payload(port),
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
    global _model, _hw, _ollama_proc, _chat_claim
    headers    = {"X-API-Key": api_key}
    vram_limit = max(6.0, vram_gb - 1.0)

    # main() owns the heartbeat thread. Restarting this loop must not create
    # another heartbeat sender or parallel re-registration attempts.

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
            failure = RuntimeError(f"Ollama exited (code {rc})")
            failure.exit_code = rc
            _log_crash(failure)
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
                f"{coordinator}/agent/jobs/queued-models?agent_id={urlquote(_agent_id)}",
                headers=headers, timeout=8
            )
            r.raise_for_status()
            candidates = [m for m in r.json().get("models", [])
                          if model_vram(m) <= vram_limit]
        except Exception:
            pass

        # Discovery excludes a host with an outstanding offer. A lost claim
        # response must still be retried even when that endpoint returns [].
        candidates = list(dict.fromkeys([*_pending_chat_models(coordinator), *candidates]))
        served = False
        for model in candidates:
            if model not in pulled and time.time() - failed_pulls.get(model, 0) < 300:
                continue
            # Claim before downloading. The coordinator reserves this machine's
            # shared workload slot and only one agent can prepare a given job.
            try:
                job = _claim_chat_job(coordinator, api_key, model)
                if job is None:
                    continue
                job_id, job_body_json = job["id"], job["body_json"]
                claim_token = job.get("claim_token")
                _chat_claim = (job_id, claim_token)
            except Exception:
                continue

            # Pull on demand — skip models that recently failed (5-min cooldown)
            if model not in pulled:
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
                    _log_crash(e)
                    failed_pulls[model] = time.time()
                    _skip_logged.pop(model, None)  # log the failure immediately next iteration
                    err_msg = f"Pull failed for {model}: {e} — will retry in 5 min"
                    err(err_msg)
                    try:
                        _post_chat_result(coordinator, api_key, job_id,
                            payload={"error": str(e), "claim_token": claim_token})
                    except Exception:
                        pass
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
                    _chat_claim = (None, None)
                    continue

            # Run the job already reserved before model preparation.
            try:
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
                    post_body = f'{{"result":{raw_result},"elapsed_ms":{ms},"claim_token":{json.dumps(claim_token)}}}'
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
                    _post_chat_result(coordinator, api_key, job_id, body_json=post_body)
                    ok(f"Job {job_id} done in {ms/1000:.1f}s")
                else:
                    err(f"Job {job_id} inference failed: {_infer_exc}")
                    _post_chat_result(coordinator, api_key, job_id,
                        payload={"error": str(_infer_exc), "claim_token": claim_token})

                served = True
                break
            except Exception:
                pass  # poll error, skip
            finally:
                _chat_claim = (None, None)

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
    delay = subprocess.list2cmdline([sys.executable, "-c", "import time; time.sleep(5)"])
    paths["launcher"].write_text(
        "@echo off\r\n"
        "set SWARM_LLM_SUPERVISED=1\r\n"
        "set PYTHONIOENCODING=utf-8:backslashreplace\r\n"
        "set PYTHONUNBUFFERED=1\r\n"
        ":restart\r\n"
        f"{child} >> \"{paths['log']}\" 2>&1\r\n"
        f"{delay}\r\n"
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
    parser.add_argument("--foreground", action="store_true",
                        help="Run in this terminal instead of the Windows watchdog.")
    args = parser.parse_args()

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

    args.coordinator = args.coordinator.rstrip("/")
    # Preserve configured identity even when context/watchdog installation fails.
    _coordinator, _api_key, _model, _hw = args.coordinator, args.api_key, args.model, hw
    _ollama_port = args.port

    if not _acquire_instance_lock(args.port):
        step(f"An agent is already active on Ollama port {args.port}; keeping it running.")
        return

    _agent_id = get_agent_id()

    _save_context(
        args.config,
        coordinator=args.coordinator,
        api_key=args.api_key,
        model=args.model,
        port=args.port,
    )
    _install_windows_watchdog(args.config)
    # Every normal Windows install is supervised immediately, including the
    # dashboard command with explicit credentials. Foreground mode is opt-in.
    if platform.system() == "Windows" and not args.supervised and not args.foreground:
        _release_instance_lock()
        if _launch_windows_watchdog():
            ok("Agent installed and started in the background. This window can close.")
            return

    # 1. Verify coordinator
    if not test_coordinator(args.coordinator, args.api_key):
        raise RuntimeError("Coordinator connection or agent authentication failed")
    _flush_reports()
    # Startup failures must be able to update too, before reaching work_loop.
    _check_for_update(args.coordinator, {"X-API-Key": args.api_key})

    # 2. Ensure Ollama
    ollama_exe = find_ollama()
    if not ollama_exe:
        ollama_exe = install_ollama()
    else:
        ok(f"Ollama found at: {ollama_exe}")

    # 3–4. Reuse a healthy server. Starting another agent or updating must not
    # terminate a model download or inference running in the existing server.
    _ollama_port = args.port
    _ollama_exe  = ollama_exe
    if wait_ollama_ready(args.port, timeout=3):
        step(f"Ollama already running on port {args.port} — reusing it")
        _ollama_proc = None  # not managed by us; liveness check skipped
    else:
        write_status("STARTING", f"Ollama server on port {args.port}")
        parallel = ollama_parallel(hw["vram_gb"], args.model)
        env = dict(os.environ,
                   OLLAMA_HOST=f"127.0.0.1:{args.port}",
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
    # install, RPC thread start, etc.). This is the sole heartbeat owner.
    threading.Thread(
        target=_heartbeat_thread,
        args=(args.coordinator, args.api_key, args.port),
        daemon=True, name="heartbeat-early",
    ).start()

    # 8. Media loop (always on; deps installed lazily on first job; skipped if no GPU)
    threading.Thread(target=_lan_probe_loop, args=(args.coordinator, _agent_id),
                     daemon=True, name="lan-connectivity-probe").start()

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
    try:
        main()
    except Exception as exc:
        _log_crash(exc)
        sys.exit(1)
