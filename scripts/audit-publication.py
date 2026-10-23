#!/usr/bin/env python3
"""Scan publishable files without displaying credentials or identifying matches."""
import argparse
import ipaddress
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = (
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:api[_-]?key|secret|password|token)\s*[:=]\s*['\"][A-Za-z0-9+/=_-]{40,}['\"]", re.I),
)
PRIVATE_ARTIFACT = re.compile(
    r"(^|/)(?:\.env(?:\..*)?|\.aws|\.terraform|\.journal|\.rpc-trial)(?:/|$)"
    r"|\.(?:tfstate(?:\..*)?|tfplan|tfvars|pem|key|db|sqlite3?)$"
)
ADDRESS = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def findings(path, content, forbidden=()):
    """Return categories and line numbers; never include the matched value."""
    result = []
    if PRIVATE_ARTIFACT.search(path) and not path.endswith(".example"):
        result.append((0, "private-artifact"))
    for number, line in enumerate(content.splitlines(), 1):
        if any(pattern.search(line) for pattern in SECRET_PATTERNS):
            result.append((number, "credential-pattern"))
        if any(re.search(r"\b" + re.escape(token) + r"\b", line, re.I) if token.isalnum()
               else token.casefold() in line.casefold() for token in forbidden):
            result.append((number, "forbidden-identifier"))
        # Private test fixtures and network configuration are legitimate. Public
        # setup prose should use documentation addresses or operator placeholders.
        if path.startswith("docs/") or path in ("README.md", "SECURITY.md"):
            for raw in ADDRESS.findall(line):
                try:
                    address = ipaddress.ip_address(raw)
                except ValueError:
                    continue
                if any(address in ipaddress.ip_network(net) for net in
                       ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
                    result.append((number, "private-address-in-prose"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forbid", action="append", default=[],
                        help="Additional private name, domain, hostname or address to reject")
    args = parser.parse_args()
    if any(not token for token in args.forbid):
        parser.error("Forbidden identifiers must not be empty")
    paths = subprocess.check_output(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT
    ).decode().split("\0")
    count, issues = 0, []
    for name in sorted(set(paths) - {""}):
        path = ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        count += 1
        issues.extend((name, number, kind) for number, kind in findings(name, content, args.forbid))
    for name, number, kind in issues:
        print(f"{name}:{number}: {kind}")
    print(f"Checked {count} publishable text files; {len(issues)} findings. Git history and ignored files are excluded.")
    return bool(issues)


if __name__ == "__main__":
    raise SystemExit(main())
