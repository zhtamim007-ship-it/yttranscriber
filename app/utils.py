from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

LANGUAGE_NAMES = {
    "af": "Afrikaans", "ar": "Arabic", "az": "Azerbaijani", "be": "Belarusian",
    "bg": "Bulgarian", "bn": "Bangla", "bs": "Bosnian", "ca": "Catalan",
    "cs": "Czech", "cy": "Welsh", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "es": "Spanish", "et": "Estonian",
    "fa": "Persian", "fi": "Finnish", "fr": "French", "gl": "Galician",
    "gu": "Gujarati", "he": "Hebrew", "hi": "Hindi", "hr": "Croatian",
    "hu": "Hungarian", "hy": "Armenian", "id": "Indonesian", "is": "Icelandic",
    "it": "Italian", "ja": "Japanese", "ka": "Georgian", "kk": "Kazakh",
    "kn": "Kannada", "ko": "Korean", "la": "Latin", "lt": "Lithuanian",
    "lv": "Latvian", "mk": "Macedonian", "ml": "Malayalam", "mr": "Marathi",
    "ms": "Malay", "my": "Myanmar", "ne": "Nepali", "nl": "Dutch",
    "no": "Norwegian", "pa": "Punjabi", "pl": "Polish", "pt": "Portuguese",
    "ro": "Romanian", "ru": "Russian", "sk": "Slovak", "sl": "Slovenian",
    "sq": "Albanian", "sr": "Serbian", "sv": "Swedish", "sw": "Swahili",
    "ta": "Tamil", "te": "Telugu", "th": "Thai", "tl": "Tagalog",
    "tr": "Turkish", "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek",
    "vi": "Vietnamese", "zh": "Chinese",
}


def validate_youtube_url(value: str) -> str:
    """Return a normalized YouTube URL or raise ValueError.

    Keeping extraction limited to known YouTube hosts also prevents this endpoint
    from becoming a general-purpose downloader/SSRF primitive.
    """
    value = value.strip()
    if not value:
        raise ValueError("Paste a YouTube link first.")
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        raise ValueError("Enter a valid youtube.com or youtu.be video link.")
    if not parsed.path or parsed.path == "/":
        raise ValueError("This link does not point to a YouTube video.")
    return parsed.geturl()


def clock(seconds: float, milliseconds: bool = False) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if milliseconds:
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"
    return f"{hours:02d}:{minutes:02d}:{int(secs):02d}"


def parse_clock(value: str) -> float:
    """Parse SS, MM:SS, or HH:MM:SS into seconds."""
    raw = value.strip()
    if not raw:
        raise ValueError("A time value is required.")
    parts = raw.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise ValueError("Use SS, MM:SS, or HH:MM:SS.")
    numbers = [float(part) for part in parts]
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours, minutes, seconds = 0, *numbers
    else:
        hours, minutes, seconds = 0, 0, numbers[0]
    if minutes >= 60 or seconds >= 60:
        raise ValueError("Minutes and seconds must be below 60.")
    return hours * 3600 + minutes * 60 + seconds


def safe_filename(value: str, fallback: str = "transcript") -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "", value, flags=re.UNICODE).strip(" .")
    cleaned = re.sub(r"\s+", "-", cleaned)
    return (cleaned[:80] or fallback).lower()


def language_name(code: str | None) -> str:
    if not code:
        return "Auto-detected"
    normalized = code.lower().split("-")[0]
    return LANGUAGE_NAMES.get(normalized, code.title() if len(code) > 3 else code.upper())


def srt_timestamp(seconds: float) -> str:
    return clock(seconds, milliseconds=True).replace(".", ",")


def vtt_timestamp(seconds: float) -> str:
    return clock(seconds, milliseconds=True)


def segments_to_srt(segments: list[dict], offset: float = 0) -> str:
    rows: list[str] = []
    for index, segment in enumerate(segments, start=1):
        start = float(segment.get("start", 0)) + offset
        end = max(start + 0.05, float(segment.get("end", start + 0.05)) + offset)
        rows.extend([str(index), f"{srt_timestamp(start)} --> {srt_timestamp(end)}", segment.get("text", "").strip(), ""])
    return "\n".join(rows)


def segments_to_vtt(segments: list[dict], offset: float = 0) -> str:
    rows = ["WEBVTT", ""]
    for segment in segments:
        start = float(segment.get("start", 0)) + offset
        end = max(start + 0.05, float(segment.get("end", start + 0.05)) + offset)
        rows.extend([f"{vtt_timestamp(start)} --> {vtt_timestamp(end)}", segment.get("text", "").strip(), ""])
    return "\n".join(rows)


def first_existing(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path.exists() and path.is_file()), None)
