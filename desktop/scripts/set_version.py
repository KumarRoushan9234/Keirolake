"""Set KeiroLake's version everywhere it is written down.

    python desktop/scripts/set_version.py 0.8.0      # set it
    python desktop/scripts/set_version.py            # print the current version
    python desktop/scripts/set_version.py --next     # print the version the next automatic release will use

The release workflow uses --next on every push to main: the version in the repository, or one patch above the newest
release tag when that version was already released. Bump minor or major versions yourself with this script.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
FILES = {  # file: (pattern, replacement); group 1 is kept, the version follows it
    "backend/app/support.py": r'(APP_VERSION = os\.getenv\("KEIROLAKE_APP_VERSION", ")[^"]+',
    "backend/pyproject.toml": r'(?m)(^version = ")[^"]+',
    "desktop/src-tauri/Cargo.toml": r'(?m)(^version = ")[^"]+',
    "desktop/scripts/make_release.py": r'(else ")\d+\.\d+\.\d+',
}
JSON_FILES = ["desktop/src-tauri/tauri.conf.json", "desktop/package.json", "frontend/package.json"]


def current() -> str:
    return json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["version"]


def parts(v: str) -> tuple[int, int, int]:
    a, b, c = (int(x) for x in v.split("."))
    return a, b, c


def newest_tag() -> str | None:
    out = subprocess.run(["git", "tag", "--list", "v*"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    tags = [t[1:] for t in out if SEMVER.match(t[1:])]
    return max(tags, key=parts) if tags else None


def next_version() -> str:
    here, tag = current(), newest_tag()
    if not tag or parts(here) > parts(tag):
        return here
    a, b, c = parts(tag)
    return f"{a}.{b}.{c + 1}"


def set_version(v: str) -> None:
    if not SEMVER.match(v):
        raise SystemExit(f"Not a version: {v} (use MAJOR.MINOR.PATCH, like 0.8.0)")
    for rel, pattern in FILES.items():
        p = ROOT / rel
        text, n = re.subn(pattern, lambda m: m.group(1) + v, p.read_text(encoding="utf-8"), count=1)
        if not n:
            raise SystemExit(f"Version not found in {rel}")
        p.write_text(text, encoding="utf-8")
    for rel in JSON_FILES:
        p = ROOT / rel
        data = json.loads(p.read_text(encoding="utf-8"))
        data["version"] = v
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Version set to {v}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if not arg:
        print(current())
    elif arg == "--next":
        print(next_version())
    else:
        set_version(arg.lstrip("v"))
