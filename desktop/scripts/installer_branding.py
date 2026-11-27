"""Brand the Windows installer: the side panel and header images, and the installer script's pages.

    python desktop/scripts/installer_branding.py

Writes desktop/src-tauri/windows/{sidebar.bmp, header.bmp, installer.nsi}. installer.nsi is Tauri's own NSIS template
(taken from the installed Tauri CLI, so it always matches the CLI that builds the app) with KeiroLake's welcome,
progress and finish pages added. Run it again after upgrading @tauri-apps/cli.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "desktop/src-tauri/windows"
LOGO = ROOT / "frontend/public/keirolabs.png"
SITE = "kumarroushan9234.github.io/Keirolake"
NAVY, NAVY_2, BLUE, WHITE, MUTED = (8, 13, 24), (19, 33, 61), (59, 123, 255), (232, 237, 245), (150, 164, 186)


def font(size: int, weight: str = "") -> ImageFont.FreeTypeFont:
    names = {"": ["segoeui.ttf", "DejaVuSans.ttf"], "b": ["segoeuib.ttf", "DejaVuSans-Bold.ttf"], "sb": ["seguisb.ttf", "DejaVuSans-Bold.ttf"]}[weight]
    for name in names:
        for folder in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
            if (folder / name).exists():
                return ImageFont.truetype(str(folder / name), size)
    return ImageFont.load_default(size)


def gradient(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), NAVY)
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        for x in range(w):
            glow = max(0.0, 1 - (((x - w * 0.3) / w) ** 2 + ((y - h * 0.22) / h) ** 2) * 3.2)
            px[x, y] = tuple(int(NAVY[i] + (NAVY_2[i] - NAVY[i]) * (1 - t) * 0.9 + (BLUE[i] - NAVY[i]) * glow * 0.18) for i in range(3))
    return img


def grid(img: Image.Image, step: int) -> None:
    d = ImageDraw.Draw(img, "RGBA")
    for x in range(0, img.width, step):
        d.line([(x, 0), (x, img.height)], fill=(255, 255, 255, 10))
    for y in range(0, img.height, step):
        d.line([(0, y), (img.width, y)], fill=(255, 255, 255, 10))


def sidebar() -> Image.Image:
    """The welcome and finish pages' panel: 164 x 314 at 2x, scaled to fit by NSIS."""
    s = 2
    img = gradient(164 * s, 314 * s)
    grid(img, 16 * s)
    d = ImageDraw.Draw(img)
    logo = Image.open(LOGO).convert("RGBA").resize((64 * s, 64 * s), Image.LANCZOS)
    img.paste(logo, (20 * s, 36 * s), logo)
    d.text((20 * s, 112 * s), "KeiroLake", font=font(22 * s, "b"), fill=WHITE)
    y = 144 * s
    for line in ("Your private data lake", "and AI gateway.", "", "Local models first.", "Your data stays yours."):
        d.text((20 * s, y), line, font=font(10 * s), fill=MUTED)
        y += 15 * s
    d.line([(20 * s, 262 * s), (144 * s, 262 * s)], fill=(42, 53, 80), width=s)
    small = Image.open(LOGO).convert("RGBA").resize((16 * s, 16 * s), Image.LANCZOS)
    img.paste(small, (20 * s, 274 * s), small)
    d.text((42 * s, 274 * s), "by KeiroLabs", font=font(10 * s, "sb"), fill=WHITE)
    d.text((20 * s, 293 * s), "Free and open source", font=font(8 * s), fill=MUTED)
    return img


def header() -> Image.Image:
    """The top-right image on the inner pages: 150 x 57 at 2x, on white like the page header."""
    s = 2
    img = Image.new("RGB", (150 * s, 57 * s), (255, 255, 255))
    d = ImageDraw.Draw(img)
    logo = Image.open(LOGO).convert("RGBA").resize((34 * s, 34 * s), Image.LANCZOS)
    img.paste(logo, (8 * s, 11 * s), logo)
    d.text((50 * s, 13 * s), "KeiroLake", font=font(15 * s, "b"), fill=(15, 23, 38))
    d.text((50 * s, 33 * s), "by KeiroLabs", font=font(9 * s), fill=(85, 96, 114))
    return img


def tauri_template() -> str:
    """Tauri's NSIS template, read from the installed CLI so it matches the CLI exactly."""
    mods = ROOT / "desktop/node_modules/@tauri-apps"
    for binary in sorted(mods.glob("cli-*/*.node")):
        data = binary.read_bytes()
        i = data.find(b"Unicode true\n")
        if i < 0:
            i = data.find(b"Unicode true\r\n")
        if i >= 0:
            return data[i:data.find(b"\x00", i)].decode("utf-8").replace("\r\n", "\n")
    raise SystemExit("Tauri CLI not found: run npm ci in desktop/ first")


WELCOME = r'''!define MUI_WELCOMEPAGE_TITLE "Welcome to KeiroLake"
!define MUI_WELCOMEPAGE_TEXT "KeiroLake keeps your tables, documents and offline libraries in one private lake on this computer, answered by local AI models first.$\r$\n$\r$\n  •  Ask questions and get answers with sources$\r$\n  •  Build datasets by chatting$\r$\n  •  Use it from your phone, Claude or ChatGPT$\r$\n  •  An OpenAI-compatible API and MCP for your apps$\r$\n$\r$\nMade by KeiroLabs. Free and open source (MIT).$\r$\n$\r$\nClick Next to continue."
'''
PROGRESS = r'''!define MUI_INSTFILESPAGE_PROGRESSBAR "smooth"
!define MUI_PAGE_HEADER_TEXT "Installing KeiroLake"
!define MUI_PAGE_HEADER_SUBTEXT "Setting up the engine, the vector database and the app. This takes about a minute."
!define MUI_INSTFILESPAGE_FINISHHEADER_TEXT "KeiroLake is installed"
!define MUI_INSTFILESPAGE_FINISHHEADER_SUBTEXT "Everything is in place."
'''
FINISH = rf'''!define MUI_FINISHPAGE_TITLE "KeiroLake is ready"
!define MUI_FINISHPAGE_TEXT "KeiroLake runs in the tray and starts with Windows, so your lake stays reachable. It keeps itself up to date.$\r$\n$\r$\nFirst steps: ask a question, build a table with AI, or connect Claude in Serve → AI apps.$\r$\n$\r$\nYour data stays in one folder on this computer."
!define MUI_FINISHPAGE_LINK "Guide, news and source: {SITE}"
!define MUI_FINISHPAGE_LINK_LOCATION "https://{SITE}/"
'''


def patch(template: str) -> str:
    def before(anchor: str, text: str, src: str) -> str:
        if src.count(anchor) != 1:
            raise SystemExit(f"The Tauri template changed; can't find: {anchor!r}")
        return src.replace(anchor, text + anchor, 1)

    out = before("!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive\n!insertmacro MUI_PAGE_WELCOME", WELCOME, template)
    out = before("!insertmacro MUI_PAGE_INSTFILES", PROGRESS, out)
    out = before("!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipIfPassive\n!insertmacro MUI_PAGE_FINISH", FINISH, out)
    return "; KeiroLake: Tauri's NSIS template plus branded pages. Made by desktop/scripts/installer_branding.py; don't edit.\n" + out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sidebar().save(OUT / "sidebar.bmp")
    header().save(OUT / "header.bmp")
    (OUT / "installer.nsi").write_text(patch(tauri_template()), encoding="utf-8", newline="\n")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
