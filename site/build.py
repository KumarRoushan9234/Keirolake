"""Assemble the GitHub Pages site from site/index.html plus the README's screenshots and demo video.

    python site/build.py [out-dir]     (default: _site)

Screenshots become WebP (much smaller than the PNGs), and the home screenshot becomes the social preview image.
Nothing is duplicated in the repository: the images stay in docs/.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ["home", "ask", "build-with-ai", "tables", "models", "capture-phone", "mobile-home"]


def main(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    assets = out / "assets"
    assets.mkdir(parents=True)
    shutil.copy2(ROOT / "site/index.html", out / "index.html")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    shutil.copy2(ROOT / "docs/media/demo.mp4", assets / "demo.mp4")
    shutil.copy2(ROOT / "frontend/public/icons/icon-192.png", assets / "icon-192.png")
    for name in SHOTS:
        img = Image.open(ROOT / f"docs/screenshots/{name}.png").convert("RGB")
        img.save(assets / f"{name}.webp", "WEBP", quality=82, method=6)
    home = Image.open(ROOT / "docs/screenshots/home.png").convert("RGB")
    w = home.width
    home.crop((0, 0, w, round(w * 630 / 1200))).resize((1200, 630), Image.LANCZOS).save(assets / "og.jpg", quality=85)
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Built {out} ({total / 2**20:.1f} MB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "_site")
