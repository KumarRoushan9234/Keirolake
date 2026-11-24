"""Release notes for GitHub: downloads, install steps, and this version's section of CHANGELOG.md.

    python desktop/scripts/release_notes.py 0.8.0 owner/repo [folder-with-files] > notes.md
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def changes(version: str) -> str:
    """The CHANGELOG section for `version`, or for "Unreleased" when it has none yet."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8") if (ROOT / "CHANGELOG.md").exists() else ""
    for heading in (re.escape(version), "Unreleased"):
        m = re.search(rf"^## \[?{heading}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return "Fixes and improvements."


def notes(version: str, repo: str, folder: Path | None = None) -> str:
    base = f"https://github.com/{repo}/releases/download/v{version}"
    rows = [
        ("Windows 10 / 11", f"KeiroLake_{version}_x64-setup.exe", "Run it. If SmartScreen warns, choose More info, then Run anyway."),
        ("Ubuntu / Debian", f"KeiroLake_{version}_amd64.deb", f"`sudo apt install ./KeiroLake_{version}_amd64.deb`"),
        ("Fedora / RHEL / openSUSE", f"KeiroLake-{version}-1.x86_64.rpm", f"`sudo dnf install ./KeiroLake-{version}-1.x86_64.rpm`"),
        ("Any Linux", f"KeiroLake_{version}_amd64.AppImage", "`chmod +x` it, then run it. Updates itself."),
        ("macOS, Apple Silicon (M1 and newer)", f"KeiroLake_{version}_aarch64.dmg", "Open it and drag KeiroLake to Applications. First launch: right-click → Open."),
        ("macOS, Intel", f"KeiroLake_{version}_x86_64.dmg", "Same as above."),
    ]
    present = (lambda f: True) if folder is None else (lambda f: (folder / f).exists())  # leave out a Mac build that failed
    table = "\n".join(f"| {system} | [{file}]({base}/{file}) | {how} |" for system, file, how in rows if present(file))
    return f"""## What's new

{changes(version)}

## Download

| System | File | Install |
|---|---|---|
{table}
| Server (Docker) | build from source | `KEIROLAKE_PASSWORD=... docker compose -f docker-compose.server.yml up -d --build` |

Already installed? The Windows app, the AppImage and the Mac app update themselves (Settings → Updates). Check a download with
`sha256sum -c SHA256SUMS`. Local AI runs through [Ollama](https://ollama.com/download).
"""


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    sys.stdout.write(notes(sys.argv[1].lstrip("v"), sys.argv[2], Path(sys.argv[3]) if len(sys.argv) == 4 else None))
