"""Write latest.json, the file installed copies of KeiroLake read to find and verify updates.

    python desktop/scripts/updater_manifest.py <version> <repo> <folder-with-installers-and-.sig-files>

Windows updates with the NSIS installer, Linux with the AppImage, macOS with the .app.tar.gz; each is checked against its .sig before install.
(.deb and .rpm copies are updated by the system package manager instead.)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def main(version: str, repo: str, folder: Path) -> int:
    base = f"https://github.com/{repo}/releases/download/v{version}"
    platforms = {}
    required = {"windows-x86_64": f"KeiroLake_{version}_x64-setup.exe", "linux-x86_64": f"KeiroLake_{version}_amd64.AppImage"}
    optional = {"darwin-aarch64": f"KeiroLake_{version}_aarch64.app.tar.gz", "darwin-x86_64": f"KeiroLake_{version}_x86_64.app.tar.gz"}
    for key, pattern in {**required, **optional}.items():
        file, sig = folder / pattern, folder / f"{pattern}.sig"
        if not (file.exists() and sig.exists()):
            if key in required:
                print(f"Missing {pattern} or its signature", file=sys.stderr)
                return 1
            print(f"Leaving out {key}: no {pattern}", file=sys.stderr)  # a Mac build that failed doesn't block the release
            continue
        platforms[key] = {"signature": sig.read_text(encoding="utf-8").strip(), "url": f"{base}/{pattern}"}
    notes = f"KeiroLake {version}. See https://github.com/{repo}/releases/tag/v{version}"
    manifest = {"version": version, "notes": notes, "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "platforms": platforms}
    (folder / "latest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"latest.json for {version}: {', '.join(platforms)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1].lstrip("v"), sys.argv[2], Path(sys.argv[3])))
