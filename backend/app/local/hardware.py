"""What this machine can run: RAM, CPU, GPUs and free disk."""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

import psutil

from app.config import get_settings

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def _cpu_name() -> str:
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        if sys.platform.startswith("linux"):
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        name, _, mem = line.rpartition(",")
        try:
            gpus.append({"name": name.strip(), "vram_gb": round(int(mem.strip()) / 1024, 1), "vendor": "nvidia"})
        except ValueError:
            continue
    return gpus


def _amd_linux() -> list[dict]:
    gpus = []
    for card in Path("/sys/class/drm").glob("card[0-9]*/device/mem_info_vram_total") if sys.platform.startswith("linux") else []:
        try:
            vram = int(card.read_text().strip()) / 2**30
            if vram >= 2:
                gpus.append({"name": "AMD GPU", "vram_gb": round(vram, 1), "vendor": "amd"})
        except (OSError, ValueError):
            continue
    return gpus


def probe() -> dict:
    data_dir = get_settings().data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    disk = psutil.disk_usage(str(data_dir))
    gpus = _nvidia() + _amd_linux()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": _cpu_name(),
        "cores": psutil.cpu_count(logical=False) or 0,
        "threads": psutil.cpu_count(logical=True) or 0,
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "ram_free_gb": round(psutil.virtual_memory().available / 2**30, 1),
        "gpus": gpus,
        "vram_gb": max((g["vram_gb"] for g in gpus), default=0.0),
        "data_dir": str(data_dir),
        "disk_free_gb": round(disk.free / 2**30, 1),
        "disk_total_gb": round(disk.total / 2**30, 1),
    }
