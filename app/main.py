from __future__ import annotations

import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import settings
from .services import Job, ServiceError, inspect_video, manager
from .utils import safe_filename, segments_to_srt, segments_to_vtt, validate_youtube_url
import re

app = FastAPI(
    title="SonicScript AI",
    description="Timeline-precise, multilingual YouTube transcription",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

_metadata_cache: dict[str, tuple[float, dict]] = {}


class InspectRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)


def _get_user_id(request: Request) -> str | None:
    # Priority: header X-User-Id, then cookie ss_user_id, then query param user_id
    uid = request.headers.get("X-User-Id") or request.cookies.get("ss_user_id") or request.query_params.get("user_id")
    if uid:
        uid = uid.strip()
        # sanitize: allow 8-64 alphanumeric _ -
        if re.match(r"^[a-zA-Z0-9_-]{8,64}$", uid):
            return uid
    return None

def _require_user_id(request: Request) -> str:
    uid = _get_user_id(request)
    if not uid:
        raise HTTPException(status_code=428, detail="Missing user session. Refresh the page to get a user ID.")
    return uid


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "transcription_configured": bool(settings.transcription_api_key)}


@app.get("/api/config")
async def public_config(request: Request) -> dict:
    cookies = settings.resolve_cookies_file()
    user_id = _get_user_id(request)
    from .user_cookies import get_shared_pool_stats, get_user_status
    pool = get_shared_pool_stats()
    me = get_user_status(user_id) if user_id else None
    return {
        "ready": bool(settings.transcription_api_key),
        "refinement_ready": bool(settings.refinement_api_key),
        "max_video_duration": settings.max_video_duration,
        "denoise": settings.denoise,
        "engine": settings.transcription_model,
        "cookies_configured": bool(cookies and Path(cookies).exists()),
        "require_user_cookie": settings.require_user_cookie,
        "shared_pool": pool,
        "me": me,
    }

# --- Legacy global cookies (kept for backward compat, now secondary) ---
@app.get("/api/cookies/status")
async def cookies_status() -> dict:
    cookies = settings.resolve_cookies_file()
    has = bool(cookies and Path(cookies).exists())
    return {
        "configured": has,
        "path": cookies if has else None,
        "runtime_path": str(settings.runtime_cookies_file),
    }


@app.post("/api/cookies")
async def upload_cookies(file: UploadFile = File(...)) -> dict:
    content = await file.read()
    if len(content) < 20 or len(content) > 2_000_000:
        raise HTTPException(status_code=422, detail="Upload a valid cookies.txt file (Netscape format).")
    text = content.decode("utf-8", errors="replace")
    if "# Netscape" not in text and "youtube.com" not in text.lower() and "# HTTP Cookie File" not in text:
        raise HTTPException(status_code=422, detail="This does not look like a Netscape cookies.txt. Export it with 'Get cookies.txt LOCALLY' extension.")
    dest = settings.runtime_cookies_file
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    try:
        dest.chmod(0o600)
    except Exception:
        pass
    return {"ok": True, "path": str(dest), "size": len(content)}


@app.delete("/api/cookies")
async def delete_cookies() -> dict:
    dest = settings.runtime_cookies_file
    if dest.exists():
        dest.unlink()
    return {"ok": True}

# --- Per-user cookies (new extraordinary system) ---
@app.get("/api/me")
async def get_me(request: Request) -> dict:
    user_id = _get_user_id(request)
    if not user_id:
        # create a new one for client to adopt
        new_id = secrets.token_urlsafe(16)[:32]
        return {"user_id": new_id, "is_new": True}
    from .user_cookies import get_user_status, get_shared_pool_stats
    return {"user_id": user_id, "status": get_user_status(user_id), "pool": get_shared_pool_stats()}

