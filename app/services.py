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


# Ordered list of YouTube player-client sets to try when inspecting or
# downloading, from most to least preferred.
#
# Ordering rationale (yt-dlp >= 2025.11 "EJS" architecture):
# - Solving YouTube's signature/n challenges requires a *supported* JavaScript
#   runtime (deno >= 2.3, node >= 22); there is no pure-Python fallback any
#   more. Without one, web/web_safari/tv/mweb/web_embedded formats are dropped
#   entirely, and android/ios formats additionally require a GVS PO token.
# - yt-dlp's built-in default client set adapts to the environment
#   (android_vr alone when no JS runtime is usable, android_vr + web_safari
#   when one is, auth-aware sets when cookies are present), so it goes FIRST.
# - The remaining sets run from least to most dependent on a JS runtime and
#   PO tokens, so a missing runtime degrades gracefully to clients that can
#   still yield playable formats instead of every rotation returning an empty
#   format list (which used to surface as "Requested format is not available").
# `None` means "let yt-dlp use its built-in default client set".
_YD_PLAYER_CLIENT_SETS: list[list[str] | None] = [
    None,                             # 0 - yt-dlp default set (runtime- and auth-aware)
    ["android_vr", "web_embedded"],   # 1 - android_vr needs no JS runtime or PO token
    ["web", "tv"],                    # 2 - need a JS runtime; tv needs no PO token
    ["android", "web"],               # 3 - GVS PO-token dependent for audio
    ["ios", "mweb"],                  # 4 - PO-token / JS-runtime dependent
    ["tv"],                           # 5 - last resort
]

# (player_client_set, format_spec) pairs tried in order when downloading audio.
# `format_spec=None` means let yt-dlp pick its default format. Same ordering
# rationale as _YD_PLAYER_CLIENT_SETS: adapt to the environment first, then
# fall back from runtime-independent to runtime/PO-token-dependent clients.
_YD_DOWNLOAD_STRATEGIES: list[tuple[list[str] | None, str | None]] = [
    (None, "bestaudio/best"),
    (None, None),
    (["android_vr", "web_embedded"], "bestaudio/best"),
    (["android_vr", "web_embedded"], None),
    (["web", "tv"], "bestaudio/best"),
    (["web", "tv"], None),
    (["android", "web"], "bestaudio/best"),
    (["android", "web"], None),
    (["ios", "mweb"], "bestaudio/best"),
    (["tv"], "bestaudio/best"),
]


def _is_bot_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        phrase in lowered
        for phrase in (
            "sign in to confirm",
            "confirm you're not a bot",
            "bot",
            "cookies",
            "use --cookies",
            "from-browser",
        )
    )


def _is_format_unavailable(message: str) -> bool:
    lowered = message.lower()
    return "requested format is not available" in lowered or "no formats" in lowered


def _is_js_runtime_missing(message: str) -> bool:
    lowered = message.lower()
    return any(
        phrase in lowered
        for phrase in (
            "javascript runtime could not be found",
            "no supported javascript runtime",
            "signature solving failed",
            "n challenge solving failed",
            "challenge solving failed",
            "challenge solver",
        )
    )


_JS_RUNTIME_MESSAGE = (
    "YouTube answered with no playable formats because this server has no supported "
    "JavaScript runtime to solve YouTube's signature challenges. The server needs "
    "Deno (>= 2.3) or Node.js (>= 22): the Docker image installs Deno through the "
    "yt-dlp[default,deno] pip extra. Redeploy with the updated image, or install one "
    "of those runtimes and restart."
)


