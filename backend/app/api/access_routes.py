"""In-app management of the data API: keys, access log, webhooks, network access, exports."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import access, network, prefs
from app.lake import warehouse

router = APIRouter(prefix="/api/access")


@router.get("")
async def overview() -> dict:
    return {"keys": access.list_keys(), "webhooks": access.list_webhooks(), "network": network.status(),
            "scopes": access.SCOPES, "events": access.EVENTS}


class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    scopes: list[str] = Field(min_length=1)
    kb_ids: list[str] = Field(default_factory=list)
    expires_days: int | None = Field(None, ge=1, le=3650)


@router.post("/keys", status_code=201)
async def create_key(body: KeyBody) -> dict:
    try:
        key, raw = access.create_key(body.name, body.scopes, body.kb_ids, body.expires_days)
    except access.AccessError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"key": key, "secret": raw}


@router.post("/keys/{key_id}/revoke")
async def revoke(key_id: str) -> dict:
    if not access.revoke(key_id):
        raise HTTPException(404, "Key not found")
    return {"revoked": True}


@router.delete("/keys/{key_id}", status_code=204)
async def delete_key(key_id: str) -> None:
    if not access.delete_key(key_id):
        raise HTTPException(404, "Key not found")


@router.get("/log")
async def log(limit: int = 200) -> list[dict]:
    return access.access_log(min(max(limit, 1), 1000))


class HookBody(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    events: list[str] = Field(min_length=1)


@router.post("/webhooks", status_code=201)
async def add_webhook(body: HookBody) -> dict:
    try:
        hook, secret = access.add_webhook(body.url, body.events)
    except access.AccessError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"webhook": hook, "secret": secret}


@router.delete("/webhooks/{hook_id}", status_code=204)
async def delete_webhook(hook_id: str) -> None:
    if not access.delete_webhook(hook_id):
        raise HTTPException(404, "Webhook not found")


@router.post("/webhooks/test")
async def test_webhooks() -> dict:
    for event in access.EVENTS:
        access.emit(event, {"test": True})
    return {"sent": True}


class NetworkBody(BaseModel):
    mode: str = Field(pattern="^(off|local|lan|open)$")
    port: int = Field(network.DEFAULT_PORT, ge=1024, le=65535)
    bind: str | None = Field(None, max_length=45)
    allow: list[str] | None = Field(None, max_length=50)
    cert: str | None = Field(None, max_length=500)
    key: str | None = Field(None, max_length=500)
    upnp: bool | None = None


def _local_only(request: Request) -> None:
    if network.is_network_request(request.scope):
        raise HTTPException(403, "Change this on the computer running KeiroLake")


def _check_network(body: NetworkBody) -> dict:
    import ipaddress
    import ssl
    from pathlib import Path

    from app import auth

    if body.port == network.app_port():
        raise HTTPException(400, f"Port {body.port} is in use by the app itself; pick another")
    if body.mode == "open" and not auth.has_password():
        raise HTTPException(400, "Set a password first. Anyone who finds the port will see the sign-in page.")
    values = {"LAN_ACCESS": body.mode, "LAN_PORT": str(body.port)}
    if body.bind is not None:
        bind = body.bind.strip()
        if bind and bind != "0.0.0.0" and bind not in network.addresses() and bind != "127.0.0.1":  # noqa: S104
            raise HTTPException(400, f"{bind} isn't one of this computer's addresses")
        values["LAN_BIND"] = bind
    if body.allow is not None:
        for item in body.allow:
            try:
                ipaddress.ip_network(item.strip(), strict=False)
            except ValueError:
                raise HTTPException(400, f"{item!r} isn't an address or range like 203.0.113.0/24") from None
        values["LAN_ALLOW"] = ",".join(i.strip() for i in body.allow if i.strip())
    if body.cert is not None or body.key is not None:
        cert, key = (body.cert or "").strip(), (body.key or "").strip()
        if cert or key:
            if not (Path(cert).is_file() and Path(key).is_file()):
                raise HTTPException(400, "Pick both the certificate file and its key file")
            try:
                ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert, key)
            except (ssl.SSLError, OSError) as exc:
                raise HTTPException(400, f"That certificate and key don't load: {exc}") from exc
        values["LAN_CERT"], values["LAN_KEY"] = cert, key
    if body.upnp is not None:
        values["LAN_UPNP"] = "1" if body.upnp else ""
    return values


@router.put("/network")
async def set_network(body: NetworkBody, request: Request) -> dict:
    _local_only(request)
    prefs.update(_check_network(body))
    return await network.restart()


@router.get("/network/public-ip")
async def public_ip(request: Request) -> dict:
    """This connection's public address, asked of a public lookup service only when the user clicks."""
    import httpx

    _local_only(request)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            ip = (await client.get("https://api.ipify.org", params={"format": "json"})).json()["ip"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise HTTPException(502, f"Couldn't look up the public address: {exc}") from exc
    private = _is_cgnat(ip)
    return {"ip": ip, "cgnat": private}


def _is_cgnat(ip: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


@router.post("/network/check")
async def check_reachable(request: Request) -> dict:
    """Ask a public port-check service whether the port answers from the internet. Shares the public IP and port with it."""
    import httpx

    _local_only(request)
    port = network.port()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            data = (await client.get(f"https://ifconfig.co/port/{port}", headers={"Accept": "application/json"})).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"The check service didn't answer: {exc}") from exc
    return {"ip": data.get("ip"), "port": port, "reachable": bool(data.get("reachable"))}


class FirewallBody(BaseModel):
    action: str = Field(pattern="^(add|remove)$")


@router.post("/network/firewall")
async def firewall(body: FirewallBody, request: Request) -> dict:
    """Windows: add or remove an inbound rule for the port. Windows asks for administrator approval itself."""
    import subprocess
    import sys

    _local_only(request)
    port = network.port()
    rule = f"KeiroLake port {port}"
    if sys.platform != "win32":
        cmd = f"sudo ufw {'allow' if body.action == 'add' else 'delete allow'} {port}/tcp"
        return {"done": False, "manual": cmd}
    args = (f'advfirewall firewall add rule name="{rule}" dir=in action=allow protocol=TCP localport={port}' if body.action == "add"
            else f'advfirewall firewall delete rule name="{rule}"')
    ps = f"Start-Process -FilePath netsh -ArgumentList '{args}' -Verb RunAs -WindowStyle Hidden -Wait"
    res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if res.returncode != 0:
        raise HTTPException(400, "Windows didn't make the change (the approval may have been declined)")
    return {"done": True, "rule": rule}


@router.get("/export/{table}")
async def export_table(table: str, format: str = "csv"):
    """Download one of your tables as CSV, JSON or JSON Lines."""
    from app.api.public_routes import export, user_tables

    if table not in {t["table"] for t in user_tables()} or not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise HTTPException(404, "No such table")
    result = warehouse.run_query(f'SELECT * FROM "{table}"', warehouse.MAX_ROWS, user_tables_only=True)
    return export(result, format, table)
