"""The update check reads GitHub Releases and never fails the app when offline."""
import httpx
import pytest
import respx

from app import updates


def test_version_compare():
    assert updates.newer("0.7.10", "0.7.4") and updates.newer("v1.0.0", "0.9.9")
    assert not updates.newer("0.7.4", "0.7.4") and not updates.newer("0.7.3", "0.7.4")


@pytest.mark.asyncio
@respx.mock
async def test_latest_release_and_offline():
    route = respx.get(f"https://api.github.com/repos/{updates.REPO}/releases/latest")
    route.mock(return_value=httpx.Response(200, json={"tag_name": "v99.0.0", "body": "New things", "html_url": "https://x",
                                                       "assets": [{"name": "KeiroLake_99.0.0_x64-setup.exe", "browser_download_url": "https://x/a", "size": 5},
                                                                  {"name": "latest.json", "browser_download_url": "https://x/l"}]}))
    out = await updates.latest(force=True)
    assert out["available"] and out["version"] == "99.0.0" and [a["name"] for a in out["assets"]] == ["KeiroLake_99.0.0_x64-setup.exe"]
    route.mock(side_effect=httpx.ConnectError("offline"))
    out = await updates.latest(force=True)
    assert not out["available"] and "Couldn't reach GitHub" in out["error"]


def test_minisign_checks_real_tauri_signatures():
    """A file signed with the project's updater key verifies; a changed file or another key doesn't."""
    from pathlib import Path

    from app import minisign

    here = Path(__file__).parent / "fixtures"
    data, sig = (here / "signed.txt").read_bytes(), (here / "signed.txt.sig").read_text()
    assert "file:signed.txt" in minisign.verify(data, sig)
    with pytest.raises(minisign.SignatureError, match="doesn't match"):
        minisign.verify(data + b"!", sig)
    import base64
    other = base64.b64encode(b"untrusted comment: x\n" + base64.b64encode(b"Ed" + b"\x00" * 40) + b"\n").decode()
    with pytest.raises(minisign.SignatureError, match="different key"):
        minisign.verify(data, sig, other)


@pytest.mark.asyncio
@respx.mock
async def test_package_update_refuses_an_unsigned_or_tampered_package(monkeypatch):
    from app import minisign
    from pathlib import Path

    monkeypatch.setattr(updates, "package_kind", lambda: "deb")
    monkeypatch.setattr(updates, "latest", lambda force=False: _async({"available": True, "version": "99.0.0", "current": "0.8.0"}))
    base = f"https://github.com/{updates.REPO}/releases/download/v99.0.0/KeiroLake_99.0.0_amd64.deb"
    fixture = Path(__file__).parent / "fixtures"
    respx.get(base + ".sig").mock(return_value=httpx.Response(200, text=(fixture / "signed.txt.sig").read_text()))
    respx.get(base).mock(return_value=httpx.Response(200, content=b"not the signed file"))
    ran = []

    async def fake_exec(*a, **k):
        ran.append(a)
        raise AssertionError("must not install")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    class Ctx:
        async def progress(self, *a):
            pass

    with pytest.raises(minisign.SignatureError):
        await updates.install_package_job()(Ctx())
    assert not ran
    assert updates.install_command("deb", "/tmp/k.deb")[:3] == ["pkexec", "apt-get", "install"]


async def _async(v):
    return v
