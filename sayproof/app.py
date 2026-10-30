"""Sayproof web app: paste a talk, get a receipt of checked claims."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import speech, storage
from .clients import JUDGE_MODEL, MissingKey, Nemotron, Tavily
from .pipeline import check_transcript, stream_transcript

load_dotenv()
STATIC = Path(__file__).parent / "static"
MAX_TEXT = 30000  # about 30 minutes of speech
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_AUDIO_SECONDS = 30 * 60
app = FastAPI(title="Sayproof")


class RateLimiter:
    """Best-effort, in-memory limits that protect the API credits of a public demo.

    Each server instance keeps its own counts, so on serverless hosting this is a speed bump, not a hard cap.
    """

    def __init__(self, per_ip: int, window_s: int, per_day: int) -> None:
        self.per_ip, self.window_s, self.per_day = per_ip, window_s, per_day
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.day, self.day_count = time.strftime("%Y-%m-%d"), 0

    def check(self, ip: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if today != self.day:
            self.day, self.day_count = today, 0
        if self.day_count >= self.per_day:
            return "The public demo has reached today's limit. Please try again tomorrow."
        q = self.hits[ip]
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.per_ip:
            wait = int(self.window_s - (now - q[0])) + 1
            return f"You've run {self.per_ip} checks in a short time. Please wait {wait} seconds."
        q.append(now)
        self.day_count += 1
        return None


limiter = RateLimiter(
    per_ip=int(os.environ.get("LIMIT_PER_IP", "6")),
    window_s=int(os.environ.get("LIMIT_WINDOW_S", "600")),
    per_day=int(os.environ.get("LIMIT_PER_DAY", "300")),
)


class CheckRequest(BaseModel):
    text: str = Field(min_length=10, max_length=MAX_TEXT)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "nebius_key": bool(os.environ.get("NEBIUS_API_KEY")),
        "tavily_key": bool(os.environ.get("TAVILY_API_KEY")),
        "model": os.environ.get("NEMOTRON_MODEL", "nvidia/nemotron-3-super-120b-a12b"),
        "speech": speech.available(),
        "asr": speech.name() if speech.available() else None,
        "storage": uploads_to_storage(),
    }


def uploads_to_storage() -> bool:
    """Recordings go through Supabase Storage when ElevenLabs transcribes and Supabase is configured."""
    return speech.backend() == "elevenlabs" and storage.available()



def lan_address() -> str | None:
    """This computer's address on the local network. Connecting a UDP socket sends nothing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def reachable(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=0.3).close()
        return True
    except OSError:
        return False


@app.get("/api/phone")
def phone(request: Request) -> dict:
    """A QR code that opens this local server on a phone in the same Wi-Fi, to upload a recording from there."""
    if not speech.available():
        raise HTTPException(status_code=404, detail="Speech input is not installed.")
    try:
        import segno
    except ImportError:
        raise HTTPException(status_code=404, detail="Install the speech extra: uv sync --extra speech")
    ip, port = lan_address(), request.url.port or 80
    if not ip or not reachable(ip, port):
        raise HTTPException(status_code=404, detail="Start the server with --host 0.0.0.0 to open it on your phone.")
    url = f"http://{ip}:{port}/"
    return {"url": url, "svg": segno.make(url, error="m").svg_inline(scale=4, dark="#16202e", light="#ffffff")}

def open_clients(request: Request) -> tuple[Nemotron, Nemotron, Tavily]:
    blocked = limiter.check(client_ip(request))
    if blocked:
        raise HTTPException(status_code=429, detail=blocked)
    try:
        return Nemotron(), Nemotron(os.environ.get("JUDGE_MODEL", JUDGE_MODEL)), Tavily()
    except MissingKey as exc:
        raise HTTPException(status_code=503, detail=str(exc))


async def run_check(text: str, request: Request) -> dict:
    model, judge, search = open_clients(request)
    started = time.perf_counter()
    try:
        receipt = await check_transcript(model, search, text, judge=judge)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"The model reply could not be read: {exc}")
    receipt["seconds"] = round(time.perf_counter() - started, 1)
    receipt["model"] = judge.model
    return receipt


@app.post("/api/check")
async def check(req: CheckRequest, request: Request) -> dict:
    return await run_check(req.text, request)