def _base_ydl_options(
    user_id: str | None = None,
    player_client: list[str] | None = _YD_PLAYER_CLIENT_SETS[0],
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": 25,
        "retries": 2,
        # YouTube signature/n-challenge solving needs one supported JS runtime.
        # Enable both in yt-dlp's preference order: deno first (shipped via the
        # yt-dlp[default,deno] extra), then node (used when Node.js >= 22 is
        # installed, e.g. locally). Passing only {"node": {}} disabled deno and
        # left servers with no usable solver -> every client returned zero
        # formats -> "Requested format is not available".
        "js_runtimes": {"deno": {}, "node": {}},
    }
    # Only pin player clients explicitly; `player_client=None` keeps yt-dlp's
    # built-in default set (which itself covers many clients).
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": player_client}}
    cookies, _ = _cookies_file(user_id)
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def _handle_inspect_error(exc: DownloadError) -> ServiceError:
    message = str(exc).replace("ERROR: ", "").strip()
    lowered = message.lower()
    if _is_bot_error(message):
        return ServiceError(
            "YouTube blocked this request from the server (bot check). "
            "YouTube often blocks cloud servers like Render. No code fix can permanently avoid it. "
            "Quick fix: click 'Fix with cookies' below, install the free 'Get cookies.txt LOCALLY' extension, export cookies.txt, upload it, and try again. "
            "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
        )
    if _is_js_runtime_missing(message):
        return ServiceError(_JS_RUNTIME_MESSAGE)
    if _is_format_unavailable(message):
        return ServiceError(
            "YouTube could not open this video. No playable format was found for it. "
            "This usually means the video is private, region- or age-restricted, DRM-protected, "
            "members-only, removed, or otherwise blocked to this server. Confirm the video is "
            "public and playable, then try again or use a different video."
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
    # Track which cookie we tried for expiry marking
    cookies_tried, source = _cookies_file(user_id)
    info: dict[str, Any] | None = None
    last_error: DownloadError | None = None
    # Rotate through player-client sets. Different clients can expose different
    # format lists, and some (notably "tv") sidestep YouTube's bot-check more
    # often, so a video that one client rejects may work with the next.
    for player_client in _YD_PLAYER_CLIENT_SETS:
        options = _base_ydl_options(user_id=user_id, player_client=player_client)
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                candidate = downloader.extract_info(url, download=False)
        except DownloadError as exc:
            msg = str(exc).lower()
            # If we got a bot error and we used a user cookie, mark it expired so the UI prompts a renew.
            if _is_bot_error(str(exc)) and cookies_tried and source == "own" and user_id:
                try:
                    from .user_cookies import mark_expired
                    mark_expired(user_id)
                except Exception:
                    pass
            last_error = exc
            # Bot/verification errors won't be fixed by a different client, so fail fast.
            if _is_bot_error(str(exc)):
                raise _handle_inspect_error(exc) from exc
            continue  # otherwise try the next client set
        except Exception as exc:
            raise ServiceError("YouTube metadata could not be loaded. Check the link and try again.") from exc

        if candidate and candidate.get("entries"):
            candidate = next((entry for entry in candidate["entries"] if entry), None)
        if not candidate:
            last_error = DownloadError("ERROR: No video was found at that link.")
            continue
        # This client answered, but returned no playable formats -> another client
        # may be able to play it, so keep rotating.
        formats = candidate.get("formats")
        if formats is not None and not formats:
            last_error = DownloadError(
                f"ERROR: [youtube] {candidate.get('id')}: Requested format is not available. "
                "Use --list-formats for a list of available formats"
            )
            continue
        info = candidate
        break

    if not info:
        if last_error is not None:
            raise _handle_inspect_error(last_error) from last_error
        raise ServiceError("No video was found at that link.")

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

        def _build_command(player_client: list[str] | None, format_spec: str | None) -> list[str]:
            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--no-playlist", "--no-warnings", "--newline",
                # The CLI enables deno by default; this flag additionally enables
                # node (used when Node.js >= 22 is installed). Do not pass
                # --no-js-runtimes: deno must stay enabled for YouTube signature
                # solving (it is provided by the yt-dlp[default,deno] extra).
                "--js-runtimes", "node",
                "--retries", "3", "--fragment-retries", "3",
            ]
            if player_client:
                cmd.extend(["--extractor-args", f"youtube:player_client={','.join(player_client)}"])
            if format_spec:
                cmd.extend(["-f", format_spec])
            cmd.extend(["-o", output, *section_args, *command_suffix])
            return cmd

        # Try yt-dlp's environment-adaptive default client set first (it picks
        # clients that work with the available JS runtime / auth state), then
        # fall back through client sets ordered from runtime-independent to
        # runtime/PO-token-dependent, each with efficient audio-only selection
        # before the default format. Different clients expose different format
        # lists, so rotating through them recovers most "Requested format is
        # not available" cases instead of giving up after the first failure.
        last_error_detail = ""
        attempted = 0
        for player_client, format_spec in _YD_DOWNLOAD_STRATEGIES:
            attempted += 1
            if attempted > 1:
                # Clean previous partial/complete download before retrying so the
                # candidate-file selection below never picks up a stale attempt.
                for stale in job.directory.glob("source.*"):
                    stale.unlink(missing_ok=True)
            command = _build_command(player_client, format_spec)
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
            if _is_bot_error(detail):
                raise ServiceError(
                    "YouTube requested verification from this server (bot check). "
                    "Upload a fresh cookies.txt via 'Fix with cookies' or set YTDLP_COOKIES_FILE. "
                    "See https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp"
                )
            last_error_detail = detail
            # For format errors, keep trying the remaining strategies.
            # For other errors, keep the detail but still try other clients too,
            # since a different client may succeed where this one failed.
        else:
            # All strategies exhausted
            if last_error_detail:
                if _is_js_runtime_missing(last_error_detail):
                    raise ServiceError(_JS_RUNTIME_MESSAGE)
                if _is_format_unavailable(last_error_detail):
                    raise ServiceError(
                        "YouTube could not open this video. No playable audio format was available. "
                        "This usually means the video is private, region/age-restricted, DRM-protected, "
                        "members-only, removed, or otherwise blocked to this server. Confirm the video "
                        "is public and playable, then try again or use a different video."
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
