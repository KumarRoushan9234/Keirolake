"""Shared, pooled HTTP clients.

Opening a new client per request means a fresh TCP + TLS handshake every time
(often 150-400 ms on its own). One keep-alive pool per event loop removes that
cost for KeiroLabs, model providers and Ollama. Timeouts are passed per request.
"""
from __future__ import annotations

import asyncio

import httpx

_pools: dict[tuple[int, str], tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}
LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=90)


def client(name: str = "default", *, follow_redirects: bool = False) -> httpx.AsyncClient:
    """A pooled client bound to the running event loop."""
    loop = asyncio.get_running_loop()
    for key, (owner, _c) in list(_pools.items()):  # drop clients whose loop is gone (tests create many loops)
        if owner.is_closed():
            _pools.pop(key, None)
    key = (id(loop), name)
    entry = _pools.get(key)
    if entry is None or entry[1].is_closed:
        entry = (loop, httpx.AsyncClient(timeout=None, limits=LIMITS, follow_redirects=follow_redirects))
        _pools[key] = entry
    return entry[1]


async def close_all() -> None:
    loop = asyncio.get_running_loop()
    for key, (owner, c) in list(_pools.items()):
        if owner is loop:
            await c.aclose()
            _pools.pop(key, None)
