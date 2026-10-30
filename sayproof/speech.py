"""Speech to text with NVIDIA Parakeet (parakeet-tdt-0.6b-v3, 25 European languages) via parakeet-mlx,
or with ElevenLabs Scribe when ELEVENLABS_API_KEY is set.

Parakeet runs locally on Apple Silicon (`uv sync --extra speech`) and wins when installed. The hosted version has no
Parakeet, so it uses ElevenLabs. With neither, the app stays text-only.
"""

from __future__ import annotations

import asyncio
import bisect
import os
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

import httpx

PARAKEET_MODEL = os.environ.get("PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v2")
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def _parakeet_installed() -> bool:
    try:
        import parakeet_mlx  # noqa: F401
    except ImportError:
        return False
    return True


def backend() -> str | None:
    """"parakeet", "elevenlabs" or None."""
    if _parakeet_installed():
        return "parakeet"
    if os.environ.get("ELEVENLABS_API_KEY"):
        return "elevenlabs"
    return None


def available() -> bool:
    return backend() is not None


def name() -> str:
    return "ElevenLabs" if backend() == "elevenlabs" else "Parakeet"


def model_id() -> str:
    return ELEVENLABS_MODEL if backend() == "elevenlabs" else PARAKEET_MODEL


@lru_cache(maxsize=1)
def _model():
    import parakeet_mlx

    return parakeet_mlx.from_pretrained(PARAKEET_MODEL)


@dataclass
class Transcript:
    text: str
    duration: float
    # (character offset in text, start time in seconds) for every token, in order
    marks: list[tuple[int, float]]

    def time_at(self, char_pos: int | None) -> float | None:
        if char_pos is None or not self.marks:
            return None
        i = bisect.bisect_right([m[0] for m in self.marks], char_pos) - 1
        return round(self.marks[max(i, 0)][1], 1)


def build_transcript(tokens: list[Any], duration: float) -> Transcript:
    """Rebuild the text from Parakeet's tokens so every character maps back to a time."""
    text, marks = "", []
    for tok in tokens:
        marks.append((len(text), float(tok.start)))
        text += tok.text
    stripped = len(text) - len(text.lstrip())
    marks = [(max(pos - stripped, 0), t) for pos, t in marks]
    return Transcript(text=text.strip(), duration=round(duration, 1), marks=marks)


def _transcribe_sync(path: str) -> Transcript:
    result = _model().transcribe(path)
    tokens = [tok for sentence in result.sentences for tok in sentence.tokens]
    duration = result.sentences[-1].end if result.sentences else 0.0
    return build_transcript(tokens, duration)


def transcript_from_words(words: list[dict]) -> Transcript:
    """ElevenLabs returns words and the spaces between them, each with a start time."""
    tokens, last = [], 0.0
    for w in words:
        if w.get("type") == "audio_event":
            continue
        last = w["start"] if w.get("start") is not None else last
        tokens.append(SimpleNamespace(text=w["text"], start=last))
    duration = max((w["end"] for w in words if w.get("end") is not None), default=0.0)
    return build_transcript(tokens, duration)


async def _elevenlabs(data: dict, files: dict | None = None) -> Transcript:
    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(ELEVENLABS_URL, headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                              data={"model_id": ELEVENLABS_MODEL, "tag_audio_events": "false", **data},
                              files=files)
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs answered {r.status_code}: {r.text[:200]}")
    return transcript_from_words(r.json().get("words") or [])


async def _transcribe_elevenlabs(path: str) -> Transcript:
    with open(path, "rb") as f:
        return await _elevenlabs({}, {"file": (os.path.basename(path), f.read())})


async def transcribe_url(url: str) -> Transcript:
    """ElevenLabs fetches the recording itself, for files kept in Supabase Storage."""
    return await _elevenlabs({"source_url": url})


_lock = asyncio.Lock()


async def transcribe(path: str) -> Transcript:
    if backend() == "elevenlabs":
        return await _transcribe_elevenlabs(path)
    # One transcription at a time: the model is shared and uses the GPU.
    async with _lock:
        return await asyncio.to_thread(_transcribe_sync, path)
