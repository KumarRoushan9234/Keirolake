"""Build the two programs the desktop app ships next to itself.

  binaries/keirolake-engine-<target>   the Python engine, frozen with PyInstaller
  binaries/qdrant-<target>             Qdrant's official release binary

Run from anywhere: python desktop/scripts/build_sidecars.py [--skip-engine] [--skip-qdrant]
Needs: the backend virtualenv (or any Python with `pip install -e backend[desktop]`) and internet for Qdrant.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

QDRANT_VERSION = "v1.20.0"
ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
BIN = ROOT / "desktop" / "src-tauri" / "binaries"
QDRANT_ASSETS = {
    "x86_64-pc-windows-msvc": "qdrant-x86_64-pc-windows-msvc.zip",
    # The static musl build: the glibc one needs glibc 2.38+, which Ubuntu 22.04 and Debian 12 do not have.
    "x86_64-unknown-linux-gnu": "qdrant-x86_64-unknown-linux-musl.tar.gz",
    "aarch64-unknown-linux-musl": "qdrant-aarch64-unknown-linux-musl.tar.gz",
    "x86_64-apple-darwin": "qdrant-x86_64-apple-darwin.tar.gz",
    "aarch64-apple-darwin": "qdrant-aarch64-apple-darwin.tar.gz",
}


def target_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    return next(line.split(": ", 1)[1] for line in out.splitlines() if line.startswith("host: "))


def web_ui_args() -> list[str]:
    """Bundle the built web UI so the engine can serve it to a browser tab (build the frontend first)."""
    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("Build the web UI first: cd frontend && npm run build")
    return ["--add-data", f"{dist}{';' if sys.platform == 'win32' else ':'}web"]


def build_engine(triple: str) -> Path:
    ext = ".exe" if "windows" in triple else ""
    work = ROOT / "desktop" / "build" / "pyinstaller"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
        "--name", "keirolake-engine", "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
        "--specpath", str(work), "--paths", str(BACKEND),
        "--collect-all", "libzim", "--collect-all", "zstandard", "--collect-submodules", "cryptography", "--collect-data", "trafilatura", "--collect-data", "justext", "--collect-data", "pdfminer", "--collect-all", "pypdfium2", "--collect-all", "pypdfium2_raw",
        "--collect-all", "faster_whisper", "--collect-all", "ctranslate2", "--collect-all", "onnxruntime", "--collect-binaries", "av", "--collect-submodules", "av",
        "--collect-submodules", "app", "--collect-data", "app", "--collect-submodules", "qdrant_client", "--collect-submodules", "uvicorn",
        *web_ui_args(),
        "--hidden-import", "keyring.backends.Windows", "--hidden-import", "keyring.backends.SecretService",
        "--exclude-module", "tkinter", "--exclude-module", "matplotlib",
        str(BACKEND / "engine_main.py"),
    ]
    subprocess.run(cmd, check=True, cwd=BACKEND)
    built = work / "dist" / f"keirolake-engine{ext}"
    target = BIN / f"keirolake-engine-{triple}{ext}"
    shutil.copy2(built, target)
    return target


def fetch_qdrant(triple: str) -> Path:
    asset = QDRANT_ASSETS.get(triple)
    if asset is None:
        raise SystemExit(f"No Qdrant release binary for {triple}")
    url = f"https://github.com/qdrant/qdrant/releases/download/{QDRANT_VERSION}/{asset}"
    print(f"Downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as resp:  # noqa: S310 - pinned https URL
        data = resp.read()
    print(f"sha256 {hashlib.sha256(data).hexdigest()}  {asset}")
    ext = ".exe" if "windows" in triple else ""
    target = BIN / f"qdrant-{triple}{ext}"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(n for n in zf.namelist() if n.endswith(f"qdrant{ext}"))
            target.write_bytes(zf.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("qdrant"))
            target.write_bytes(tf.extractfile(member).read())
    target.chmod(0o755)
    return target


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-engine", action="store_true")
    parser.add_argument("--skip-qdrant", action="store_true")
    args = parser.parse_args()
    BIN.mkdir(parents=True, exist_ok=True)
    triple = target_triple()
    if not args.skip_qdrant:
        print("qdrant ->", fetch_qdrant(triple))
    if not args.skip_engine:
        print("engine ->", build_engine(triple))


if __name__ == "__main__":
    main()
