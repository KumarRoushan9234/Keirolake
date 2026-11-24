"""Build and start the desktop app the right way.

    python desktop/scripts/launch.py            # release build: the web UI is embedded in the app
    python desktop/scripts/launch.py --dev      # development: hot-reloading UI from the Vite dev server

Why this exists: a plain `cargo build` (debug OR release) opens http://localhost:5173 instead of the embedded UI, so it shows
"localhost refused to connect" unless a dev server is running. The `custom-protocol` feature (which `tauri build` turns on for
you) is what embeds the UI.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = ROOT / "desktop" / "src-tauri"
NPM = "npm.cmd" if os.name == "nt" else "npm"


def run(cmd: list[str], cwd: Path) -> None:
    print("->", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> None:
    if "--dev" in sys.argv:
        run([NPM, "run", "dev"], ROOT / "desktop")  # tauri dev starts the Vite server itself
        return
    run([NPM, "run", "build"], ROOT / "frontend")
    run(["cargo", "build", "--release", "--features", "custom-protocol"], SHELL)
    exe = SHELL / "target" / "release" / ("keirolake-desktop.exe" if os.name == "nt" else "keirolake-desktop")
    env = dict(os.environ)
    env.setdefault("KEIROLAKE_DATA_DIR", str(ROOT / "data"))
    print("starting", exe)
    subprocess.Popen([str(exe)], env=env, cwd=ROOT)


if __name__ == "__main__":
    main()
