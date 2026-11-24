"""Localhost API protection for the desktop app.

Any website open in a normal browser can send requests to 127.0.0.1, and DNS
rebinding can make a hostile domain resolve to it. When the desktop shell sets
KEIROLAKE_TOKEN, every /api request must carry that token in the
X-KeiroLake-Token header and a loopback Host header. Library pages shown in the
Reader iframe can't send headers, so their URL carries the token instead
(/api/zim/<token>/...) and that route checks it itself.
"""
from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import get_settings

LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
OPEN_PATHS = ("/api/zim/",)


def token_ok(given: str | None) -> bool:
    expected = get_settings().token
    return not expected or (given is not None and hmac.compare_digest(given, expected))


class TokenMiddleware(BaseHTTPMiddleware):
    async def _network_ui(self, request: Request, call_next):
        """Requests through the network listener: the UI files are open, /api needs a signed-in session."""
        from app import auth
        from app.api.auth_routes import OPEN

        path = request.url.path
        if not path.startswith("/api"):
            return await call_next(request)  # static UI files
        if path in OPEN:
            return await call_next(request)
        if auth.session(request.cookies.get(auth.COOKIE)) is None:
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):  # SameSite=Strict plus an Origin check against cross-site posts
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        return await call_next(request)

    async def dispatch(self, request: Request, call_next):
        from app import access, network

        path = request.url.path
        public = path.startswith(network.PUBLIC_PREFIXES)
        if network.is_network_request(request.scope) and not public:
            return await self._network_ui(request, call_next)
        if public:
            response = await call_next(request)
            client = request.client.host if request.client else ""
            access.record(getattr(request.state, "api_key", None), request.method, path, response.status_code, client)
            return response
        if path.startswith("/api") and request.client and not network.is_loopback(request.client.host):
            return JSONResponse({"detail": "The app API is only available on this computer"}, status_code=403)
        if get_settings().token and path.startswith("/api"):
            host = (request.headers.get("host") or "").rsplit(":", 1)[0]
            if host not in LOOPBACK:
                return JSONResponse({"detail": "Forbidden host"}, status_code=403)
            if request.method != "OPTIONS" and not request.url.path.startswith(OPEN_PATHS):
                if not token_ok(request.headers.get("x-keirolake-token")):
                    return JSONResponse({"detail": "Missing or wrong API token"}, status_code=401)
        return await call_next(request)
