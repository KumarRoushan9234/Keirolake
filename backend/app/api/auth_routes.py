"""Sign-in endpoints. Login, status and code exchange work from the network listener; managing the
password and devices works only from the desktop window (it carries the launch token)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import auth, network

router = APIRouter(prefix="/api/auth")
OPEN = ("/api/auth/status", "/api/auth/login", "/api/auth/exchange", "/api/auth/logout")


def _client(request: Request) -> str:
    """Who is signing in, for the lockout. Behind the tunnel every visitor reaches us from this computer, so the
    tunnel's own client-address header is used, but only when the connection really is from this computer."""
    host = request.client.host if request.client else "unknown"
    if network.is_loopback(host):
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if forwarded and len(forwarded) <= 45:
            return forwarded
    return host


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def _device(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    for name in ("Edg", "Chrome", "Firefox", "Safari"):
        if name in ua:
            os_ = "Windows" if "Windows" in ua else "Mac" if "Mac" in ua else "Android" if "Android" in ua else "iPhone" if "iPhone" in ua else "Linux" if "Linux" in ua else ""
            return f"{'Edge' if name == 'Edg' else name} on {os_}".strip()
    return ua[:60] or "Unknown device"


def _cookie(response: Response, token: str, secure: bool) -> None:
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="strict", secure=secure, path="/")


def _local_only(request: Request) -> None:
    if network.is_network_request(request.scope):
        raise HTTPException(403, "Change this on the computer running KeiroLake")


@router.get("/status")
async def status(request: Request) -> dict:
    token = request.cookies.get(auth.COOKIE)
    via_network = network.is_network_request(request.scope)
    return {"network": via_network, "password_set": auth.has_password(),
            "signed_in": (not via_network) or auth.session(token) is not None}


class LoginBody(BaseModel):
    password: str = Field(max_length=200)


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict:
    try:
        token = auth.login(body.password, _device(request), _client(request))
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    _cookie(response, token, _https(request))
    return {"signed_in": True}


class CodeBody(BaseModel):
    code: str = Field(max_length=100)


@router.post("/exchange")
async def exchange(body: CodeBody, request: Request, response: Response) -> dict:
    try:
        token = auth.exchange_code(body.code, _device(request), _client(request))
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    _cookie(response, token, _https(request))
    return {"signed_in": True}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    auth.logout(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"signed_in": False}


# ----- desktop window only -----
class PasswordBody(BaseModel):
    password: str = Field(max_length=200)


@router.put("/password")
async def set_password(body: PasswordBody, request: Request) -> dict:
    _local_only(request)
    try:
        auth.set_password(body.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"password_set": True}


@router.delete("/password")
async def clear_password(request: Request) -> dict:
    _local_only(request)
    auth.clear_password()
    return {"password_set": False}


@router.get("/sessions")
async def sessions(request: Request) -> list[dict]:
    _local_only(request)
    return auth.sessions()


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke(session_id: str, request: Request) -> None:
    _local_only(request)
    if not auth.revoke(session_id):
        raise HTTPException(404, "Session not found")


@router.post("/sessions/sign-out-all")
async def sign_out_all(request: Request) -> dict:
    _local_only(request)
    return {"signed_out": auth.sign_out_everywhere()}


@router.post("/open-code")
async def open_code(request: Request) -> dict:
    """A one-time code for opening the UI in a browser on this PC; the tray turns it into a link."""
    _local_only(request)
    n = network.status()
    return {"code": auth.make_code(), "base": (n["urls"][0] if n["urls"] else None), "network_mode": n["mode"], "port": n["port"]}


@router.post("/open-link")
async def open_link(request: Request) -> dict:
    """A link that opens the UI in a browser on this PC, signed in. Starts the local listener if it is off."""
    import os

    from app import prefs

    _local_only(request)
    if network.mode() == "off":
        prefs.update({"LAN_ACCESS": "local", "LAN_PORT": os.getenv("LAN_PORT") or str(network.DEFAULT_PORT)})
        await network.restart()
    n = network.status()
    if not n["running"]:
        raise HTTPException(500, n["error"] or "The browser link couldn't start")
    base = f"http://127.0.0.1:{n['port']}" if n["mode"] == "local" else f"https://127.0.0.1:{n['port']}"
    return {"url": f"{base}/#code={auth.make_code()}"}
