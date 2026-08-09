from __future__ import annotations

import asyncio
import math
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yt_dlp
from yt_dlp.utils import DownloadError

from .config import settings
from .utils import clock, language_name, validate_youtube_url


class ServiceError(RuntimeError):
    """An error safe to show to the user."""


@dataclass
class Job:
    id: str
    url: str
    video: dict[str, Any]
    start_seconds: float
    end_seconds: float
    directory: Path
    owner_user_id: str | None = None
    cookie_source: str | None = None  # 'own' | 'shared' | 'global' | None
    status: str = "queued"
    stage: str = "Waiting for a transcription slot"
    progress: float = 2
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)
    transcript: str = ""
    languages: list[dict[str, Any]] = field(default_factory=list)
    audio_path: Path | None = None
    refined_transcript: str | None = None
    refinement_status: str = "idle"
    refinement_progress: float = 0
    refinement_error: str | None = None
    cancel_requested: bool = False
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    refinement_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def selected_duration(self) -> float:
        return self.end_seconds - self.start_seconds

    def touch(self, *, status: str | None = None, stage: str | None = None, progress: float | None = None) -> None:
        if status is not None:
            self.status = status
        if stage is not None:
            self.stage = stage
        if progress is not None:
            self.progress = max(0, min(100, round(progress, 1)))
        self.updated_at = time.time()

    def public(self) -> dict[str, Any]:
        segments = [
            {
                **segment,
                "video_start": round(self.start_seconds + float(segment.get("start", 0)), 3),
                "video_end": round(self.start_seconds + float(segment.get("end", 0)), 3),
            }
            for segment in self.segments
        ]
        words = len(self.transcript.split()) if self.transcript else 0
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "error": self.error,
            "video": self.video,
            "selection": {
                "start": self.start_seconds,
                "end": self.end_seconds,
                "duration": self.selected_duration,
            },
            "transcript": self.transcript,
            "segments": segments,
            "languages": self.languages,
            "word_count": words,
            "audio_url": f"/api/jobs/{self.id}/audio" if self.audio_path and self.audio_path.exists() else None,
            "refined_transcript": self.refined_transcript,
            "refinement": {
                "status": self.refinement_status,
                "progress": self.refinement_progress,
                "error": self.refinement_error,
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _cookies_file(user_id: str | None = None) -> tuple[str | None, str | None]:
    """Return (path, source) for given user_id."""
    resolved = settings.resolve_cookies_file(user_id=user_id)
    if not resolved:
        return None, None
    # Determine source by checking user store
    if user_id:
        try:
            from .user_cookies import get_user_cookie_path, find_shared_cookie
            own = get_user_cookie_path(user_id)
            if own and str(own) == resolved:
                return resolved, "own"
            shared = find_shared_cookie(exclude_user_id=user_id)
            if shared and str(shared[1]) == resolved:
                return resolved, "shared"
        except Exception:
            pass
    # fallback global
    if resolved == settings.cookies_file or resolved == str(settings.runtime_cookies_file):
        return resolved, "global"
    return resolved, "shared"


def _base_ydl_options(user_id: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 25,
        "retries": 2,
        "js_runtimes": {"node": {}},
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    cookies, _ = _cookies_file(user_id)
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _handle_inspect_error(exc: DownloadError) -> ServiceError:
    message = str(exc).replace("ERROR: ", "").strip()
    lowered = message.lower()
    if any(
        phrase in lowered
        for phrase in (
            "sign in to confirm",
            "confirm you're not a bot",
            "bot",
            "cookies",
            "use --cookies",
            "from-browser",
        )
    ):
        return ServiceError(
            "YouTube blocked this request from the server (bot check). "
            "YouTube often blocks cloud servers like Render. No code fix can permanently avoid it. "
            "Quick fix: click 'Fix with cookies' below, install the free 'Get cookies.txt LOCALLY' extension, export cookies.txt, upload it, and try again. "
            "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
        )
    return ServiceError(f"YouTube could not open this video. {message[-400:]}")


def inspect_video(url: str, user_id: str | None = None) -> dict[str, Any]:
    url = validate_youtube_url(url)
    # Enforce per-user cookie if required
    if settings.require_user_cookie and user_id:
        from .user_cookies import is_user_cookie_valid, find_shared_cookie
        own_valid = is_user_cookie_valid(user_id)
        has_shared = find_shared_cookie(exclude_user_id=user_id) is not None
        has_global = bool(settings.resolve_cookies_file(user_id=None))
        if not own_valid and not has_shared and not has_global:
            raise ServiceError(
                "You need to upload your YouTube cookie first. Click 'Upload My Cookie' below. "
                "You can also choose 'Use Community Cookie' if someone shared theirs."
            )
        if not own_valid and not has_shared and has_global:
            # allow global fallback silently
            pass
    options = _base_ydl_options(user_id=user_id)
    info: dict[str, Any] | None = None
    # Track which cookie we tried for expiry marking
    cookies_tried, source = _cookies_file(user_id)
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
        # success: mark cookie used
        if cookies_tried and source in {"own", "shared"} and user_id:
            try:
                from .user_cookies import mark_used
                # if shared, mark donor as used
                if source == "shared":
                    from .user_cookies import find_shared_cookie
                    shared = find_shared_cookie(exclude_user_id=user_id)
                    if shared:
                        mark_used(shared[0])
                else:
                    mark_used(user_id)
            except Exception:
                pass
    except DownloadError as exc:
        msg = str(exc).lower()
        # If we got a bot error and we used a user cookie, mark it expired so UI prompts renew
        if ("sign in" in msg or "bot" in msg) and cookies_tried and source == "own" and user_id:
            try:
                from .user_cookies import mark_expired
                mark_expired(user_id)
            except Exception:
                pass
        if "sign in" in msg or "bot" in msg:
            try:
                alt_opts = dict(options)
                alt_opts["extractor_args"] = {"youtube": {"player_client": ["android"]}}
                with yt_dlp.YoutubeDL(alt_opts) as downloader:
                    info = downloader.extract_info(url, download=False)
            except DownloadError as exc2:
                raise _handle_inspect_error(exc2) from exc2
            except Exception as exc2:
                raise _handle_inspect_error(exc) from exc2
            # if retry succeeded, fall through to processing
        else:
            raise _handle_inspect_error(exc) from exc
    except Exception as exc:
        raise ServiceError("YouTube metadata could not be loaded. Check the link and try again.") from exc

    if info and info.get("entries"):
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        raise ServiceError("No video was found at that link.")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ServiceError("Live and upcoming streams cannot be transcribed until they end.")

    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise ServiceError("YouTube did not report a duration for this video.")
    if duration > settings.max_video_duration:
        limit = clock(settings.max_video_duration)
        raise ServiceError(f"This server accepts videos up to {limit} long.")

    thumbnails = info.get("thumbnails") or []
    thumbnail = info.get("thumbnail")
    if thumbnails:
        thumbnail = max(thumbnails, key=lambda item: (item.get("width") or 0) * (item.get("height") or 0)).get("url") or thumbnail
    return {
        "id": str(info.get("id") or ""),
        "title": info.get("title") or "Untitled YouTube video",
        "channel": info.get("channel") or info.get("uploader") or "YouTube",
        "duration": round(duration, 3),
        "duration_label": clock(duration),
        "thumbnail": thumbnail,
        "webpage_url": info.get("webpage_url") or url,
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
    }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

    def get(self, job_id: str) -> Job | None:
        self.cleanup_expired()
        return self.jobs.get(job_id)

    def add(self, job: Job) -> Job:
        self.cleanup_expired()
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job), name=f"transcribe-{job.id}")
        return job

    def cleanup_expired(self) -> None:
        cutoff = time.time() - settings.job_ttl_seconds
        for job_id, job in list(self.jobs.items()):
            if job.updated_at >= cutoff or (job.task and not job.task.done()):
                continue
            shutil.rmtree(job.directory, ignore_errors=True)
            self.jobs.pop(job_id, None)

    async def cancel(self, job: Job) -> None:
        job.cancel_requested = True
        if job.process and job.process.returncode is None:
            job.process.terminate()
            try:
                await asyncio.wait_for(job.process.wait(), timeout=4)
            except asyncio.TimeoutError:
                job.process.kill()
        if job.status not in {"complete", "failed"}:
            job.touch(status="cancelled", stage="Cancelled", progress=job.progress)

    async def _run(self, job: Job) -> None:
        try:
            async with self.semaphore:
                if job.cancel_requested:
                    return
                if not settings.transcription_api_key:
                    raise ServiceError("Transcription is not configured. Add GROQ_API_KEY to the server environment, then try again.")
                job.touch(status="downloading", stage="Fetching the selected audio from YouTube", progress=5)
                await self._download_audio(job)
                self._check_cancelled(job)
                job.touch(status="processing", stage="Optimizing speech for noisy audio", progress=30)
                await self._transcribe(job)
                self._check_cancelled(job)
                job.touch(status="complete", stage="Transcript ready", progress=100)
        except asyncio.CancelledError:
            job.touch(status="cancelled", stage="Cancelled")
            raise
        except ServiceError as exc:
            job.error = str(exc)
            job.touch(status="failed", stage="Transcription stopped")
        except Exception as exc:  # noqa: BLE001 - task boundary must record failures
            print(f"Unhandled job error {job.id}: {exc!r}", flush=True)
            job.error = "An unexpected processing error occurred. Please retry this selection."
            job.touch(status="failed", stage="Transcription stopped")
        finally:
            job.process = None

    @staticmethod
    def _check_cancelled(job: Job) -> None:
        if job.cancel_requested:
            raise asyncio.CancelledError

    async def _download_audio(self, job: Job) -> None:
        output = str(job.directory / "source.%(ext)s")
        base_command = [
            sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--no-warnings", "--newline",
            "--js-runtimes", "node",
            "--retries", "3", "--fragment-retries", "3",
            "--extractor-args", "youtube:player_client=android,web",
        ]
        is_full = job.start_seconds <= 0.05 and abs(job.end_seconds - float(job.video["duration"])) <= 0.75
        section_args: list[str] = []
        if not is_full:
            section = f"*{clock(job.start_seconds, milliseconds=True)}-{clock(job.end_seconds, milliseconds=True)}"
            section_args = ["--download-sections", section]
        cookies, source = _cookies_file(job.owner_user_id)
        if cookies:
            job.cookie_source = source
        command_suffix = []
        if cookies:
            command_suffix.extend(["--cookies", cookies])
        command_suffix.append(job.url)

        # Try efficient audio-only first; if format unavailable, fall back to default
        attempts = [
            [*base_command, "-f", "bestaudio/best", "-o", output, *section_args, *command_suffix],
            [*base_command, "-o", output, *section_args, *command_suffix],
        ]
        last_error_detail = ""
        for attempt_index, command in enumerate(attempts):
            if attempt_index > 0:
                # Clean previous partial download on retry
                for partial in job.directory.glob("source.*.part"):
                    partial.unlink(missing_ok=True)
            job.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert job.process.stdout
            recent_output: list[str] = []
            while True:
                line_bytes = await job.process.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                recent_output.append(line)
                recent_output = recent_output[-12:]
                if "%" in line:
                    try:
                        percent_text = line.split("%", 1)[0].split()[-1]
                        percent = float(percent_text)
                        job.touch(progress=5 + min(percent, 100) * 0.18)
                    except (ValueError, IndexError):
                        pass
                self._check_cancelled(job)
            return_code = await job.process.wait()
            if return_code == 0:
                break
            detail = " ".join(recent_output)[-500:]
            lowered_detail = detail.lower()
            if "requested format is not available" in lowered_detail:
                last_error_detail = detail
                continue  # retry with default format
            if "sign in" in lowered_detail or "bot" in lowered_detail or "cookies" in lowered_detail:
                raise ServiceError(
                    "YouTube requested verification from this server (bot check). "
                    "Upload a fresh cookies.txt via 'Fix with cookies' or set YTDLP_COOKIES_FILE. "
                    "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
                )
            last_error_detail = detail
        else:
            # All attempts exhausted
            if last_error_detail:
                lowered_detail = last_error_detail.lower()
                if "requested format is not available" in lowered_detail:
                    raise ServiceError(
                        "YouTube could not open this video. The requested format was not available. "
                        "This usually means the video has restricted or unusual format settings. "
                        "Try selecting the full video instead of a partial clip, or try a different video."
                    )
                raise ServiceError(f"The video audio could not be downloaded. {last_error_detail}")
            raise ServiceError("The video audio could not be downloaded.")

        candidates = [
            path for path in job.directory.glob("source.*")
            if path.is_file() and path.suffix not in {".part", ".ytdl", ".json"}
        ]
        if not candidates:
            raise ServiceError("The audio download finished without a usable file.")
        source = max(candidates, key=lambda path: path.stat().st_size)
        selected = job.directory / "selected-audio.mp3"
        await self._run_command([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-t", str(job.selected_duration),
            "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "16000",
            "-b:a", "64k", str(selected),
        ], job, "Audio conversion failed")
        if not selected.exists() or selected.stat().st_size < 1000:
            raise ServiceError("The selected timeline did not contain usable audio.")
        job.audio_path = selected
        if source != selected:
            source.unlink(missing_ok=True)
        job.touch(progress=30)

    async def _run_command(self, command: list[str], job: Job, error_label: str) -> str:
        job.process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await job.process.communicate()
        self._check_cancelled(job)
        if job.process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-500:]
            raise ServiceError(f"{error_label}. {detail}")
        return stdout.decode("utf-8", errors="replace").strip()

    async def _audio_duration(self, path: Path, job: Job) -> float:
        output = await self._run_command([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ], job, "Could not inspect the prepared audio")
        try:
            return float(output)
        except ValueError as exc:
            raise ServiceError("Could not determine the prepared audio duration.") from exc

    async def _make_chunk(self, job: Job, index: int, desired_start: float, duration: float) -> tuple[Path, float]:
        overlap = 2.0 if index else 0.0
        actual_start = max(0.0, desired_start - overlap)
        actual_duration = duration + (desired_start - actual_start)
        output = job.directory / f"speech-{index:04d}.mp3"
        filters = "highpass=f=60,lowpass=f=7800"
        if settings.denoise:
            filters += ",afftdn=nr=10:nf=-35,dynaudnorm=f=150:g=12"
        await self._run_command([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(actual_start), "-i", str(job.audio_path),
            "-t", str(actual_duration), "-vn", "-ac", "1", "-ar", "16000",
            "-af", filters, "-b:a", "64k", str(output),
        ], job, "Speech optimization failed")
        return output, actual_start

    async def _transcribe(self, job: Job) -> None:
        assert job.audio_path
        audio_duration = await self._audio_duration(job.audio_path, job)
        if audio_duration < 0.2:
            raise ServiceError("The selected timeline is too short to transcribe.")
        chunk_length = max(60, settings.chunk_seconds)
        chunk_count = max(1, math.ceil(audio_duration / chunk_length))
        all_segments: list[dict[str, Any]] = []
        fallback_texts: list[str] = []
        language_counts: dict[str, int] = {}

        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30)) as client:
            for index in range(chunk_count):
                self._check_cancelled(job)
                desired_start = index * chunk_length
                duration = min(chunk_length, audio_duration - desired_start)
                job.touch(
                    status="transcribing",
                    stage=f"Listening carefully · part {index + 1} of {chunk_count}",
                    progress=35 + (index / chunk_count) * 57,
                )
                chunk_path, chunk_offset = await self._make_chunk(job, index, desired_start, duration)
                data = await self._request_transcription(client, chunk_path)
                chunk_path.unlink(missing_ok=True)

                language = str(data.get("language") or "unknown").strip()
                language_counts[language] = language_counts.get(language, 0) + 1
                text = str(data.get("text") or "").strip()
                if text:
                    fallback_texts.append(text)
                api_segments = data.get("segments") or []
                for api_segment in api_segments:
                    segment_text = str(api_segment.get("text") or "").strip()
                    if not segment_text:
                        continue
                    start = chunk_offset + float(api_segment.get("start") or 0)
                    end = chunk_offset + float(api_segment.get("end") or start + 0.1)
                    if index and (start + end) / 2 < desired_start:
                        continue
                    all_segments.append({
                        "start": round(max(0, start), 3),
                        "end": round(min(audio_duration, max(start + 0.05, end)), 3),
                        "text": segment_text,
                    })
                job.touch(progress=35 + ((index + 1) / chunk_count) * 57)

        if not all_segments and fallback_texts:
            cursor = 0.0
            per_chunk = audio_duration / len(fallback_texts)
            for text in fallback_texts:
                all_segments.append({"start": round(cursor, 3), "end": round(min(audio_duration, cursor + per_chunk), 3), "text": text})
                cursor += per_chunk
        if not all_segments and not fallback_texts:
            raise ServiceError("No speech was detected in this selection. Try a longer range or a section with clearer speech.")

        all_segments.sort(key=lambda item: item["start"])
        job.segments = all_segments
        job.transcript = " ".join(segment["text"] for segment in all_segments).strip() or " ".join(fallback_texts)
        job.languages = [
            {"code": code, "name": language_name(code), "parts": count}
            for code, count in sorted(language_counts.items(), key=lambda item: item[1], reverse=True)
            if code != "unknown"
        ] or [{"code": "auto", "name": "Auto-detected", "parts": chunk_count}]
        job.touch(status="processing", stage="Aligning timestamps and building your transcript", progress=96)

    async def _request_transcription(self, client: httpx.AsyncClient, path: Path) -> dict[str, Any]:
        url = f"{settings.transcription_base_url}/audio/transcriptions"
        last_error = ""
        for attempt in range(4):
            try:
                with path.open("rb") as audio:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {settings.transcription_api_key}"},
                        data={
                            "model": settings.transcription_model,
                            "response_format": "verbose_json",
                            "temperature": "0",
                        },
                        files={"file": (path.name, audio, "audio/mpeg")},
                    )
                if response.status_code < 400:
                    return response.json()
                try:
                    payload = response.json()
                    last_error = payload.get("error", {}).get("message") or response.text
                except (ValueError, AttributeError):
                    last_error = response.text
                if response.status_code not in {408, 429, 500, 502, 503, 504}:
                    break
                retry_after = float(response.headers.get("retry-after", 0) or 0)
                await asyncio.sleep(max(retry_after, 2 ** attempt))
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = str(exc)
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
        if "api key" in last_error.lower() or "authentication" in last_error.lower():
            raise ServiceError("The transcription API key was rejected. Update GROQ_API_KEY on the server.")
        raise ServiceError(f"The speech model could not process this audio. {last_error[:300]}")

    def refine(self, job: Job) -> None:
        if job.refinement_task and not job.refinement_task.done():
            return
        job.refinement_error = None
        job.refinement_status = "queued"
        job.refinement_progress = 2
        job.refinement_task = asyncio.create_task(self._refine(job), name=f"refine-{job.id}")

    async def _refine(self, job: Job) -> None:
        try:
            if not settings.refinement_api_key:
                raise ServiceError("AI refinement is not configured. Add GROQ_API_KEY to the server environment.")
            chunks = self._text_chunks(job.transcript)
            refined: list[str] = []
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as client:
                for index, text in enumerate(chunks):
                    job.refinement_status = "refining"
                    job.refinement_progress = round(5 + (index / len(chunks)) * 90, 1)
                    refined.append(await self._request_refinement(client, text))
            job.refined_transcript = "\n\n".join(part.strip() for part in refined if part.strip())
            job.refinement_status = "complete"
            job.refinement_progress = 100
            job.updated_at = time.time()
        except ServiceError as exc:
            job.refinement_error = str(exc)
            job.refinement_status = "failed"
        except Exception as exc:  # noqa: BLE001 - task boundary must preserve original
            print(f"Unhandled refinement error {job.id}: {exc!r}", flush=True)
            job.refinement_error = "AI refinement failed unexpectedly. The original transcript is unchanged."
            job.refinement_status = "failed"

    @staticmethod
    def _text_chunks(text: str, limit: int = 9000) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text.strip()
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split = remaining.rfind(". ", 0, limit)
            if split < limit // 2:
                split = remaining.rfind(" ", 0, limit)
            split = split + 1 if split > 0 else limit
            chunks.append(remaining[:split].strip())
            remaining = remaining[split:].strip()
        return chunks

    async def _request_refinement(self, client: httpx.AsyncClient, text: str) -> str:
        response = await client.post(
            f"{settings.refinement_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.refinement_api_key}"},
            json={
                "model": settings.refinement_model,
                "temperature": 0.1,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a forensic transcript editor. Correct only obvious speech-recognition spelling, "
                            "word-boundary, capitalization, and punctuation errors. Preserve every fact, repetition, "
                            "hesitation, tone, and the speaker's meaning. Never summarize, censor, embellish, or translate. "
                            "The text may switch between Bangla, English, and other languages; keep every passage in its "
                            "original language and script. Do not add headings, notes, or speaker names. Return only the "
                            "corrected transcript. If uncertain, retain the original wording."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
            },
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message") or response.text
            except (ValueError, AttributeError):
                detail = response.text
            if response.status_code in {401, 403}:
                raise ServiceError("The refinement API key was rejected. Update GROQ_API_KEY on the server.")
            raise ServiceError(f"The refinement model could not complete the edit. {detail[:300]}")
        try:
            return response.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ServiceError("The refinement model returned an unexpected response.") from exc


manager = JobManager()
