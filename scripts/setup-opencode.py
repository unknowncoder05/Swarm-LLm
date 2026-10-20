#!/usr/bin/env python3
"""Set up this checkout's OpenCode client for Swarm (no administrator needed)."""
import argparse
import getpass
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from gen_opencode_config import build_config, load_consumer_key, merge_config, update_config, write_private
except ModuleNotFoundError:
    from scripts.gen_opencode_config import build_config, load_consumer_key, merge_config, update_config, write_private

ROOT = Path(__file__).resolve().parents[1]
# Match the CLI used for the repository's integration checks.
OPENCODE_VERSION = "1.18.32"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the consumer key to another endpoint during setup.
        return None


def validate_base_url(base_url):
    parsed = urllib.parse.urlsplit(base_url)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != "https" and not
                (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")))):
        raise ValueError("Use an HTTPS API base URL (HTTP is allowed for loopback only)")
    return base_url.rstrip("/")


def check_connection(base_url, key):
    validate_base_url(base_url)
    request = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"X-API-Key": key})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            payload = json.load(response)
        models = payload["data"]
        if not isinstance(models, list) or any(not isinstance(m.get("id"), str) for m in models):
            raise ValueError("invalid model list")
        return list(dict.fromkeys(m["id"] for m in models))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("Swarm rejected the consumer credential (HTTP %s)" % exc.code) from None
        raise ValueError("Swarm model discovery failed (HTTP %s); check the API base URL" % exc.code) from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("Cannot read Swarm's model catalog; check connectivity and the API base URL") from None


