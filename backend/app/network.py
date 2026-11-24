"""Reaching KeiroLake from a browser or another device.

Off by default. "This computer" starts a second listener on a fixed port at 127.0.0.1 (plain HTTP; the
desktop window's own port changes every launch). "Home network" listens on all interfaces with HTTPS
(a self-signed certificate made on this PC). The listener serves the data API (/v1, /mcp, with an API
key) and the web UI (sign in with the password you set; /api needs the session cookie). The desktop
window itself never goes through here. For access from outside the home network, Tailscale (or a
similar private network) is the safe way: it reaches this same listener without opening a router port.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import socket
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULT_PORT = 8443
PUBLIC_PREFIXES = ("/v1", "/mcp")

_server = None
_task: asyncio.Task | None = None
_error = ""


MODES = ("off", "local", "lan", "open")
RATE_PER_MINUTE = 600  # requests a minute from one visitor over the listener


def mode() -> str:
    raw = os.getenv("LAN_ACCESS", "off").lower()
    if raw == "open":
        return "open"
    return "lan" if raw in ("on", "lan", "1", "true") else "local" if raw == "local" else "off"


def bind_address() -> str:
    """Where the listener listens: this computer only, every adapter, or one adapter the user picked."""
    m = mode()
    if m == "local":
        return "127.0.0.1"
    if m == "open":
        raw = os.getenv("LAN_BIND", "").strip()
        try:
            return str(ipaddress.ip_address(raw)) if raw else "0.0.0.0"  # noqa: S104 - the user's explicit choice
        except ValueError:
            return "0.0.0.0"  # noqa: S104
    return "0.0.0.0"  # noqa: S104 - home network mode listens on the LAN


def allow_list() -> list:
    nets = []
    for part in os.getenv("LAN_ALLOW", "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            with contextlib.suppress(ValueError):
                nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def own_cert() -> tuple[Path, Path] | None:
    cert, key = os.getenv("LAN_CERT", "").strip(), os.getenv("LAN_KEY", "").strip()
    if cert and key and Path(cert).is_file() and Path(key).is_file():
        return Path(cert), Path(key)
    return None


_hits: dict[str, list[float]] = {}


def visitor(scope: dict) -> str:
    """The visitor's address. Behind the tunnel (a connection from this computer) the tunnel's header is used."""
    host = (scope.get("client") or ("", 0))[0]
    if is_loopback(host):
        for k, v in scope.get("headers") or []:
            if k == b"cf-connecting-ip" and 0 < len(v) <= 45:
                return v.decode("latin-1")
    return host


def permitted(ip: str) -> bool:
    nets = allow_list()
    if not nets or is_loopback(ip):
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def rate_ok(ip: str, now: float | None = None) -> bool:
    import time

    now = time.monotonic() if now is None else now
    recent = [t for t in _hits.get(ip, []) if now - t < 60]
    if len(recent) >= RATE_PER_MINUTE:
        _hits[ip] = recent
        return False
    recent.append(now)
    _hits[ip] = recent
    if len(_hits) > 5000:
        _hits.clear()
    return True


def enabled() -> bool:
    return mode() != "off"


def port() -> int:
    try:
        return int(os.getenv("LAN_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


TAG = "keirolake.network"


def is_network_request(scope: dict) -> bool:
    """True for requests that came in through the network listener (it tags every request it receives)."""
    return bool(scope.get(TAG))


def tagged(app):
    """The app as served by the network listener: same routes, every request marked."""
    async def network_app(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            ip = visitor(scope)
            refusal = None if permitted(ip) else (403, "This address isn't on the allow-list")
            if refusal is None and not rate_ok(ip):
                refusal = (429, "Too many requests")
            if refusal is not None:
                if scope["type"] == "http":
                    import json as _json

                    await send({"type": "http.response.start", "status": refusal[0], "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": _json.dumps({"detail": refusal[1]}).encode()})
                return
        scope[TAG] = True
        await app(scope, receive, send)

    return network_app


def app_port() -> int | None:
    try:
        return int(os.environ["KEIROLAKE_PORT"])
    except (KeyError, ValueError):
        return None


def is_loopback(host: str | None) -> bool:
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


def _tls_dir() -> Path:
    from app.config import get_settings

    path = get_settings().data_dir / "tls"
    path.mkdir(parents=True, exist_ok=True)
    return path


def addresses() -> list[str]:
    """This PC's private network addresses (LAN and Tailscale's 100.64.0.0/10)."""
    found: set[str] = set()
    try:
        import psutil

        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET:
                    ip = ipaddress.ip_address(a.address)
                    if (ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")) and not ip.is_loopback and not ip.is_link_local:
                        found.add(a.address)
    except Exception:  # noqa: BLE001 - addresses are a convenience
        pass
    return sorted(found)


def ensure_cert() -> tuple[Path, Path]:
    """A self-signed certificate for this PC's names and addresses, made once and reused."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder = _tls_dir()
    cert_path, key_path = folder / "cert.pem", folder / "key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName(host), x509.DNSName("localhost")]
    names += [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses()]
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"KeiroLake on {host}")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650)).add_extension(x509.SubjectAlternativeName(names), critical=False)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    with contextlib.suppress(OSError):
        os.chmod(key_path, 0o600)
    return cert_path, key_path


