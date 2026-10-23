#!/usr/bin/env python3
"""Review or apply the PeerFoundry public-proxy timeout needed for cold RPC loads."""
import argparse
from datetime import datetime, timezone
import difflib
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def update_timeout(source):
    blocks = list(re.finditer(r"location\s+\^~\s+/peerfoundry/\s*\{[^{}]*\}", source))
    if len(blocks) != 1:
        raise ValueError("Expected one flat location ^~ /peerfoundry/ block")
    block = blocks[0]
    updated, count = re.subn(r"(proxy_read_timeout\s+)(?:180|3660)s\s*;", r"\g<1>3660s;", block[0])
    if count != 1:
        raise ValueError("Expected exactly one known PeerFoundry timeout (180s or 3660s)")
    return source[:block.start()] + updated + source[block.end():]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True, help="Path to your existing nginx site configuration")
    parser.add_argument("--apply", action="store_true", help="Back up, install, validate, and reload; requires sudo")
    args = parser.parse_args()
    before = args.site.read_text()
    after = update_timeout(before)
    print("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                     fromfile=str(args.site), tofile=str(args.site))), end="", flush=True)
    if not args.apply or before == after:
        return
    os.umask(0o077)
    directory = ROOT / ".rpc-trial"
    directory.mkdir(exist_ok=True)
    backup = directory / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-nginx-before.conf")
    backup.write_text(before)
    def install(text):
        subprocess.run(["sudo", "-n", "tee", str(args.site)], input=text.encode(),
                       stdout=subprocess.DEVNULL, check=True)
    install(after)
    try:
        subprocess.run(["sudo", "-n", "nginx", "-t"], check=True)
        subprocess.run(["sudo", "-n", "nginx", "-s", "reload"], check=True)
    except BaseException:
        install(before)
        subprocess.run(["sudo", "-n", "nginx", "-t"], check=True)
        subprocess.run(["sudo", "-n", "nginx", "-s", "reload"], check=True)
        raise
    print(f"Applied PeerFoundry timeout 3660s; backup: {backup}")


if __name__ == "__main__":
    main()