def load_launcher():
    spec = importlib.util.spec_from_file_location("swarm_opencode_launcher", ROOT / "scripts/run-opencode.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def show_models(advertised):
    print("Models currently advertised by Swarm (may load on first request):")
    for index, name in enumerate(advertised, 1):
        print("  %s. %s" % (index, name))
    if not advertised:
        print("  None right now. Connect eligible machines or wait for capacity.")


def select_model(advertised, default):
    while True:
        choice = input("Default model: number or model ID [%s]: " % default).strip()
        if not choice:
            return default
        if choice.isdigit() and 1 <= int(choice) <= len(advertised):
            return advertised[int(choice) - 1]
        if choice in advertised:
            return choice
        print("Choose a listed model, or press Enter to keep the current selection.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Install pinned OpenCode locally using npm")
    parser.add_argument("--model", help="Select a model without the interactive picker")
    parser.add_argument("--title-model", help="Use a separate advertised Swarm model for session titles only")
    parser.add_argument("--context", type=int, choices=range(512, 32769), metavar="512..32768")
    parser.add_argument("--base-url", help="Swarm OpenAI-compatible URL, including /v1")
    parser.add_argument("--list-models", action="store_true", help="Show the live catalog without changing config or loading a model")
    parser.add_argument("--offline", action="store_true", help="Skip the authenticated API check")
    args = parser.parse_args(argv)
    path = ROOT / "opencode.json"
    if (ROOT / "opencode.jsonc").exists():
        parser.error("opencode.jsonc exists; consolidate it into opencode.json before running setup")
    # Validate existing configuration before installing or saving anything.
    try:
        from_config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        if not isinstance(from_config, dict):
            raise ValueError()
    except (OSError, ValueError):
        parser.error("Existing opencode.json must be valid JSON; it has been left untouched")
    try:
        saved_provider = from_config.get("provider", {}).get("swarm-llm", {})
        saved_url = saved_provider.get("options", {}).get("baseURL", "")
        base_url = args.base_url or os.environ.get("SWARM_BASE_URL")
        if not base_url and sys.stdin.isatty():
            base_url = input("Swarm API base URL%s: " % (" [%s]" % saved_url if saved_url else "")).strip() or saved_url
        base_url = validate_base_url(base_url or saved_url)
        key = load_consumer_key(ROOT)
    except (OSError, ValueError, AttributeError):
        parser.error("Provide --base-url https://your-host/swarm/api/v1 (or SWARM_BASE_URL) and valid client settings")
    prompted = False
    if not key and sys.stdin.isatty():
        key = getpass.getpass("Swarm consumer key (saved in ignored .env.opencode): ").strip()
        prompted = True
    if not key or any(c in key for c in "\r\n\x00"):
        parser.error("Set SWARM_CONSUMER_KEY or add it to .env.opencode, then retry")
    if args.offline and args.list_models:
        parser.error("--list-models requires an online catalog check")
    advertised = None
    if not args.offline:
        try:
            advertised = check_connection(base_url, key)
        except ValueError as exc:
            parser.error(str(exc))
        print("Authenticated Swarm API check passed.")
        show_models(advertised)
        if args.list_models:
            return 0
    else:
        print("Offline setup: API connectivity and credential were not verified.")
    previous_model = from_config.get("model", "")
    default = previous_model[len("swarm-llm/"):] if previous_model.startswith("swarm-llm/") else "qwen3:30b-a3b"
    model = args.model or (select_model(advertised, default)
                          if advertised and sys.stdin.isatty() else default)
    if args.title_model and advertised is not None and args.title_model not in advertised:
        parser.error("The requested title model is not currently advertised; no configuration was changed")
    if advertised is not None and model not in advertised:
        print("Selected model is not currently advertised; keeping your selection. Connect enough GPU capacity before use.")
    # Offline updates retain the last saved catalog instead of restoring a static list.
    names = advertised if advertised is not None else saved_provider.get("whitelist", list(saved_provider.get("models", {}))) or [model]
    if args.title_model:
        names = list(dict.fromkeys([*names, args.title_model]))
    window = args.context or saved_provider.get("models", {}).get("qwen3:30b-a3b", {}).get("limit", {}).get("context", 32768)
    config = build_config(model, base_url, window, advertised=names)
    if args.title_model:
        config["agent"] = {"title": {"model": "swarm-llm/" + args.title_model}}
    for name, profile in config["provider"]["swarm-llm"]["models"].items():
        if name != "qwen3:30b-a3b":
            existing = saved_provider.get("models", {}).get(name, {})
            if isinstance(existing, dict):
                config["provider"]["swarm-llm"]["models"][name] = merge_config(profile, existing)
    try:
        merge_config(from_config, config)
    except ValueError:
        parser.error("Existing OpenCode provider settings are invalid; config has been left untouched")
    if args.install:
        npm = shutil.which("npm")
        if not npm:
            parser.error("Install Node.js/npm in your user account, then retry --install")
        subprocess.run([npm, "install", "--prefix", str(ROOT / ".opencode-tools"),
                        "--no-audit", "--no-fund", "opencode-ai@" + OPENCODE_VERSION], check=True)
    launcher = load_launcher()
    executable = launcher.find_executable(ROOT)
    if not executable:
        parser.error("OpenCode is missing; retry with --install (requires Node.js/npm)")
    version = subprocess.run([executable, "--version"], check=True, capture_output=True, text=True, timeout=30)
    try:
        update_config(path, config)
        if prompted:
            write_private(ROOT / ".env.opencode", "SWARM_CONSUMER_KEY=" + json.dumps(key) + "\n")
    except (OSError, ValueError) as exc:
        parser.error("Cannot update OpenCode configuration (%s)" % type(exc).__name__)
    window = config["provider"]["swarm-llm"]["models"][model].get("limit", {}).get("context", "custom")
    print("OpenCode %s configured: swarm-llm/%s, client context %s." % (version.stdout.strip(), model, window))
    print("Other providers and project settings were preserved. No cloud resources were created.")
    print('Launch: %s"%s" "%s"' % ("& " if sys.platform == "win32" else "",
                                    sys.executable, ROOT / "scripts/run-opencode.py"))
    print("In OpenCode use /models to choose a Swarm model, then send a prompt. Swarm loads it on demand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
