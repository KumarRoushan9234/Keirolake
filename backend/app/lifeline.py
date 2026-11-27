"""End the engine when the desktop app that started it is gone.

The packaged engine runs as a small launcher plus the real engine process. When the app stops the launcher (on an
update, a crash or a forced quit), the real engine could keep running and hold its ports, so the next start fails. The
app passes its own process id in KEIROLAKE_PARENT_PID; this thread checks it every two seconds and exits when it's gone.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger(__name__)
CHECK_SECONDS = 2.0


def _alive(pid: int, created: float | None) -> bool:
    import psutil

    try:
        proc = psutil.Process(pid)
        # A new process can reuse the id; the creation time tells them apart.
        return proc.is_running() and (created is None or abs(proc.create_time() - created) < 1)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def watch(pid: int, on_gone=lambda: os._exit(0), interval: float = CHECK_SECONDS) -> threading.Thread | None:
    import psutil

    try:
        created = psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        log.warning("The app that started the engine (pid %s) is already gone; stopping", pid)
        on_gone()
        return None

    def loop() -> None:
        while _alive(pid, created):
            time.sleep(interval)
        log.warning("The app that started the engine (pid %s) has stopped; stopping the engine too", pid)
        on_gone()

    thread = threading.Thread(target=loop, name="lifeline", daemon=True)
    thread.start()
    return thread


def start_from_env() -> None:
    raw = os.getenv("KEIROLAKE_PARENT_PID", "")
    if raw.isdigit():
        watch(int(raw))
