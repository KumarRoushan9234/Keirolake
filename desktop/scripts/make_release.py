"""Gather one version's installers into dist/release-<version>/ with checksums and release notes.

    python desktop/scripts/make_release.py 0.7.3

Then publish it from your own GitHub account (this script never uploads anything):

    gh release create v0.7.3 --repo <owner>/<repo> --title "KeiroLake 0.7.3" --notes-file dist/release-0.7.3/RELEASE_NOTES.md dist/release-0.7.3/*

or run desktop/scripts/publish_release.ps1 -Repo <owner>/<repo> -Version 0.7.3, which checks everything first.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(version: str) -> int:
    wanted = {
        "Windows installer": ROOT / "desktop/src-tauri/target/release/bundle/nsis" / f"KeiroLake_{version}_x64-setup.exe",
        "Debian / Ubuntu (.deb)": ROOT / "dist/linux" / f"KeiroLake_{version}_amd64.deb",
        "Fedora / RHEL / openSUSE (.rpm)": ROOT / "dist/linux" / f"KeiroLake-{version}-1.x86_64.rpm",
        "Any Linux (AppImage)": ROOT / "dist/linux" / f"KeiroLake_{version}_amd64.AppImage",
    }
    missing = [str(p) for p in wanted.values() if not p.exists()]
    if missing:
        print("Missing:\n  " + "\n  ".join(missing))
        return 1
    out = ROOT / "dist" / f"release-{version}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    sums, table = [], []
    for label, src in wanted.items():
        dst = out / src.name
        shutil.copy2(src, dst)
        digest = hashlib.sha256(dst.read_bytes()).hexdigest()
        sums.append(f"{digest}  {dst.name}")
        table.append(f"| {label} | `{dst.name}` | {dst.stat().st_size / 2**20:.0f} MB |")
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    notes = f"""# KeiroLake {version}

A private data lake that runs on your computer: tables, documents and offline libraries in one place, answered by local
AI models first, and reachable by your own apps (OpenAI-compatible gateway, tables API, MCP) and devices.

## Downloads

| System | File | Size |
|---|---|---|
{chr(10).join(table)}

Check a download with `sha256sum -c SHA256SUMS` (Linux) or `Get-FileHash <file> -Algorithm SHA256` (Windows).

## Install

- **Windows 10/11:** run the installer. Windows SmartScreen may warn because the installer isn't code-signed yet: choose *More info* then *Run anyway*.
- **Debian/Ubuntu:** `sudo apt install ./KeiroLake_{version}_amd64.deb`
- **Fedora/RHEL/openSUSE:** `sudo dnf install ./KeiroLake-{version}-1.x86_64.rpm`
- **Any Linux:** `chmod +x KeiroLake_{version}_amd64.AppImage` and run it.

Local AI models run through [Ollama](https://ollama.com/download) (install it separately). Web gathering uses a
KeiroLabs key; RenderData scrapers use a RenderData key. Everything else works offline.

## New in {version}

- Build with AI: describe a table or an offline library in a chat, agree the columns or sections, and the model fills it
  from your collections, offline libraries or its own knowledge (marked as low confidence), then asks what to add next.
- The internet link works with home Wi-Fi sharing on (it returned 502 before), comes back by itself after a restart, and
  can use your own Cloudflare tunnel for a fixed address such as lake.yourdomain.com.
- Stay reachable: keep the computer awake while the app runs, and optionally keep it on with the lid closed.

### Earlier in 0.7

- Home shows what is in the lake, with panels you can show, hide and reorder.
- Ask has a chat history grouped by day with search and rename, answers with collapsible sources, and options inside the composer.
- A fresh install starts with sample data: a World countries table (World Bank, CC BY 4.0) with a saved view, and a guide you can ask about.
- Data tools: RenderData scrapers (live catalog, paste-a-link, scheduled runs as a time series) and ten free open-data sources.
- OpenAI-compatible LLM gateway, tables API with saved views and search by meaning, more MCP tools.
- Sharing: home Wi-Fi, an internet link, or a port you open yourself with HTTPS, an allow-list and optional router forwarding.
- Heretic runs from the app, and a run can be converted and imported into Ollama.
- The window can no longer be scrolled off-screen, the title bar follows the app's theme instead of the Windows accent colour, and light is the default theme.
"""
    (out / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8")
    print(f"Ready: {out}")
    for line in sums:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "0.9.0"))
