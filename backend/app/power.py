"""Keep the lake reachable: stop the computer from sleeping while KeiroLake runs, and (optionally) keep it on when the lid closes.

What each case means:
- Locked screen: nothing to do. Locking doesn't pause programs; KeiroLake and its links keep working.
- Idle sleep: "Keep this computer awake" holds a sleep blocker while the app runs (Windows SetThreadExecutionState,
  Linux systemd-inhibit). The screen may still turn off; that's fine.
- Lid closed: Windows normally sleeps on lid close regardless of programs. "Stay on when the lid closes" changes the
  current power plan's lid action to "Do nothing" when plugged in (and on battery if chosen), remembering the old values
  so turning it off puts them back. On Linux this is a logind setting (HandleLidSwitch) that needs an administrator;
  the page shows the exact line to set.
- A computer that is actually asleep or shut down can't answer anything; nothing can change that.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading

from app.config import get_settings

log = logging.getLogger(__name__)
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_blocker: dict = {"thread": None, "stop": None, "proc": None}


class PowerError(RuntimeError):
    pass


# ---------- stay awake ----------
def _windows_hold(stop: threading.Event) -> None:
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_AWAYMODE_REQUIRED = 0x80000000, 0x00000001, 0x00000040
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
    stop.wait()
    kernel.SetThreadExecutionState(ES_CONTINUOUS)


def awake_on() -> bool:
    return _blocker["thread"] is not None or _blocker["proc"] is not None


def stay_awake(on: bool) -> None:
    """Hold (or release) the sleep blocker. The blocker belongs to a thread or process that lives as long as it is needed."""
    if on == awake_on():
        return
    if not on:
        if _blocker["stop"] is not None:
            _blocker["stop"].set()
        if _blocker["proc"] is not None:
            _blocker["proc"].terminate()
        _blocker.update(thread=None, stop=None, proc=None)
        return
    if sys.platform == "win32":
        stop = threading.Event()
        t = threading.Thread(target=_windows_hold, args=(stop,), name="keirolake-awake", daemon=True)
        t.start()
        _blocker.update(thread=t, stop=stop)
    elif sys.platform.startswith("linux"):
        try:
            proc = subprocess.Popen(["systemd-inhibit", "--what=sleep:idle", "--who=KeiroLake", "--why=Keeping your data lake reachable",
                                     "--mode=block", "sleep", "infinity"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise PowerError(f"This system has no systemd-inhibit: {exc}") from exc
        _blocker.update(proc=proc)
    elif sys.platform == "darwin":  # caffeinate holds the assertion for as long as it runs
        proc = subprocess.Popen(["caffeinate", "-ims"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _blocker.update(proc=proc)
    else:
        raise PowerError("Keeping the computer awake is available on Windows, macOS and Linux")


# ---------- lid action (Windows power plan) ----------
LID = ("SUB_BUTTONS", "LIDACTION")


def _state_file():
    return get_settings().data_dir / "power-lid.json"


def _powercfg(*args: str) -> str:
    res = subprocess.run(["powercfg", *args], capture_output=True, text=True, timeout=30, creationflags=NO_CONSOLE)
    if res.returncode != 0:
        raise PowerError(f"powercfg {' '.join(args[:1])} failed: {(res.stderr or res.stdout).strip()[:200]}")
    return res.stdout


def lid_values() -> dict:
    """The current plan's lid action when plugged in (ac) and on battery (dc): 0 nothing, 1 sleep, 2 hibernate, 3 shut down."""
    out = _powercfg("/qh", "SCHEME_CURRENT", *LID)  # /qh: many laptops hide the lid setting from /q
    ac = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
    dc = re.search(r"Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
    if not ac:
        raise PowerError("Couldn't read the lid setting from the power plan")
    return {"ac": int(ac.group(1), 16), "dc": int(dc.group(1), 16) if dc else None}


def keep_on_with_lid_closed(on: bool, on_battery: bool = False) -> dict:
    if sys.platform != "win32":
        if sys.platform == "darwin":
            raise PowerError("On a Mac, stay on with the lid closed by running: sudo pmset -a disablesleep 1 (and sudo pmset -a disablesleep 0 to undo)")
        raise PowerError("On Linux set HandleLidSwitch=ignore (and HandleLidSwitchExternalPower=ignore) in /etc/systemd/logind.conf, then restart systemd-logind")
    path = _state_file()
    if on:
        if not path.exists():  # remember the user's own values once, before the first change
            path.write_text(json.dumps(lid_values()), encoding="utf-8")
        _powercfg("/setacvalueindex", "SCHEME_CURRENT", *LID, "0")
        if on_battery:
            _powercfg("/setdcvalueindex", "SCHEME_CURRENT", *LID, "0")
    else:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"ac": 1, "dc": 1}
        _powercfg("/setacvalueindex", "SCHEME_CURRENT", *LID, str(saved.get("ac", 1)))
        if saved.get("dc") is not None:
            _powercfg("/setdcvalueindex", "SCHEME_CURRENT", *LID, str(saved["dc"]))
        path.unlink(missing_ok=True)
    _powercfg("/setactive", "SCHEME_CURRENT")
    return lid_values()


def status() -> dict:
    out = {"platform": sys.platform, "awake": awake_on(), "keep_awake": os.getenv("KEEP_AWAKE") == "1", "lid_changed": _state_file().exists()}
    if sys.platform == "win32":
        try:
            out["lid"] = lid_values()
        except PowerError as exc:
            out["lid_error"] = str(exc)
    return out


def apply_saved() -> None:
    """At start-up: hold the sleep blocker if the user asked for it."""
    if os.getenv("KEEP_AWAKE") == "1":
        try:
            stay_awake(True)
        except PowerError as exc:
            log.info("Couldn't keep the computer awake: %s", exc)
