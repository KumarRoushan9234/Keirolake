"""macOS: the tunnel program comes from a checked .tgz, keep-awake uses caffeinate, Ollama points at the Mac app."""
import hashlib
import io
import tarfile

import httpx
import pytest
import respx

from app import power, tunnel
from app.local import install


def _tgz(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


@pytest.mark.asyncio
@respx.mock
async def test_mac_tunnel_install_extracts_the_checked_archive(monkeypatch, tmp_path):
    monkeypatch.setattr(tunnel.sys, "platform", "darwin")
    monkeypatch.setattr(tunnel, "binary", lambda: tmp_path / "bin" / "cloudflared")
    import platform
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    archive = _tgz("cloudflared", b"#!/bin/sh\necho mac\n")
    respx.get(tunnel.RELEASES).mock(return_value=httpx.Response(200, json={"assets": [
        {"name": "cloudflared-darwin-arm64.tgz", "digest": "sha256:" + hashlib.sha256(archive).hexdigest(), "browser_download_url": "https://dl/arm"}]}))
    respx.get("https://dl/arm").mock(return_value=httpx.Response(200, content=archive))
    await tunnel.install()
    assert (tmp_path / "bin" / "cloudflared").read_bytes() == b"#!/bin/sh\necho mac\n"


def test_mac_keep_awake_and_ollama(monkeypatch):
    started = []

    class Proc:
        def __init__(self, args, **kw):
            started.append(args)

        def terminate(self):
            started.append("stopped")

    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(power.subprocess, "Popen", Proc)
    power.stay_awake(True)
    power.stay_awake(False)
    assert started == [["caffeinate", "-ims"], "stopped"]
    with pytest.raises(power.PowerError, match="pmset"):
        power.keep_on_with_lid_closed(True)
    monkeypatch.setattr(install.sys, "platform", "darwin")
    monkeypatch.setattr(install.platform, "machine", lambda: "arm64")
    with pytest.raises(install.InstallError, match="download/mac"):
        install.asset_name()
