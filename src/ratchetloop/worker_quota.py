"""Quota text/code matching next to worker_io.QUOTA_CODES / QUOTA_TEXT."""
from __future__ import annotations

from .worker_io import QUOTA_CODES, QUOTA_TEXT, Event, loads_obj


def text_is_quota(text: str, provider: str | None = None) -> bool:
    blob = (text or "").lower()
    phrases = QUOTA_TEXT.get(provider or "", tuple(
        p for v in QUOTA_TEXT.values() for p in v))
    return bool(blob) and any(p in blob for p in phrases)


def event_is_quota(ev: Event, provider: str | None = None) -> bool:
    if ev.kind not in ("error", "usage"):
        return False
    if text_is_quota(ev.text, provider) or text_is_quota(ev.raw, provider):
        return True
    obj = loads_obj(ev.raw) or {}
    nested = obj.get("error") if isinstance(obj.get("error"), dict) else {}
    for src in (obj, nested):
        if not isinstance(src, dict):
            continue
        if src.get("limit_reached") is True:
            return True
        for key in ("code", "error_code", "status", "type"):
            if src.get(key) in QUOTA_CODES:
                return True
    return False
