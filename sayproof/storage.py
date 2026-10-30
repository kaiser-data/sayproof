"""Recordings in Supabase Storage, so the hosted version can take files larger than Vercel's 4.5 MB request limit.

The browser uploads straight to a private bucket through a signed upload URL. ElevenLabs then fetches the file
through a signed download URL. Only the server holds SUPABASE_SECRET_KEY; the bucket and the checks table
have no public access.
"""

from __future__ import annotations

import os
import re
import uuid

import httpx

BUCKET = "recordings"
# Paths are made by new_upload, so anything else sent back by a browser is rejected.
PATH_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{1,5}$")


def available() -> bool:
    return bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SECRET_KEY"))


def _base() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/")


def _headers() -> dict[str, str]:
    key = os.environ["SUPABASE_SECRET_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}"}


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{_base()}{path}", headers=_headers(), json=body)
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase answered {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


async def new_upload(filename: str) -> dict:
    """A fresh object path and a signed URL the browser PUTs the file to."""
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    ext = ext if re.fullmatch(r"[a-z0-9]{1,5}", ext) else "webm"
    path = f"{uuid.uuid4().hex}.{ext}"
    signed = await _post(f"/storage/v1/object/upload/sign/{BUCKET}/{path}", {})
    return {"path": path, "upload_url": f"{_base()}/storage/v1{signed['url']}"}


async def download_url(path: str, expires_s: int = 600) -> str:
    signed = await _post(f"/storage/v1/object/sign/{BUCKET}/{path}", {"expiresIn": expires_s})
    return f"{_base()}/storage/v1{signed['signedURL']}"


async def save_check(audio_path: str | None, transcript: str, asr_model: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{_base()}/rest/v1/checks", headers={**_headers(), "Prefer": "return=minimal"},
                              json={"audio_path": audio_path, "transcript": transcript, "asr_model": asr_model})
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase answered {r.status_code}: {r.text[:200]}")
