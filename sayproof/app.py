"""Sayproof web app: paste a talk, get a receipt of checked claims."""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .clients import MissingKey, Nemotron, Tavily
from .pipeline import check_transcript

load_dotenv()
STATIC = Path(__file__).parent / "static"
MAX_TEXT = 3000
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
    }


@app.post("/api/check")
async def check(req: CheckRequest, request: Request) -> dict:
    blocked = limiter.check(client_ip(request))
    if blocked:
        raise HTTPException(status_code=429, detail=blocked)
    try:
        model, search = Nemotron(), Tavily()
    except MissingKey as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    started = time.perf_counter()
    try:
        receipt = await check_transcript(model, search, req.text)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"The model reply could not be read: {exc}")
    receipt["seconds"] = round(time.perf_counter() - started, 1)
    receipt["model"] = model.model
    return receipt
