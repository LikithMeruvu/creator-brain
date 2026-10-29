"""Public caption fetch through the yt-dlp Python API (no cookies, no config files, optional proxy)."""

from __future__ import annotations

import html
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

import yt_dlp

from ..models import Chapter, Segment, Transcript
from .errors import Blocked, FetchError, NoCaptions, Permanent, classify

MAX_CAPTION_BYTES = 5_000_000
WATCH_URL = "https://www.youtube.com/watch?v={}"


class _Quiet:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def ydl_opts(proxy: str | None = None, timeout: int = 15, **extra) -> dict:
    """Isolated yt-dlp options: no user config, no cookies, no cache, no retries (lanes own retry logic)."""
    opts = {
        "quiet": True, "no_warnings": True, "logger": _Quiet(), "ignoreconfig": True,
        "noplaylist": True, "cachedir": False, "cookiefile": None, "cookiesfrombrowser": None,
        "socket_timeout": timeout, "retries": 0, "extractor_retries": 0, "fragment_retries": 0,
        "file_access_retries": 0, "proxy": proxy or "",
    }
    opts.update(extra)
    return opts


def parse_json3(payload: dict) -> list[Segment]:
    """YouTube json3 -> ordered Segments. Tolerates missing durations, newline-only events and repeats."""
    events = [e for e in payload.get("events", []) if "tStartMs" in e]
    segments: list[Segment] = []
    for i, ev in enumerate(events):
        text = html.unescape("".join(s.get("utf8", "") for s in ev.get("segs") or []))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        start = float(ev["tStartMs"]) / 1000
        dur = ev.get("dDurationMs")
        if dur is not None:
            end = start + float(dur) / 1000
        elif i + 1 < len(events):
            end = float(events[i + 1]["tStartMs"]) / 1000
        else:
            end = start
        if not (math.isfinite(start) and math.isfinite(end)) or start < 0:
            continue
        end = max(start, end)
        if segments and segments[-1].text == text and abs(segments[-1].start - start) < 0.01:
            continue
        segments.append(Segment(start=start, end=end, text=text))
    return segments


def _is_en(lang: str, pref: str | None) -> bool:
    base = lang.split("-")[0].lower()
    return base == "en" or (pref is not None and base == pref.split("-")[0].lower())


def choose_track(info: dict) -> tuple[str, str, dict] | None:
    """Best json3 track as (lang, kind, track); manual en > auto en/en-orig > manual other > auto other."""
    pref = info.get("language")
    best: tuple[tuple[int, str], str, str, dict] | None = None
    for kind, key in (("manual", "subtitles"), ("auto", "automatic_captions")):
        for lang, formats in (info.get(key) or {}).items():
            if lang == "live_chat":
                continue
            for track in formats or []:
                if track.get("ext") != "json3" or not track.get("url"):
                    continue
                if "tlang" in parse_qs(urlsplit(track["url"]).query):  # machine translation of another track
                    continue
                en = _is_en(lang, pref) and (kind == "manual" or lang.lower() in ("en", "en-orig") or lang.lower().startswith("en-"))
                rank = (0 if kind == "manual" else 1) if en else (2 if kind == "manual" else 3)
                cand = ((rank, lang), lang, kind, track)
                if best is None or cand[0] < best[0]:
                    best = cand
    return (best[1], best[2], best[3]) if best else None


def video_info_summary(info: dict) -> dict:
    up = str(info.get("upload_date") or "")
    published = f"{up[:4]}-{up[4:6]}-{up[6:8]}" if re.fullmatch(r"\d{8}", up) else None
    chapters = [{"start": float(c.get("start_time") or 0), "title": c.get("title") or ""}
                for c in info.get("chapters") or []]
    return {
        "title": info.get("title"), "published": published, "duration": info.get("duration"),
        "view_count": info.get("view_count"), "chapters": chapters, "description": info.get("description"),
        "channel_id": info.get("channel_id"), "is_live": bool(info.get("is_live")),
        "live_status": info.get("live_status"), "language": info.get("language"),
        "availability": info.get("availability"),
    }


def extract_info(video_id: str, proxy: str | None, timeout: int, **extra) -> tuple[yt_dlp.YoutubeDL, dict]:
    """Not a context manager on purpose: caller needs the live YoutubeDL for urlopen. Caller must close()."""
    ydl = yt_dlp.YoutubeDL(ydl_opts(proxy, timeout, skip_download=True, ignore_no_formats_error=True, **extra))
    try:
        try:
            info = ydl.extract_info(WATCH_URL.format(video_id), download=False)
        except yt_dlp.utils.DownloadError as e:
            if "Requested format is not available" not in str(e):
                raise
            # captions need no media format: take the extractor's raw result and skip format selection
            info = ydl.extract_info(WATCH_URL.format(video_id), download=False, process=False)
    except Exception as e:
        ydl.close()
        raise classify(e, proxied=bool(proxy)) from e
    if not info:
        ydl.close()
        raise classify("empty response from extractor", proxied=bool(proxy))
    return ydl, info


def fetch_captions(video_id: str, proxy: str | None = None, timeout: int = 15) -> tuple[Transcript, dict]:
    """Return (Transcript, info). Raises Blocked / NoCaptions / Retryable / Permanent."""
    ydl, info = extract_info(video_id, proxy, timeout)
    try:
        summary = video_info_summary(info)
        if summary["is_live"] or summary["live_status"] in ("is_live", "is_upcoming"):
            raise Permanent(f"live/upcoming ({summary['live_status']})")
        chosen = choose_track(info)
        if chosen is None:
            raise NoCaptions("no json3 caption tracks", info=summary)
        lang, kind, track = chosen
        try:
            with ydl.urlopen(track["url"]) as resp:
                raw = resp.read(MAX_CAPTION_BYTES + 1)
        except Exception as e:
            raise classify(e, proxied=bool(proxy)) from e
        if len(raw) > MAX_CAPTION_BYTES:
            raise classify("caption payload too large / unexpected", proxied=bool(proxy))
        if not raw.strip():  # YouTube answers throttled caption requests with an empty 200
            raise Blocked("empty caption response (throttled)")
        try:
            segments = parse_json3(json.loads(raw))
        except (ValueError, TypeError, KeyError) as e:
            raise classify(f"malformed caption payload: {e}", proxied=bool(proxy)) from e
        if not segments:
            raise NoCaptions("caption track empty", info=summary)
    except FetchError:
        raise
    finally:
        ydl.close()
    chapters = [Chapter(start=c["start"], title=c["title"]) for c in summary["chapters"]]
    transcript = Transcript(video_id=video_id, segments=segments,
                            source="manual_caption" if kind == "manual" else "auto_caption",
                            language=lang, chapters=chapters)
    return transcript, summary
