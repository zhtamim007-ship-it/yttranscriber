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
    runtime_cookies_file: Path = Path(os.getenv("YTDLP_RUNTIME_COOKIES_FILE", "/tmp/sonicscript-cookies.txt"))
    user_cookies_dir: Path = Path(os.getenv("USER_COOKIES_DIR", "/tmp/sonicscript-user-cookies"))
    require_user_cookie: bool = _as_bool("REQUIRE_USER_COOKIE", True)
    cookie_ttl_seconds: int = int(os.getenv("USER_COOKIE_TTL_SECONDS", "2592000"))

    def resolve_cookies_file(self, user_id: str | None = None) -> str | None:
        """Resolve cookie file with priority: user -> shared pool -> env -> legacy runtime."""
        if user_id:
            # lazily import to avoid circular
            from .user_cookies import get_user_cookie_path, find_shared_cookie
            p = get_user_cookie_path(user_id)
            if p:
                return str(p)
            shared = find_shared_cookie(exclude_user_id=user_id)
            if shared:
                _, sp = shared
                return str(sp)
        # fallback: explicit env file or legacy runtime single file
        if self.cookies_file:
            p = Path(self.cookies_file)
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                return self.cookies_file
        if self.runtime_cookies_file.exists() and self.runtime_cookies_file.is_file() and self.runtime_cookies_file.stat().st_size > 0:
            return str(self.runtime_cookies_file)
        # last resort: any shared pool even without user_id
        if not user_id:
            from .user_cookies import find_shared_cookie
            shared = find_shared_cookie()
            if shared:
                _, sp = shared
                return str(sp)
        return self.cookies_file

    transcription_base_url: str = os.getenv("TRANSCRIPTION_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    transcription_model: str = os.getenv("TRANSCRIPTION_MODEL", "whisper-large-v3")
    transcription_api_key: str | None = os.getenv("TRANSCRIPTION_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")
    # How many upload attempts per (model, encoding) combination before moving
    # to the next fallback. The primary combination gets the full budget;
    # fallback combinations get 2 attempts each.
    transcription_max_attempts: int = int(os.getenv("TRANSCRIPTION_MAX_ATTEMPTS", "5"))
    # Upper bound for backoff sleeps (seconds) when the provider does not send
    # a Retry-After header.
    transcription_retry_after_cap: int = int(os.getenv("TRANSCRIPTION_RETRY_AFTER_CAP_SECONDS", "45"))
    # Extra models tried only when the configured model keeps returning
    # transient server errors (HTTP 500/502/503/504), never on auth failures.
    transcription_model_fallbacks: tuple[str, ...] = tuple(
        name.strip()
        for name in os.getenv("TRANSCRIPTION_MODEL_FALLBACKS", "whisper-large-v3-turbo").split(",")
        if name.strip()
    )

    refinement_base_url: str = os.getenv("REFINEMENT_BASE_URL", os.getenv("TRANSCRIPTION_BASE_URL", "https://api.groq.com/openai/v1")).rstrip("/")
    refinement_model: str = os.getenv("REFINEMENT_MODEL", "llama-3.3-70b-versatile")
    refinement_api_key: str | None = os.getenv("REFINEMENT_API_KEY") or os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
