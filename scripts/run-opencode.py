#!/usr/bin/env python3
"""Launch OpenCode with the Swarm credential without embedding it in config."""
import os
from pathlib import Path
import shutil
import sys

try:
    from gen_opencode_config import load_base_url, load_consumer_key
except ModuleNotFoundError:
    from scripts.gen_opencode_config import load_base_url, load_consumer_key

ROOT = Path(__file__).resolve().parents[1]


def find_executable(root=ROOT):
    native = root / ".opencode-tools" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    executable = str(native) if native.is_file() else shutil.which("opencode")
    if not executable:
        return None
    if Path(executable).suffix.lower() in (".cmd", ".bat"):
        native = Path(executable).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
        if not native.is_file():
            raise SystemExit("OpenCode's native executable was not found beside its npm launcher")
        executable = str(native)
    return executable


def main():
    executable = find_executable(ROOT)
    if not executable:
        raise SystemExit("Run python scripts/setup-opencode.py --install first")
    env = dict(os.environ)
    env["SWARM_CONSUMER_KEY"] = load_consumer_key(ROOT, env)
    env["SWARM_BASE_URL"] = load_base_url(ROOT, env)
    if not env["SWARM_CONSUMER_KEY"]:
        raise SystemExit("Run python scripts/setup-opencode.py to configure the Swarm consumer key")
    env.setdefault("OPENCODE_CONFIG", str(ROOT / "opencode.json"))
    # OpenCode run resolves its default project from PWD, which subprocess
    # callers can inherit from a different directory than their actual cwd.
    env["PWD"] = os.getcwd()
    os.execve(executable, [executable, *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
