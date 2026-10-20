#!/usr/bin/env python3
"""Create private local settings without replacing an existing deployment."""
import argparse
import os
from pathlib import Path
import re
import secrets

ROOT = Path(__file__).resolve().parents[1]


def initialize(path):
    template = (ROOT / ".env.example").read_text()
    for name in ("ADMIN_KEY", "AGENT_KEY", "CONSUMER_KEY"):
        template = re.sub(r"^" + name + r"=.*$", name + "=" + secrets.token_hex(32), template, flags=re.M)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Local environment already exists; retained existing configuration.")
        return
    with os.fdopen(descriptor, "w") as stream:
        stream.write(template)
    print("Created private local environment with fresh API keys.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".env")
    initialize(parser.parse_args().output)
