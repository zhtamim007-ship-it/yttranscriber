from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "SonicScript AI"
    storage_dir: Path = Path(os.getenv("JOB_STORAGE_DIR", "/tmp/sonicscript-jobs"))
    job_ttl_seconds: int = int(os.getenv("JOB_TTL_SECONDS", "14400"))
    max_video_duration: int = int(os.getenv("MAX_VIDEO_DURATION_SECONDS", "21600"))
    max_concurrent_jobs: int = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
    chunk_seconds: int = int(os.getenv("TRANSCRIPTION_CHUNK_SECONDS", "600"))
    denoise: bool = _as_bool("ENABLE_DENOISE", True)
    cookies_file: str | None = os.getenv("YTDLP_COOKIES_FILE")

    transcription_base_url: str = os.getenv("TRANSCRIPTION_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    transcription_model: str = os.getenv("TRANSCRIPTION_MODEL", "whisper-large-v3")
    transcription_api_key: str | None = os.getenv("TRANSCRIPTION_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")

    refinement_base_url: str = os.getenv("REFINEMENT_BASE_URL", os.getenv("TRANSCRIPTION_BASE_URL", "https://api.groq.com/openai/v1")).rstrip("/")
    refinement_model: str = os.getenv("REFINEMENT_MODEL", "llama-3.3-70b-versatile")
    refinement_api_key: str | None = os.getenv("REFINEMENT_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
