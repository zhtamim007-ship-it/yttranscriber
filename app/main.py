from __future__ import annotations

import json
import secrets
import shutil
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import settings
from .services import Job, ServiceError, inspect_video, manager
from .utils import safe_filename, segments_to_srt, segments_to_vtt, validate_youtube_url

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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "transcription_configured": bool(settings.transcription_api_key)}


@app.get("/api/config")
async def public_config() -> dict:
    return {
        "ready": bool(settings.transcription_api_key),
        "refinement_ready": bool(settings.refinement_api_key),
        "max_video_duration": settings.max_video_duration,
        "denoise": settings.denoise,
        "engine": settings.transcription_model,
    }


@app.post("/api/videos/inspect")
async def inspect(request: InspectRequest) -> dict:
    try:
        normalized = validate_youtube_url(request.url)
        info = await run_in_threadpool(inspect_video, normalized)
    except (ValueError, ServiceError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _metadata_cache[normalized] = (time.time(), info)
    _metadata_cache[info["webpage_url"]] = (time.time(), info)
    return info


async def _metadata_for(url: str) -> tuple[str, dict]:
    try:
        normalized = validate_youtube_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cached = _metadata_cache.get(normalized)
    if cached and cached[0] > time.time() - 600:
        return normalized, cached[1]
    try:
        info = await run_in_threadpool(inspect_video, normalized)
    except ServiceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _metadata_cache[normalized] = (time.time(), info)
    return normalized, info


@app.post("/api/jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_job(request: CreateJobRequest) -> dict:
    if not settings.transcription_api_key:
        raise HTTPException(
            status_code=503,
            detail="Transcription is not configured yet. The site owner must add GROQ_API_KEY on the server.",
        )
    normalized, video = await _metadata_for(request.url)
    duration = float(video["duration"])
    start_seconds = min(float(request.start_seconds), duration)
    end_seconds = duration if request.end_seconds is None else min(float(request.end_seconds), duration)
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
