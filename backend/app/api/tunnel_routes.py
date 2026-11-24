"""One switch to reach the lake from other networks. Desktop window only: a remote visitor can never turn it on or off."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import network, prefs, tunnel

router = APIRouter(prefix="/api/network/tunnel")


def _local_only(request: Request) -> None:
    if network.is_network_request(request.scope):
        raise HTTPException(403, "Change this on the computer running KeiroLake")


@router.get("")
async def tunnel_status(request: Request) -> dict:
    _local_only(request)
    return tunnel.status()


@router.post("/start")
async def tunnel_start(request: Request) -> dict:
    _local_only(request)
    try:
        return await tunnel.start()
    except tunnel.TunnelError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/stop")
async def tunnel_stop(request: Request) -> dict:
    _local_only(request)
    return await tunnel.stop(remember=True)


class TunnelConfig(BaseModel):
    token: str | None = Field(None, max_length=600)  # "" removes it and goes back to the quick tunnel
    hostname: str | None = Field(None, max_length=253)
    port: int | None = Field(None, ge=1024, le=65535)


@router.put("/config")
async def tunnel_config(body: TunnelConfig, request: Request) -> dict:
    """A named tunnel: your Cloudflare tunnel token and the hostname you routed to http://localhost:<port>."""
    import re

    _local_only(request)
    values: dict[str, str] = {}
    if body.token is not None:
        token = body.token.strip()
        if token and not re.fullmatch(r"[A-Za-z0-9_\-=.]{40,600}", token):
            raise HTTPException(400, "That doesn't look like a Cloudflare tunnel token (a long string from the tunnel's install command)")
        values["CLOUDFLARE_TUNNEL_TOKEN"] = token
    if body.hostname is not None:
        host = body.hostname.strip().lower().removeprefix("https://").strip("/")
        if host and not re.fullmatch(r"(?=.{3,253}$)([a-z0-9-]+\.)+[a-z]{2,}", host):
            raise HTTPException(400, "Use a hostname such as lake.example.com")
        values["TUNNEL_HOSTNAME"] = host
    if body.port is not None:
        values["TUNNEL_PORT"] = str(body.port)
    was = tunnel.running()
    if was:
        await tunnel.stop(remember=False)
    prefs.update(values)
    if was:
        try:
            await tunnel.start()
        except tunnel.TunnelError as exc:
            raise HTTPException(400, str(exc)) from exc
    return tunnel.status()


# ---------- staying reachable: sleep and lid ----------
power_router = APIRouter(prefix="/api/power")


@power_router.get("")
async def power_status(request: Request) -> dict:
    from app import power

    _local_only(request)
    return power.status()


class PowerBody(BaseModel):
    keep_awake: bool | None = None
    lid: bool | None = None
    lid_on_battery: bool = False


@power_router.put("")
async def power_set(body: PowerBody, request: Request) -> dict:
    from app import power

    _local_only(request)
    try:
        if body.keep_awake is not None:
            power.stay_awake(body.keep_awake)
            prefs.update({"KEEP_AWAKE": "1" if body.keep_awake else ""})
        if body.lid is not None:
            power.keep_on_with_lid_closed(body.lid, body.lid_on_battery)
    except power.PowerError as exc:
        raise HTTPException(400, str(exc)) from exc
    return power.status()
