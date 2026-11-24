"""Speech to text on this computer, for voice notes and the microphone buttons.

faster-whisper runs Whisper on the CPU (or an NVIDIA GPU when SPEECH_DEVICE=cuda). Audio never leaves the machine.
The model (CTranslate2 weights, not code) is downloaded once from Hugging Face into <data>/models/whisper the first time
it's needed: "base" is about 145 MB and good for most speech; SPEECH_MODEL=small is more accurate and slower.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # Windows without Developer Mode: plain copies are fine
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)
SIZES = ("tiny", "base", "small", "medium")
MAX_SECONDS = 15 * 60
_lock = threading.Lock()
_model = None


class SpeechError(RuntimeError):
    pass


def model_size() -> str:
    size = os.getenv("SPEECH_MODEL", "base")
    return size if size in SIZES else "base"


def model_dir() -> Path:
    return get_settings().data_dir / "models" / "whisper"


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - not installed in this build
        return False


def downloaded() -> bool:
    return any(model_dir().glob(f"models--Systran--faster-whisper-{model_size()}/snapshots/*/model.bin"))


def status() -> dict:
    return {"available": available(), "model": model_size(), "downloaded": downloaded() if available() else False}


def _load():
    global _model
    if _model is None:
        if not available():
            raise SpeechError("Voice notes need the speech module, which this copy of KeiroLake doesn't include")
        from faster_whisper import WhisperModel

        device = os.getenv("SPEECH_DEVICE", "cpu")
        model_dir().mkdir(parents=True, exist_ok=True)
        _model = WhisperModel(model_size(), device=device, compute_type="int8" if device == "cpu" else "float16",
                              download_root=str(model_dir()))
    return _model


def decode(path: Path):
    """16 kHz mono samples (float32, -1..1), the input Whisper expects, from any common audio file.

    Decoded here with PyAV instead of faster-whisper's own helper, which breaks when PyAV changes its options."""
    import av
    import numpy as np

    chunks = []
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise SpeechError("The file has no sound in it")
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
                if sum(len(c) for c in chunks) > MAX_SECONDS * 16000:
                    break
        for out in resampler.resample(None):  # flush what the resampler still holds
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise SpeechError("The recording is empty")
    return (np.concatenate(chunks).astype(np.float32) / 32768.0)[: MAX_SECONDS * 16000]


def _transcribe(path: Path, language: str | None) -> dict:
    audio = decode(path)
    with _lock:  # one at a time: the model is large and already uses every core
        model = _load()
        segments, info = model.transcribe(audio, language=language, vad_filter=True, beam_size=5)
        parts, seconds = [], 0.0
        for seg in segments:
            if seg.end > MAX_SECONDS:
                break
            parts.append(seg.text.strip())
            seconds = seg.end
    text = " ".join(p for p in parts if p).strip()
    return {"text": text, "language": info.language, "seconds": round(seconds, 1)}


async def transcribe(path: Path, language: str | None = None, progress=None) -> dict:
    """Text of a recording (webm, ogg, m4a, mp3, wav...). Downloads the model the first time."""
    if progress and not downloaded():
        await progress(0.1, f"Downloading the speech model ({model_size()}, first time only)")
    elif progress:
        await progress(0.2, "Listening")
    try:
        return await asyncio.to_thread(_transcribe, path, language)
    except SpeechError:
        raise
    except Exception as exc:  # noqa: BLE001 - unreadable audio, no network for the first download...
        log.warning("Transcription failed: %s", exc)
        raise SpeechError(f"Couldn't turn the recording into text: {exc.__class__.__name__}") from exc
