"""Speech to text with NVIDIA Parakeet (parakeet-tdt-0.6b-v3, 25 European languages) via parakeet-mlx.

Runs locally on Apple Silicon. Install with `uv sync --extra speech`. Without it, the app stays text-only.
"""

from __future__ import annotations

import asyncio
import bisect
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

MODEL_ID = os.environ.get("PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v3")


def available() -> bool:
    try:
        import parakeet_mlx  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _model():
    import parakeet_mlx

    return parakeet_mlx.from_pretrained(MODEL_ID)


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


_lock = asyncio.Lock()


async def transcribe(path: str) -> Transcript:
    # One transcription at a time: the model is shared and uses the GPU.
    async with _lock:
        return await asyncio.to_thread(_transcribe_sync, path)
