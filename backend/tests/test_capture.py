"""Capture: voice notes, photos, links and text land in the "Captured" collection; checks on what's accepted."""
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import inbox, speech
from app.local import ollama
from app.main import app


class Ctx:
    async def progress(self, *a):
        pass


@pytest.fixture(autouse=True)
def fake_inbox(monkeypatch):
    """No vector store in unit tests: the "Captured" collection is a fixed id."""
    async def inbox_id():
        return "kb-captured"

    monkeypatch.setattr(inbox, "inbox_id", inbox_id)
    monkeypatch.setattr(inbox, "start_run", lambda kind, name, kb_id, config=None: ("src-test", f"run-{kind}"))
    monkeypatch.setattr("app.ingest.web.start_run", lambda kind, name, kb_id, config=None: ("src-test", f"run-{kind}"))


@pytest.fixture
def fake_index(monkeypatch):
    """Skip embedding: record what would be indexed."""
    saved = []

    async def run(kb_id, source_id, run_id, raw_dir, docs, ctx, extra=None):
        saved.extend(docs)
        return {"documents": len(docs)}

    monkeypatch.setattr(inbox, "_run", run)
    return saved


@pytest.fixture
def fake_speech(monkeypatch):
    async def transcribe(path, language=None, progress=None):
        assert path.exists() and path.read_bytes() == b"opus-audio"
        return {"text": "Remember to compare solar panel prices in Pune", "language": "en", "seconds": 3.2}

    monkeypatch.setattr(speech, "transcribe", transcribe)


@pytest.mark.asyncio
async def test_voice_note_and_transcribe(fake_index, fake_speech):
    out = await inbox.voice_note_job(b"opus-audio", ".webm")(Ctx())
    assert out["text"].startswith("Remember") and out["kb_id"] == "kb-captured"
    doc = fake_index[0]
    assert doc.title == "Voice note: Remember to compare solar panel prices in Pune" and doc.raw_path.read_bytes() == b"opus-audio"
    text = await inbox.transcribe_job(b"opus-audio", ".webm")(Ctx())
    assert text["text"].startswith("Remember") and len(fake_index) == 1  # transcribe only: nothing saved


@pytest.mark.asyncio
@respx.mock
async def test_photo_is_read_by_a_local_vision_model(fake_index):
    base = ollama.base_url()
    respx.get(f"{base}/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "gemini-3-flash:latest", "remote_host": "https://ollama.com:443"}, {"name": "qwen2.5:7b"}, {"name": "qwen2.5vl:3b"}]}))
    respx.post(f"{base}/api/show").mock(side_effect=lambda req: httpx.Response(
        200, json={"capabilities": ["completion", "vision"] if (b"vl" in req.content or b"gemini" in req.content) else ["completion"]}))
    respx.post(f"{base}/api/chat").mock(return_value=httpx.Response(200, json={"message": {"content": "# Receipt\nTotal: 420"}}))
    out = await inbox.photo_job(b"\xff\xd8jpeg", ".jpg", "Lunch receipt")(Ctx())
    assert out["read_by"] == "qwen2.5vl:3b" and fake_index[0].text == "Lunch receipt\n\n# Receipt\nTotal: 420"


@pytest.mark.asyncio
@respx.mock
async def test_photo_without_a_vision_model_is_kept(fake_index):
    respx.get(f"{ollama.base_url()}/api/tags").mock(side_effect=httpx.ConnectError("no ollama"))
    out = await inbox.photo_job(b"\x89PNG", ".png")(Ctx())
    assert out["read_by"] is None and "Install a vision model" in fake_index[0].text


def test_upload_checks():
    with pytest.raises(inbox.CaptureError, match="type"):
        inbox.check_upload("evil.exe", b"x", inbox.AUDIO_TYPES, 1)
    with pytest.raises(inbox.CaptureError, match="larger"):
        inbox.check_upload("a.webm", b"x" * (2 * 2**20), inbox.AUDIO_TYPES, 1)
    with pytest.raises(inbox.CaptureError, match="empty"):
        inbox.check_upload("a.webm", b"", inbox.AUDIO_TYPES, 1)
    assert inbox.check_upload("note.m4a", b"x", inbox.AUDIO_TYPES, 1) == ".m4a"


def test_capture_routes(monkeypatch):
    c = TestClient(app)
    monkeypatch.setattr(speech, "available", lambda: True)
    assert c.post("/api/capture/audio", files={"file": ("a.exe", b"x")}).status_code == 400
    assert c.post("/api/capture/audio", files={"file": ("a.webm", b"x")}, data={"mode": "shout"}).status_code == 400
    assert c.post("/api/capture/image", files={"file": ("a.pdf", b"x")}).status_code == 400
    assert c.post("/api/capture/text", json={"text": "http://192.168.1.10/private"}).status_code == 400  # private address refused
    job, kb = __import__("asyncio").run(inbox.text_or_link_job("Buy more SSDs before the price goes up"))
    assert kb == "kb-captured" and callable(job)
    monkeypatch.setattr(speech, "available", lambda: False)
    assert c.post("/api/capture/audio", files={"file": ("a.webm", b"x")}).status_code == 501
    assert "speech" in c.get("/api/capture/status").json()


def test_decode_resamples_to_16k_mono(tmp_path):
    """Any recording becomes 16 kHz mono float samples (here: 1.5 s of stereo 44.1 kHz)."""
    import math
    import struct
    import wave

    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        frames = b"".join(struct.pack("<hh", v, v) for v in (int(12000 * math.sin(i / 20)) for i in range(int(44100 * 1.5))))
        w.writeframes(frames)
    audio = speech.decode(path)
    assert audio.dtype.name == "float32" and abs(len(audio) - 24000) < 800 and 0.2 < float(abs(audio).max()) <= 1.0
    (tmp_path / "empty.wav").write_bytes(b"")
    with pytest.raises(Exception):
        speech.decode(tmp_path / "empty.wav")
