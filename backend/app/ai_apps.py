"""Connect AI apps (Claude, ChatGPT, Cursor, VS Code...) to the lake through MCP.

Each app gets its own API key, so it can be limited and revoked on its own. Apps that start a local program (Claude
Desktop, Claude Code, Cursor, VS Code) use the desktop app's stdio bridge (`keirolake-desktop mcp`), which finds the
running engine by itself. Web apps (Claude.ai, ChatGPT) need the internet link and a connector link with the key in it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

from app import access

APPS = {
    "claude-desktop": "Claude Desktop",
    "claude-code": "Claude Code",
    "claude-web": "Claude.ai",
    "chatgpt": "ChatGPT",
    "cursor": "Cursor",
    "vscode": "VS Code",
    "other": "AI app",
}
DEFAULT_SCOPES = ["read", "ask", "write", "llm"]


class ConnectError(ValueError):
    pass


def bridge_exe() -> str:
    """The desktop app's executable, which runs the stdio bridge. Empty outside the desktop app."""
    exe = os.getenv("KEIROLAKE_APP_EXE", "")
    return exe if exe and Path(exe).exists() else ""


def claude_config_path() -> Path:
    """Claude Desktop's config file. The Microsoft Store version keeps it in its package folder."""
    home = Path.home()
    if sys.platform == "win32":
        packages = Path(os.getenv("LOCALAPPDATA", home / "AppData/Local")) / "Packages"
        for pkg in sorted(packages.glob("Claude_*")) if packages.exists() else []:
            store = pkg / "LocalCache/Roaming/Claude"
            if store.exists():
                return store / "claude_desktop_config.json"
        return Path(os.getenv("APPDATA", home / "AppData/Roaming")) / "Claude/claude_desktop_config.json"
    if sys.platform == "darwin":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    return Path(os.getenv("XDG_CONFIG_HOME", home / ".config")) / "Claude/claude_desktop_config.json"


def stdio_entry(raw_key: str) -> dict:
    return {"command": bridge_exe() or "<path to keirolake-desktop>", "args": ["mcp"], "env": {"KEIROLAKE_KEY": raw_key}}


def _quote(path: str) -> str:
    return f'"{path}"' if " " in path else path


def setups(raw_key: str, link_url: str) -> dict:
    """Ready-to-paste setup for every supported app."""
    entry = stdio_entry(raw_key)
    out = {
        "claude-desktop": json.dumps({"mcpServers": {"keirolake": entry}}, indent=2),
        "claude-code": f"claude mcp add keirolake --scope user -e KEIROLAKE_KEY={raw_key} -- {_quote(entry['command'])} mcp",
        "cursor": json.dumps({"mcpServers": {"keirolake": entry}}, indent=2),
        "vscode": json.dumps({"servers": {"keirolake": {"type": "stdio", **entry}}}, indent=2),
        "connector_url": f"{link_url.rstrip('/')}/mcp/k/{raw_key}" if link_url else "",
        "http_url": f"{link_url.rstrip('/')}/mcp" if link_url else "",
    }
    if link_url:
        out["remote"] = json.dumps({"mcpServers": {"keirolake": {"type": "http", "url": out["http_url"],
                                                                 "headers": {"Authorization": f"Bearer {raw_key}"}}}}, indent=2)
    return out


def create(app_id: str, scopes: list[str] | None, link_url: str) -> dict:
    if app_id not in APPS:
        raise ConnectError("Pick an app to connect")
    info, raw = access.create_key(APPS[app_id], scopes or DEFAULT_SCOPES)
    return {"key": info, "raw_key": raw, "setups": setups(raw, link_url), "bridge": bool(bridge_exe())}


def add_to_claude_desktop(raw_key: str) -> dict:
    """Add (or replace) the "keirolake" server in Claude Desktop's config, keeping everything else and a backup."""
    if not bridge_exe():
        raise ConnectError("Adding to Claude Desktop works from the KeiroLake desktop app")
    path = claude_config_path()
    config: dict = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as exc:
            raise ConnectError(f"Claude Desktop's config isn't valid JSON, so it was left alone: {path}") from exc
        if not isinstance(config, dict):
            raise ConnectError(f"Claude Desktop's config has an unexpected shape, so it was left alone: {path}")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"claude_desktop_config.backup-{stamp}.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ConnectError(f"Claude Desktop's config has an unexpected mcpServers entry, so it was left alone: {path}")
    servers["keirolake"] = stdio_entry(raw_key)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {"path": str(path), "restart": "Claude Desktop reads this only when it starts: quit it completely (also from the tray, or Task Manager) and open it again."}


def claude_desktop_status() -> dict:
    path = claude_config_path()
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers", {}) if path.exists() else {}
    except (ValueError, OSError, AttributeError):
        servers = {}
    return {"config": str(path), "installed": path.parent.exists(), "connected": isinstance(servers, dict) and "keirolake" in servers}