@app.post("/api/check-stream")
async def check_stream(req: CheckRequest, request: Request) -> StreamingResponse:
    """Same check as /api/check, as NDJSON: the claims first, then each verdict the moment it is ready."""
    model, judge, search = open_clients(request)

    async def lines():
        started = time.perf_counter()
        try:
            async for event in stream_transcript(model, search, req.text, judge=judge):
                if event["event"] == "done":
                    event.update(seconds=round(time.perf_counter() - started, 1), model=judge.model)
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except ValueError as exc:
            yield json.dumps({"event": "error", "detail": f"The model reply could not be read: {exc}"}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def transcribe_upload(audio: UploadFile) -> tuple[speech.Transcript, float]:
    if not speech.available():
        raise HTTPException(status_code=501, detail="Speech input is off here. "
                                                    "Paste your text instead.")
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="The recording is empty. Record again.")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="The recording is larger than 50 MB. Use a shorter clip.")
    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix) as f:
        f.write(data)
        f.flush()
        started = time.perf_counter()
        try:
            transcript = await speech.transcribe(f.name)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"{speech.name()} could not read this audio file: {exc}")
    return usable(transcript), round(time.perf_counter() - started, 1)


def usable(transcript: speech.Transcript) -> speech.Transcript:
    if transcript.duration > MAX_AUDIO_SECONDS:
        raise HTTPException(status_code=413, detail="The recording is longer than 30 minutes. Use a shorter clip.")
    if len(transcript.text) < 10:
        raise HTTPException(status_code=422, detail=f"{speech.name()} heard almost nothing. Check your microphone and record again.")
    return transcript


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)) -> dict:
    """Transcription only. The page shows the transcript, then checks it through /api/check-stream.

    `marks` pairs a character offset with its time in seconds, so the page can turn a claim's position into a time.
    """
    transcript, asr_seconds = await transcribe_upload(audio)
    return {"transcript": transcript.text, "audio_seconds": transcript.duration, "asr_seconds": asr_seconds,
            "asr_model": speech.model_id(), "marks": transcript.marks}


class UploadRequest(BaseModel):
    filename: str = Field(default="", max_length=200)
    size: int = Field(ge=1)


class StoredRequest(BaseModel):
    path: str


@app.post("/api/upload-url")
async def upload_url(req: UploadRequest) -> dict:
    """A signed URL the browser uploads the recording to, straight into the private Supabase bucket."""
    if not uploads_to_storage():
        raise HTTPException(status_code=404, detail="Uploads go to /api/transcribe here.")
    if req.size > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="The recording is larger than 50 MB. Use a shorter clip.")
    try:
        return await storage.new_upload(req.filename)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"The upload could not be prepared: {exc}")


@app.post("/api/transcribe-stored")
async def transcribe_stored(req: StoredRequest, request: Request) -> dict:
    """Like /api/transcribe, for a recording the browser already uploaded through /api/upload-url."""
    if not uploads_to_storage():
        raise HTTPException(status_code=404, detail="Uploads go to /api/transcribe here.")
    if not storage.PATH_RE.match(req.path):
        raise HTTPException(status_code=400, detail="Unknown recording.")
    blocked = limiter.check(client_ip(request))
    if blocked:
        raise HTTPException(status_code=429, detail=blocked)
    started = time.perf_counter()
    try:
        transcript = await speech.transcribe_url(await storage.download_url(req.path))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"{speech.name()} could not read this audio file: {exc}")
    transcript = usable(transcript)
    try:
        await storage.save_check(req.path, transcript.text, speech.model_id())
    except Exception:
        pass  # the transcript matters to the speaker; the log row does not
    return {"transcript": transcript.text, "audio_seconds": transcript.duration,
            "asr_seconds": round(time.perf_counter() - started, 1), "asr_model": speech.model_id(),
            "marks": transcript.marks}


@app.post("/api/check-audio")
async def check_audio(request: Request, audio: UploadFile = File(...)) -> dict:
    """Parakeet or ElevenLabs transcribes the recording, then the text is checked; each claim gets its time in the talk."""
    transcript, asr_seconds = await transcribe_upload(audio)
    receipt = await run_check(transcript.text[:MAX_TEXT], request)
    for claim in receipt["claims"]:
        claim["time"] = transcript.time_at(claim["position"])
    receipt.update(transcript=transcript.text, audio_seconds=transcript.duration, asr_seconds=asr_seconds,
                   asr_model=speech.model_id())
    return receipt
