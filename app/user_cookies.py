from __future__ import annotations

import json
import time
import re
from pathlib import Path
from typing import Any
import os

USERS_DIR = Path(os.getenv("USER_COOKIES_DIR", "/tmp/sonicscript-user-cookies"))
USERS_DIR.mkdir(parents=True, exist_ok=True)

# Cookies expire roughly after 2-4 weeks on YouTube; we warn after 14 days, expire after 30
COOKIE_TTL_SECONDS = int(os.getenv("USER_COOKIE_TTL_SECONDS", "2592000"))  # 30 days
COOKIE_WARN_SECONDS = int(os.getenv("USER_COOKIE_WARN_SECONDS", "1209600"))  # 14 days

USER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,64}$")

def _user_dir(user_id: str) -> Path:
    # sanitize: only allow safe chars, fallback to hash
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)[:64]
    return USERS_DIR / safe

def _meta_path(user_id: str) -> Path:
    return _user_dir(user_id) / "meta.json"

def _cookie_path(user_id: str) -> Path:
    return _user_dir(user_id) / "cookies.txt"

def validate_user_id(user_id: str) -> bool:
    return bool(user_id and USER_ID_RE.match(user_id))

def ensure_user(user_id: str) -> Path:
    d = _user_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except Exception:
        pass
    meta = _meta_path(user_id)
    if not meta.exists():
        meta.write_text(json.dumps({
            "user_id": user_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "share_enabled": False,
            "last_used": 0,
            "use_count": 0,
            "status": "empty",
        }, indent=2))
        try:
            meta.chmod(0o600)
        except Exception:
            pass
    return d

def load_meta(user_id: str) -> dict[str, Any]:
    try:
        return json.loads(_meta_path(user_id).read_text())
    except Exception:
        return {}

def save_meta(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    ensure_user(user_id)
    meta_path = _meta_path(user_id)
    try:
        data = json.loads(meta_path.read_text())
    except Exception:
        data = {"user_id": user_id}
    data.update(patch)
    data["updated_at"] = time.time()
    meta_path.write_text(json.dumps(data, indent=2))
    try:
        meta_path.chmod(0o600)
    except Exception:
        pass
    return data

def save_user_cookie(user_id: str, content: bytes) -> dict[str, Any]:
    ensure_user(user_id)
    cp = _cookie_path(user_id)
    cp.write_bytes(content)
    try:
        cp.chmod(0o600)
    except Exception:
        pass
    return save_meta(user_id, {
        "last_upload": time.time(),
        "status": "valid",
        "expires_at": time.time() + COOKIE_TTL_SECONDS,
        "use_count": 0,
        "last_used": 0,
    })

def get_user_cookie_path(user_id: str) -> Path | None:
    cp = _cookie_path(user_id)
    if not cp.exists() or cp.stat().st_size == 0:
        return None
    meta = load_meta(user_id)
    # check expiry
    expires_at = meta.get("expires_at") or (meta.get("last_upload", 0) + COOKIE_TTL_SECONDS)
    if expires_at and time.time() > expires_at:
        # mark expired but keep file for debugging
        save_meta(user_id, {"status": "expired"})
        return None
    if meta.get("status") == "expired":
        return None
    return cp

def is_user_cookie_valid(user_id: str) -> bool:
    return get_user_cookie_path(user_id) is not None

def set_share_enabled(user_id: str, enabled: bool) -> dict[str, Any]:
    return save_meta(user_id, {"share_enabled": bool(enabled)})

def is_share_enabled(user_id: str) -> bool:
    return bool(load_meta(user_id).get("share_enabled"))

def mark_used(user_id: str) -> None:
    meta = load_meta(user_id)
    save_meta(user_id, {
        "last_used": time.time(),
        "use_count": int(meta.get("use_count", 0)) + 1,
    })

def mark_expired(user_id: str) -> None:
    save_meta(user_id, {"status": "expired"})

def get_user_status(user_id: str) -> dict[str, Any]:
    ensure_user(user_id)
    meta = load_meta(user_id)
    cp = _cookie_path(user_id)
    has_cookie = cp.exists() and cp.stat().st_size > 0
    valid = is_user_cookie_valid(user_id) if has_cookie else False
    expires_at = meta.get("expires_at") or (meta.get("last_upload", 0) + COOKIE_TTL_SECONDS if meta.get("last_upload") else 0)
    warn = False
    if has_cookie and valid and expires_at:
        warn = time.time() > (expires_at - (COOKIE_TTL_SECONDS - COOKIE_WARN_SECONDS))
    return {
        "user_id": user_id,
        "has_cookie": has_cookie,
        "valid": valid,
        "expired": has_cookie and not valid,
        "share_enabled": bool(meta.get("share_enabled")),
        "last_upload": meta.get("last_upload"),
        "expires_at": expires_at,
        "needs_renew": not valid,
        "warn_renew": warn,
        "last_used": meta.get("last_used"),
        "use_count": meta.get("use_count", 0),
        "status": meta.get("status", "empty"),
    }

def find_shared_cookie(exclude_user_id: str | None = None) -> tuple[str, Path] | None:
    """Find best shared cookie: most recent valid + share_enabled + not expired."""
    candidates: list[tuple[float, str, Path]] = []
    if not USERS_DIR.exists():
        return None
    for user_dir in USERS_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        uid = user_dir.name
        if exclude_user_id and uid == exclude_user_id:
            continue
        meta_path = user_dir / "meta.json"
        cookie_path = user_dir / "cookies.txt"
        if not cookie_path.exists() or cookie_path.stat().st_size == 0:
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        if not meta.get("share_enabled"):
            continue
        if meta.get("status") == "expired":
            continue
        expires_at = meta.get("expires_at") or (meta.get("last_upload", 0) + COOKIE_TTL_SECONDS)
        if expires_at and time.time() > expires_at:
            continue
        # freshness: last_upload
        last_upload = meta.get("last_upload") or cookie_path.stat().st_mtime
        candidates.append((last_upload, uid, cookie_path))
    if not candidates:
        return None
    candidates.sort(reverse=True)  # most recent first
    _, uid, path = candidates[0]
    return uid, path

def get_shared_pool_stats() -> dict[str, Any]:
    count = 0
    donors = 0
    total_valid = 0
    if USERS_DIR.exists():
        for user_dir in USERS_DIR.iterdir():
            if not user_dir.is_dir():
                continue
            meta_path = user_dir / "meta.json"
            cookie_path = user_dir / "cookies.txt"
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                continue
            donors += 1 if meta.get("share_enabled") else 0
            if cookie_path.exists() and cookie_path.stat().st_size > 0:
                total_valid += 1
                if meta.get("share_enabled"):
                    expires_at = meta.get("expires_at") or (meta.get("last_upload", 0) + COOKIE_TTL_SECONDS)
                    if not (expires_at and time.time() > expires_at) and meta.get("status") != "expired":
                        count += 1
    return {
        "shared_valid_count": count,
        "total_donors": donors,
        "total_users_with_cookie": total_valid,
        "has_pool": count > 0,
    }

def delete_user_cookie(user_id: str) -> None:
    cp = _cookie_path(user_id)
    if cp.exists():
        cp.unlink()
    save_meta(user_id, {"status": "empty", "expires_at": 0})
