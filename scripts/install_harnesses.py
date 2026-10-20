"""
Install swarm-goose and swarm-aider wrappers into ~/.local/bin/.
Run once from the swarm-llm repo; after that both commands work from any directory.
"""
import os, shutil, stat, subprocess, sys, textwrap

# ── read .env ─────────────────────────────────────────────────────────────────
env = {}
try:
    for line in open(".env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
except FileNotFoundError:
    print("ERROR: .env not found — run 'make init' first", file=sys.stderr)
    sys.exit(1)

key = env.get("CONSUMER_KEY")
if not key:
    print("ERROR: CONSUMER_KEY missing from .env", file=sys.stderr)
    sys.exit(1)

SWARM_URL = "https://sandbox.yerson.co/swarm/api/v1"

# ── write ~/.config/swarm-llm/config ─────────────────────────────────────────
cfg_dir = os.path.expanduser("~/.config/swarm-llm")
os.makedirs(cfg_dir, exist_ok=True)
cfg_path = os.path.join(cfg_dir, "config")
with open(cfg_path, "w") as f:
    f.write(f"SWARM_URL={SWARM_URL}\nSWARM_KEY={key}\n")
os.chmod(cfg_path, 0o600)

# ── shared model picker (embedded in each wrapper) ────────────────────────────
PICK_MODEL_FUNC = r"""
_pick_model() {
  python3 - "$SWARM_URL" "$SWARM_KEY" "$1" <<'PYEOF'
import sys, json, urllib.request

url, key, preselect = sys.argv[1] + "/models", sys.argv[2], sys.argv[3]

try:
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    ids = [m["id"] for m in json.loads(urllib.request.urlopen(req, timeout=4).read()).get("data", [])]
except Exception:
    ids = []

if not ids:
    print(preselect or "devstral:24b", end="")
    sys.exit(0)

if preselect:
    print(preselect, end="")
    sys.exit(0)

priority = ["devstral-small-2:24b", "devstral:24b", "ministral-3:8b", "qwen3:30b-a3b", "qwen3:14b", "qwen3:8b"]
recommended = next((p for p in priority if p in ids), ids[0])

if len(ids) == 1:
    print(ids[0], end="")
    sys.exit(0)

if not sys.stdin.isatty():
    print(recommended, end="")
    sys.exit(0)

print("\n  Available models:", file=sys.stderr)
for i, m in enumerate(ids, 1):
    tag = "  <- recommended" if m == recommended else ""
    print(f"    {i}. {m}{tag}", file=sys.stderr)
rec_idx = ids.index(recommended) + 1
print(f"\n  Select [1-{len(ids)}] (default {rec_idx}): ", end="", flush=True, file=sys.stderr)

try:
    choice = input().strip()
    idx = int(choice) - 1 if choice else rec_idx - 1
    idx = max(0, min(idx, len(ids) - 1))
except (ValueError, EOFError):
    idx = rec_idx - 1

print(ids[idx], end="")
PYEOF
}
"""

PARSE_MODEL_FLAG = r"""
EXPLICIT_MODEL=""
PASSTHROUGH=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -m|--model) EXPLICIT_MODEL="$2"; shift 2 ;;
    *) PASSTHROUGH+=("$1"); shift ;;
  esac
done
set -- "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"

MODEL=$(_pick_model "$EXPLICIT_MODEL")
echo "" >&2
echo "  model: $MODEL" >&2
echo "" >&2
"""

DEVSTRAL_SYSTEM = r"""
SYSTEM="You are Devstral, a helpful agentic model trained by Mistral AI. You can interact with a computer to solve tasks.

ROLE: Execute commands, modify code, and solve technical problems. Be thorough and methodical. If the user asks a question like 'why is X happening', just answer — don't try to fix it.

EFFICIENCY: Combine multiple actions into a single action wherever possible. Use find, grep, and git with appropriate filters. Do NOT write code in markdown blocks and wait — call your tools (write/shell) directly and immediately to complete the task.

FILE_SYSTEM: Do not assume paths are relative to cwd. Explore first if unsure. Edit files in place; don't create renamed copies.

CODE_QUALITY: Minimal changes. Clean code. Understand the codebase before changing it.

WORKFLOW: 1. Explore to understand context. 2. Execute immediately using tools — write files, run commands. 3. Verify by running tests or checking output. Complete the full task autonomously without stopping to ask for confirmation."
"""

# ── swarm-goose wrapper ───────────────────────────────────────────────────────
GOOSE_WRAPPER = f"""#!/usr/bin/env bash
# swarm-goose — launch an interactive Goose session against the swarm
# Works from any directory. Use -m MODEL to skip the model picker.
set -euo pipefail

source "${{HOME}}/.config/swarm-llm/config"
{PICK_MODEL_FUNC}
{PARSE_MODEL_FLAG}
# Mistral's official system prompt — devstral was fine-tuned on this exact text.
# Passing it via --system realigns the model with its agentic training.
{DEVSTRAL_SYSTEM}
# swarm-goose "do a task"   → one-shot (no TUI)
# swarm-goose               → interactive session (with model picker)
if [[ $# -gt 0 && "$1" != --* ]]; then
  TASK="$*"
  exec env \\
    OPENAI_BASE_URL="$SWARM_URL" \\
    OPENAI_API_KEY="$SWARM_KEY" \\
    GOOSE_PROVIDER=openai \\
    GOOSE_MODEL="$MODEL" \\
    goose run --text "$TASK"
else
  exec env \\
    OPENAI_BASE_URL="$SWARM_URL" \\
    OPENAI_API_KEY="$SWARM_KEY" \\
    GOOSE_PROVIDER=openai \\
    GOOSE_MODEL="$MODEL" \\
    goose session --system "$SYSTEM" "$@"
fi
"""

# ── swarm-aider wrapper ───────────────────────────────────────────────────────
AIDER_WRAPPER = f"""#!/usr/bin/env bash
# swarm-aider — launch aider against the swarm with the right flags
# Works from any directory. Use -m MODEL to skip the model picker.
# Uses diff (SEARCH/REPLACE) edit format — works reliably with devstral/qwen3.
set -euo pipefail

source "${{HOME}}/.config/swarm-llm/config"
{PICK_MODEL_FUNC}
{PARSE_MODEL_FLAG}
exec uvx --from aider-chat aider \\
  --openai-api-base "$SWARM_URL" \\
  --openai-api-key "$SWARM_KEY" \\
  --model "openai/$MODEL" \\
  --edit-format diff \\
  --yes \\
  --no-show-model-warnings \\
  "$@"
"""

# ── install wrappers ──────────────────────────────────────────────────────────
bin_dir = os.path.expanduser("~/.local/bin")
os.makedirs(bin_dir, exist_ok=True)

wrappers = {
    "swarm-goose": GOOSE_WRAPPER,
    "swarm-aider": AIDER_WRAPPER,
}

for name, content in wrappers.items():
    path = os.path.join(bin_dir, name)
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  wrote {path}")

# ── ensure ~/.local/bin is on PATH ────────────────────────────────────────────
path_env = os.environ.get("PATH", "")
if os.path.expanduser("~/.local/bin") not in path_env.split(":"):
    shell = os.environ.get("SHELL", "")
    rc = "~/.zshrc" if "zsh" in shell else "~/.bashrc"
    rc_path = os.path.expanduser(rc)
    with open(rc_path, "a") as f:
        f.write(f'\nexport PATH="$HOME/.local/bin:$PATH"  # added by swarm-llm\n')
    print(f"\n  PATH updated in {rc} — run: source {rc}")
else:
    print(f"\n  ~/.local/bin already on PATH")

# ── ensure goose is installed ─────────────────────────────────────────────────
if not shutil.which("goose"):
    print("\n  goose not found — installing...")
    subprocess.run(
        "curl -fsSL https://github.com/block/goose/releases/download/v1.51.0/"
        "goose-x86_64-unknown-linux-gnu.tar.gz | tar -xz -C /tmp/goose-dl && "
        "cp /tmp/goose-dl/goose ~/.local/bin/goose && chmod +x ~/.local/bin/goose",
        shell=True, check=True
    )
    print("  goose installed to ~/.local/bin/goose")
else:
    print(f"  goose already installed at {shutil.which('goose')}")

# ── ensure uvx is available (for aider) ──────────────────────────────────────
if not shutil.which("uvx"):
    print("\n  uvx not found — installing uv (required for swarm-aider)...")
    subprocess.run("curl -LsSf https://astral.sh/uv/install.sh | sh", shell=True, check=True)
    print("  uv installed")
else:
    print(f"  uvx already available at {shutil.which('uvx')}")

print(f"""
  Done. Commands available from any directory:

    swarm-goose              interactive Goose session — shows model picker
    swarm-goose -m MODEL     skip picker, use specific model
    swarm-goose "a task"     one-shot (no TUI)
    swarm-aider              aider with SEARCH/REPLACE edits — shows model picker
    swarm-aider -m MODEL     skip picker, use specific model
    swarm-aider file.py      open specific files in aider

  Config: ~/.config/swarm-llm/config  (API key lives here)
""")