@app.post("/api/me/cookies")
async def upload_my_cookies(request: Request, file: UploadFile = File(...)) -> dict:
    user_id = _require_user_id(request)
    content = await file.read()
    if len(content) < 20 or len(content) > 2_000_000:
        raise HTTPException(status_code=422, detail="Upload a valid cookies.txt (Netscape format).")
    text = content.decode("utf-8", errors="replace")
    if "# Netscape" not in text and "youtube.com" not in text.lower() and "# HTTP Cookie File" not in text:
        raise HTTPException(status_code=422, detail="Not a Netscape cookies.txt. Use 'Get cookies.txt LOCALLY' → Export → youtube.com")
    from .user_cookies import save_user_cookie, get_user_status
    save_user_cookie(user_id, content)
    return {"ok": True, "status": get_user_status(user_id)}

@app.get("/api/me/cookies/status")
async def my_cookies_status(request: Request) -> dict:
    user_id = _get_user_id(request)
    if not user_id:
        return {"has_cookie": False, "needs_upload": True, "share_enabled": False, "pool": {}}
    from .user_cookies import get_user_status, get_shared_pool_stats
    return {"status": get_user_status(user_id), "pool": get_shared_pool_stats()}

@app.post("/api/me/cookies/share")
async def set_my_share(request: Request, enabled: bool = Query(...)) -> dict:
    user_id = _require_user_id(request)
    from .user_cookies import set_share_enabled, get_user_status
    if not get_user_status(user_id)["has_cookie"]:
        raise HTTPException(status_code=422, detail="Upload your cookie first before sharing.")
    set_share_enabled(user_id, enabled)
    return {"ok": True, "status": get_user_status(user_id)}

@app.delete("/api/me/cookies")
async def delete_my_cookies(request: Request) -> dict:
    user_id = _require_user_id(request)
    from .user_cookies import delete_user_cookie, get_user_status
    delete_user_cookie(user_id)
    return {"ok": True, "status": get_user_status(user_id)}

@app.get("/api/cookies/pool")
async def pool_status() -> dict:
    from .user_cookies import get_shared_pool_stats
    return get_shared_pool_stats()

@app.post("/api/me/cookies/use-shared")
async def use_shared_cookie(request: Request) -> dict:
    """Explicit opt-in to use community pool if user has no cookie."""
    user_id = _require_user_id(request)
    from .user_cookies import is_user_cookie_valid, find_shared_cookie, get_shared_pool_stats
    if is_user_cookie_valid(user_id):
        raise HTTPException(status_code=400, detail="You already have a valid cookie — using your own.")
    shared = find_shared_cookie(exclude_user_id=user_id)
    if not shared:
        raise HTTPException(status_code=404, detail="No shared cookies available. Ask a friend to enable sharing or upload your own.")
    donor_id, path = shared
    return {"ok": True, "using": "shared", "donor_id": donor_id[:8] + "...", "pool": get_shared_pool_stats()}


@app.post("/api/videos/inspect")
async def inspect(req: InspectRequest, request: Request) -> dict:
    user_id = _get_user_id(request)
    try:
        normalized = validate_youtube_url(req.url)
        info = await run_in_threadpool(inspect_video, normalized, user_id)
    except (ValueError, ServiceError) as exc:
        # Add hint about cookie requirement
        msg = str(exc)
        if "upload your" in msg.lower() or "bot" in msg.lower():
            raise HTTPException(status_code=428, detail=msg) from exc
        raise HTTPException(status_code=422, detail=msg) from exc
    _metadata_cache[normalized] = (time.time(), info)
    _metadata_cache[info["webpage_url"]] = (time.time(), info)
    return info


async def _metadata_for(url: str, user_id: str | None) -> tuple[str, dict]:
    try:
        normalized = validate_youtube_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cached = _metadata_cache.get(normalized)
    if cached and cached[0] > time.time() - 600:
        return normalized, cached[1]
    try:
        info = await run_in_threadpool(inspect_video, normalized, user_id)
    except ServiceError as exc:
        msg = str(exc)
        if "upload your" in msg.lower() or "bot" in msg.lower():
            raise HTTPException(status_code=428, detail=msg) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _metadata_cache[normalized] = (time.time(), info)
    return normalized, info


