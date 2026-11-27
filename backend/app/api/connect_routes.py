"""Connect AI apps through MCP, the in-app API and MCP reference, and the log viewer."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import access, ai_apps, tunnel
from app.config import get_settings
from app.support import _tail, redact

router = APIRouter(prefix="/api/connect")
LOGS = ("engine", "qdrant", "ollama", "llamacpp")


def _link_url() -> str:
    t = tunnel.status()
    return t["url"] if t["running"] else ""


@router.get("")
async def overview() -> dict:
    return {"apps": ai_apps.APPS, "bridge": bool(ai_apps.bridge_exe()), "link": _link_url(),
            "claude_desktop": ai_apps.claude_desktop_status(), "scopes": access.SCOPES}


class ConnectIn(BaseModel):
    app: str
    scopes: list[str] = Field(default_factory=lambda: list(ai_apps.DEFAULT_SCOPES))
    install: bool = False  # also write Claude Desktop's config


@router.post("", status_code=201)
async def connect(body: ConnectIn) -> dict:
    try:
        out = ai_apps.create(body.app, body.scopes, _link_url())
        if body.install and body.app == "claude-desktop":
            out["installed"] = ai_apps.add_to_claude_desktop(out["raw_key"])
    except (ai_apps.ConnectError, access.AccessError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return out


@router.get("/tools")
async def tools() -> list[dict]:
    from app.api.public_routes import TOOLS

    return [{"name": t["name"], "description": t["description"], "permission": scope,
             "read_only": t["annotations"]["readOnlyHint"], "arguments": t["inputSchema"].get("properties", {}),
             "required": t["inputSchema"].get("required", [])} for scope, t in TOOLS]


@router.get("/logs/{name}")
async def logs(name: str, lines: int = 400) -> dict:
    if name not in LOGS:
        raise HTTPException(404, "No such log")
    path = get_settings().data_dir / "logs" / f"{name}.log"
    text = redact(_tail(path, max(20, min(lines, 4000)))) if path.exists() else ""
    return {"name": name, "path": str(path), "text": text}
