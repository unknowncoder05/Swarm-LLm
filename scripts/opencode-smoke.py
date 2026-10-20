#!/usr/bin/env python3
"""Exercise OpenCode read/edit/test tools against the distributed Swarm model."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TESTS = '''import unittest
from labels import normalize_labels

class LabelTests(unittest.TestCase):
    def test_first_spelling_and_order(self):
        self.assertEqual(normalize_labels([" Beta ", "ALPHA", "beta", "", "Alpha ", " gamma"]),
                         ["Beta", "ALPHA", "gamma"])
    def test_no_sorting(self):
        self.assertEqual(normalize_labels(["z", "a"]), ["z", "a"])
    def test_generator(self):
        self.assertEqual(normalize_labels(x for x in [" one", "ONE ", "two"]), ["one", "two"])
    def test_empty(self):
        self.assertEqual(normalize_labels(["", "  "]), [])
        self.assertEqual(normalize_labels([]), [])
'''
PROMPT = ("Fix normalize_labels in labels.py. It must trim whitespace, drop empty labels, "
          "deduplicate case-insensitively, and preserve the order and spelling of the first "
          "trimmed occurrence. Support generator inputs too. Read labels.py and test_labels.py "
          "using your tools, edit only labels.py, then run python3 -m unittest -q using bash. "
          "Do not change the tests. Finish with a short summary of the result.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen3:30b-a3b")
    parser.add_argument("--context", type=int, default=32768, choices=range(512, 32769), metavar="512..32768")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    executable = shutil.which("opencode")
    if not executable:
        parser.error("opencode must be installed")
    values = dict(os.environ)
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip("\"'"))
    if not values.get("CONSUMER_KEY"):
        parser.error("CONSUMER_KEY is required")
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = ROOT / ".rpc-trial" / (stamp + "-opencode")
    evidence_dir.mkdir(parents=True)
    trial = Path(tempfile.mkdtemp(prefix="swarm-opencode-"))
    workspace = trial / "workspace"
    workspace.mkdir()
    (workspace / "labels.py").write_text("def normalize_labels(labels):\n    return sorted(set(labels))\n")
    (workspace / "test_labels.py").write_text(TESTS)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    model = "swarm-llm/" + args.model
    spec = importlib.util.spec_from_file_location("swarm_opencode_config", ROOT / "scripts/gen_opencode_config.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    config = generator.build_config(args.model, context=args.context)
    config.update({
        "enabled_providers": ["swarm-llm"], "autoupdate": False, "share": "disabled", "snapshot": False,
        "tools": {"task": False, "webfetch": False, "websearch": False, "skill": False,
                  "lsp": False, "question": False, "todowrite": False},
        "permission": {"*": "deny", "read": "allow", "edit": "allow", "glob": "allow", "grep": "allow",
                       "bash": {"*": "deny", "python3 -m unittest*": "allow"}},
        "agent": {"build": {"steps": 6}}})
    # Exercise the checkout's optional title-only routing while keeping this
    # disposable fixture's independent tool and permission restrictions.
    profile_path = ROOT / "opencode.json"
    if profile_path.exists():
        title_model = json.loads(profile_path.read_text()).get("agent", {}).get("title", {}).get("model")
        if title_model and title_model.startswith("swarm-llm/"):
            config["agent"]["title"] = {"model": title_model}
    config_path = trial / "opencode.json"
    config_path.write_text(json.dumps(config, indent=2))
    child_env = dict(os.environ, SWARM_CONSUMER_KEY=values["CONSUMER_KEY"], OPENCODE_CONFIG=str(config_path),
        OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_DISABLE_LSP_DOWNLOAD="true",
        OPENCODE_DISABLE_DEFAULT_PLUGINS="true")
    child_env.pop("OPENCODE_CONFIG_CONTENT", None)
    child_env.pop("OPENCODE_CONFIG_DIR", None)
    child_env["PWD"] = str(workspace)
    for name in ("DATA", "CONFIG", "CACHE", "STATE"):
        child_env["XDG_" + name + "_HOME"] = str(trial / name.lower())
    result = {"model": args.model, "context": args.context, "started_at": datetime.now(timezone.utc).isoformat(),
              "workspace": str(workspace), "status": "running",
              "title_model": config.get("agent", {}).get("title", {}).get("model"),
              "version": subprocess.check_output([executable, "--version"], text=True).strip()}
    result_path = evidence_dir / "result.json"
    result_path.write_text(json.dumps(result, indent=2))
    print(json.dumps({"evidence": str(evidence_dir), "workspace": str(workspace), "model": model}), flush=True)
    started = time.monotonic()
    with (evidence_dir / "events.jsonl").open("w") as output, (evidence_dir / "stderr.log").open("w") as errors:
        process = subprocess.Popen([sys.executable, str(ROOT / "scripts/run-opencode.py"), "run", "--pure", "--format", "json", "--agent", "build",
            "--dir", str(workspace), "--model", model, PROMPT],
            cwd=workspace, env=child_env, stdout=output, stderr=errors)
        try:
            while process.poll() is None:
                if time.monotonic() - started > args.timeout:
                    result["timeout"] = True
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                time.sleep(5)
                elapsed = round(time.monotonic() - started, 1)
                if int(elapsed) % 30 < 5:
                    print(json.dumps({"phase": "running", "elapsed_seconds": elapsed}), flush=True)
        finally:
            process.wait()
    result["cli_returncode"] = process.returncode
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    validation = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=workspace,
                                capture_output=True, text=True, timeout=30)
    result["tests"] = {"returncode": validation.returncode, "output": validation.stdout + validation.stderr}
    result["tests_unchanged"] = (workspace / "test_labels.py").read_bytes() == TESTS.encode("utf-8")
    result["final_source"] = (workspace / "labels.py").read_text()
    events = []
    for line in (evidence_dir / "events.jsonl").read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    result["event_types"] = sorted({e.get("type", "unknown") for e in events})
    result["steps"] = [{"reason": e.get("part", {}).get("reason"), "tokens": e.get("part", {}).get("tokens")}
                       for e in events if e.get("type") == "step_finish"]
    if events:
        result["first_event_seconds"] = round(events[0]["timestamp"] / 1000 -
            datetime.fromisoformat(result["started_at"]).timestamp(), 3)
    result["tools"] = [{"tool": e.get("part", {}).get("tool"), "state": e.get("part", {}).get("state")}
                       for e in events if e.get("type") == "tool_use"]
    completed = {t["tool"] for t in result["tools"] if (t.get("state") or {}).get("status") == "completed"}
    result["opencode_tests_passed"] = any(t["tool"] == "bash" and
        (t.get("state") or {}).get("metadata", {}).get("exit") == 0 for t in result["tools"])
    result["status"] = "passed" if (process.returncode == 0 and validation.returncode == 0 and
        result["tests_unchanged"] and result["opencode_tests_passed"] and "read" in completed and "bash" in completed and
        bool(completed & {"edit", "write", "apply_patch"}) and "error" not in result["event_types"]) else "failed"
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result_path.write_text(json.dumps(result, indent=2))
    print(json.dumps({"status": result["status"], "elapsed_seconds": result["elapsed_seconds"],
                      "completed_tools": sorted(completed), "tests_returncode": validation.returncode,
                      "evidence": str(result_path)}), flush=True)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