@app.post("/api/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_job(req: CreateJobRequest, request: Request) -> dict:
    if not settings.transcription_api_key:
        raise HTTPException(
            status_code=503,
            detail="Transcription is not configured yet. The site owner must add GROQ_API_KEY on the server.",
        )
    user_id = _get_user_id(request)
    normalized, video = await _metadata_for(req.url, user_id)
    duration = float(video["duration"])
    start_seconds = min(float(req.start_seconds), duration)
    end_seconds = duration if req.end_seconds is None else min(float(req.end_seconds), duration)
    if end_seconds - start_seconds < 1:
        raise HTTPException(status_code=422, detail="Select at least one second of audio.")

    job_id = secrets.token_urlsafe(9)
    directory = settings.storage_dir / job_id
    directory.mkdir(parents=True, exist_ok=False)
    job = Job(
        id=job_id,
        url=normalized,
        video=video,
        start_seconds=round(start_seconds, 3),
        end_seconds=round(end_seconds, 3),
        directory=directory,
        owner_user_id=user_id,
    )
    manager.add(job)
    return job.public()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This transcript expired or does not exist.")
    return job.public()


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> Response:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This transcript expired or does not exist.")
    await manager.cancel(job)
    if job.refinement_task and not job.refinement_task.done():
        job.refinement_task.cancel()
    manager.jobs.pop(job_id, None)
    shutil.rmtree(job.directory, ignore_errors=True)
    return Response(status_code=204)


@app.post("/api/jobs/{job_id}/refine", status_code=202)
async def refine_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This transcript expired or does not exist.")
    if job.status != "complete" or not job.transcript:
        raise HTTPException(status_code=409, detail="Wait for the original transcript before refining it.")
    if not settings.refinement_api_key:
        raise HTTPException(status_code=503, detail="AI refinement is not configured on this server.")
    manager.refine(job)
    return job.public()


@app.get("/api/jobs/{job_id}/audio")
async def job_audio(job_id: str) -> FileResponse:
    job = manager.get(job_id)
    if not job or not job.audio_path or not job.audio_path.exists():
        raise HTTPException(status_code=404, detail="The selected audio is not available.")
    return FileResponse(
        job.audio_path,
        media_type="audio/mpeg",
        filename=f"{safe_filename(job.video['title'])}-selection.mp3",
        content_disposition_type="inline",
    )


@app.get("/api/jobs/{job_id}/download")
async def download_transcript(
    job_id: str,
    format: Literal["txt", "srt", "vtt", "json"] = Query(default="txt"),
    version: Literal["original", "refined"] = Query(default="original"),
    timeline: Literal["video", "selection"] = Query(default="video"),
) -> Response:
    job = manager.get(job_id)
    if not job or job.status != "complete":
        raise HTTPException(status_code=404, detail="The completed transcript is not available.")
    if version == "refined" and not job.refined_transcript:
        raise HTTPException(status_code=409, detail="A refined transcript is not available yet.")

    basename = safe_filename(job.video["title"])
    offset = job.start_seconds if timeline == "video" else 0
    headers = {"Content-Disposition": f'attachment; filename="{basename}.{format}"'}
    if format == "txt":
        text = job.refined_transcript if version == "refined" else job.transcript
        return Response(text or "", media_type="text/plain; charset=utf-8", headers=headers)
    if format == "srt":
        return Response(segments_to_srt(job.segments, offset), media_type="application/x-subrip; charset=utf-8", headers=headers)
    if format == "vtt":
        return Response(segments_to_vtt(job.segments, offset), media_type="text/vtt; charset=utf-8", headers=headers)

    payload = {
        "video": job.video,
        "selection": {"start": job.start_seconds, "end": job.end_seconds},
        "languages": job.languages,
        "transcript": job.refined_transcript if version == "refined" else job.transcript,
        "version": version,
        "segments": job.segments,
    }
    return Response(json.dumps(payload, ensure_ascii=False, indent=2), media_type="application/json; charset=utf-8", headers=headers)


static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
