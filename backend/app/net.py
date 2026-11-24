"""Background internet check, so offline requests fail fast instead of hanging.

`online` is None until the first check finishes; callers treat None as online.
KEIROLAKE_FORCE_OFFLINE=1 simulates an outage.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger(__name__)
PROBES = (("1.1.1.1", 443), ("8.8.8.8", 443), ("openrouter.ai", 443))
INTERVAL = 15.0


class Connectivity:
    def __init__(self) -> None:
        self.online: bool | None = None
        self.checked_at: float | None = None
        self._task: asyncio.Task | None = None

    @property
    def is_online(self) -> bool:
        if os.getenv("KEIROLAKE_FORCE_OFFLINE") == "1":
            return False
        return self.online is not False

    async def check(self) -> bool:
        if os.getenv("KEIROLAKE_FORCE_OFFLINE") == "1":
            self.online, self.checked_at = False, time.time()
            return False
        for host, port in PROBES:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
                writer.close()
                self.online = True
                break
            except (OSError, asyncio.TimeoutError):
                continue
        else:
            self.online = False
        self.checked_at = time.time()
        return self.online

    async def _loop(self) -> None:
        while True:
            previous = self.online
            try:
                await self.check()
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("Connectivity check failed")
            if previous is not None and previous != self.online:
                log.info("Network is now %s", "online" if self.online else "offline")
            await asyncio.sleep(INTERVAL)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def status(self) -> dict:
        return {"online": self.is_online, "checked_at": self.checked_at,
                "forced_offline": os.getenv("KEIROLAKE_FORCE_OFFLINE") == "1"}


connectivity = Connectivity()
