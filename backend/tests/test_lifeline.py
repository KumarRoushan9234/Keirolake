"""The engine stops itself when the app that started it is gone, and a busy port never stops the engine."""
import asyncio
import socket
import subprocess
import sys
import threading

import pytest

from app import lifeline, network


def test_watch_fires_when_parent_exits():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
    gone = threading.Event()
    lifeline.watch(proc.pid, on_gone=gone.set, interval=0.1)
    assert not gone.wait(0.5), "fired while the parent was still running"
    proc.wait()
    assert gone.wait(5), "didn't notice the parent stopped"


def test_watch_fires_at_once_for_a_missing_parent():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    gone = threading.Event()
    lifeline.watch(proc.pid, on_gone=gone.set, interval=0.1)
    assert gone.wait(5)


def test_port_busy():
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        busy_port = held.getsockname()[1]
        assert network.port_busy("127.0.0.1", busy_port)
    assert not network.port_busy("127.0.0.1", busy_port)


def test_serve_quietly_turns_systemexit_into_an_error():
    class Failing:
        async def serve(self):
            raise SystemExit(3)

    with pytest.raises(OSError, match="exit 3"):
        asyncio.run(network.serve_quietly(Failing()))