def fingerprint() -> str:
    cert_path = _tls_dir() / "cert.pem"
    if not cert_path.exists():
        return ""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    der = x509.load_pem_x509_certificate(cert_path.read_bytes()).public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


_upnp = {"on": False, "external_ip": "", "error": ""}


def status() -> dict:
    running = _server is not None and _task is not None and not _task.done()
    secure = mode() in ("lan", "open")
    bind = bind_address()
    if not running:
        urls = []
    elif not secure:
        urls = [f"http://127.0.0.1:{port()}"]
    else:
        hosts = addresses() if bind == "0.0.0.0" else [bind]  # noqa: S104
        urls = [f"https://{a}:{port()}" for a in hosts]
        if mode() == "open" and _upnp["external_ip"]:
            urls.append(f"https://{_upnp['external_ip']}:{port()}")
    return {"mode": mode(), "enabled": enabled(), "running": running, "port": port(), "addresses": addresses(), "bind": bind,
            "allow": [str(n) for n in allow_list()], "own_cert": own_cert() is not None, "upnp": dict(_upnp),
            "upnp_wanted": os.getenv("LAN_UPNP", "") == "1",
            "urls": urls, "fingerprint": fingerprint() if running and secure and own_cert() is None else "", "error": _error}


async def start() -> None:
    """Start the HTTPS listener if network access is on."""
    global _server, _task, _error
    if not enabled() or (_task is not None and not _task.done()):
        return
    if port() == app_port():
        _error = f"Port {port()} is the app's own port; pick another"
        return
    import uvicorn

    from app.main import app

    class _Quiet(uvicorn.Server):
        """Shares the main server's process: leave signal handling to it."""

        def install_signal_handlers(self) -> None:  # older uvicorn
            pass

        @contextlib.contextmanager
        def capture_signals(self):  # newer uvicorn
            yield

    if mode() == "open":
        from app import auth

        if not auth.has_password():
            _error = "Set a password before opening a port to the internet"
            return
    try:
        if mode() in ("lan", "open"):
            cert, key = own_cert() or await asyncio.to_thread(ensure_cert)
            config = uvicorn.Config(tagged(app), host=bind_address(), port=port(), ssl_certfile=str(cert), ssl_keyfile=str(key),  # noqa: S104 - opt-in
                                    log_level="warning", access_log=False, lifespan="off")
        else:
            config = uvicorn.Config(tagged(app), host="127.0.0.1", port=port(), log_level="warning", access_log=False, lifespan="off")
        _server = _Quiet(config)
        _task = asyncio.create_task(_server.serve())
        _error = ""
        await asyncio.sleep(0.5)
        if _task.done() and _task.exception():
            raise _task.exception()
        log.info("Data API listening (%s) on port %s", mode(), port())
        if mode() == "open" and os.getenv("LAN_UPNP", "") == "1":
            await _map_port()
    except Exception as exc:  # noqa: BLE001 - shown in the app
        _error = f"Couldn't listen on port {port()}: {exc}"
        log.warning(_error)
        _server, _task = None, None


async def _map_port() -> None:
    from app import upnp

    try:
        _upnp.update(on=True, external_ip=await upnp.add_mapping(port()), error="")
    except Exception as exc:  # noqa: BLE001 - shown in the app; the listener still works on the LAN
        _upnp.update(on=False, external_ip="", error=str(exc)[:200])


async def _unmap_port() -> None:
    from app import upnp

    if _upnp["on"]:
        with contextlib.suppress(Exception):
            await upnp.remove_mapping(port())
    _upnp.update(on=False, external_ip="", error="")


async def stop() -> None:
    global _server, _task
    await _unmap_port()
    if _server is not None:
        _server.should_exit = True
        if _task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(_task, 5)
    _server, _task = None, None


async def restart() -> dict:
    await stop()
    await start()
    return status()
