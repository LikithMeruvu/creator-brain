"""Channel discovery: flat yt-dlp listing of a creator's videos, and the scope selector."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

import yt_dlp

from .fetch.captions import ydl_opts
from .fetch.errors import classify
from .models import Creator, Scope, VideoMeta, slugify

CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
TAB_SUFFIX_RE = re.compile(r"/(videos|shorts|streams|featured|playlists|community|about|live|releases)/?$")


def channel_base_url(handle_or_url: str) -> str:
    """Normalise '@h', 'h', 'UC…', or any channel URL to a base URL without a tab suffix."""
    s = handle_or_url.strip()
    if not s:
        raise ValueError("empty channel reference")
    if s.startswith(("http://", "https://")) or s.startswith(("youtube.com/", "www.youtube.com/", "m.youtube.com/")):
        if not s.startswith("http"):
            s = "https://" + s
        s = s.split("?")[0].split("#")[0].rstrip("/")
        s = TAB_SUFFIX_RE.sub("", s)
        return s.replace("://m.youtube.com", "://www.youtube.com").replace("://youtube.com", "://www.youtube.com")
    if CHANNEL_ID_RE.match(s):
        return f"https://www.youtube.com/channel/{s}"
    return f"https://www.youtube.com/@{s.lstrip('@')}"


def _flatten(entries) -> list[dict]:
    out: list[dict] = []
    for e in entries or []:
        if not e:
            continue
        if e.get("_type") == "playlist" or "entries" in e:
            out.extend(_flatten(e.get("entries")))
        else:
            out.append(e)
    return out


def _published(e: dict) -> str | None:
    up = str(e.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", up):
        return f"{up[:4]}-{up[4:6]}-{up[6:8]}"
    ts = e.get("timestamp") or e.get("release_timestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    return None


def _to_meta(e: dict, channel_id: str, is_short: bool) -> VideoMeta | None:
    vid = e.get("id")
    if not vid or not re.fullmatch(r"[0-9A-Za-z_-]{11}", str(vid)):
        return None
    live = e.get("live_status") in ("is_live", "is_upcoming", "post_live") or bool(e.get("is_live"))
    duration = e.get("duration")
    return VideoMeta(
        id=str(vid), title=e.get("title") or str(vid), published=_published(e),
        duration=int(duration) if isinstance(duration, (int, float)) else None,
        views=int(e["view_count"]) if isinstance(e.get("view_count"), (int, float)) else None,
        channel_id=e.get("channel_id") or channel_id, is_short=is_short, is_live=live,
    )


def _list_tab(url: str, limit: int | None, proxy: str | None, timeout: int) -> dict[str, Any]:
    extra: dict[str, Any] = {"extract_flat": "in_playlist", "skip_download": True, "ignore_no_formats_error": True}
    if limit:
        extra["playlistend"] = limit
    try:
        with yt_dlp.YoutubeDL(ydl_opts(proxy, timeout, noplaylist=False, **extra)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise classify(e, proxied=bool(proxy)) from e
    if not info:
        raise classify("empty channel listing", proxied=bool(proxy))
    return info


def list_channel(handle_or_url: str, limit: int | None = None, include_shorts: bool = False,
                 proxy: str | None = None, timeout: int = 30) -> tuple[Creator, list[VideoMeta]]:
    """Newest-first flat listing of the /videos tab (plus /shorts when asked). Fills the Creator."""
    base = channel_base_url(handle_or_url)
    info = _list_tab(f"{base}/videos", limit, proxy, timeout)
    entries = _flatten(info.get("entries"))
    channel_id = info.get("channel_id") or info.get("uploader_id") or ""
    if not CHANNEL_ID_RE.match(channel_id):
        channel_id = next((e["channel_id"] for e in entries if CHANNEL_ID_RE.match(e.get("channel_id") or "")),
                          channel_id if channel_id.startswith("UC") else "")
    name = info.get("channel") or info.get("uploader")
    if not name:
        name = re.sub(r"\s*-\s*Videos$", "", info.get("title") or "").strip() or handle_or_url.lstrip("@")
    handle = ""
    for cand in (info.get("uploader_id"), info.get("channel_url"), info.get("uploader_url"), base):
        m = re.search(r"@([^/?#\s]+)", cand or "")
        if m:
            handle = "@" + m.group(1)
            break
    if not handle and not handle_or_url.startswith(("http", "UC")):
        handle = "@" + handle_or_url.lstrip("@")

    videos: dict[str, VideoMeta] = {}
    for e in entries:
        m = _to_meta(e, channel_id, is_short=False)
        if m and m.id not in videos:
            videos[m.id] = m
    if include_shorts:
        try:
            s_info = _list_tab(f"{base}/shorts", limit, proxy, timeout)
        except Exception:  # channels without a Shorts tab are normal
            s_info = {}
        for e in _flatten(s_info.get("entries")):
            m = _to_meta(e, channel_id, is_short=True)
            if m and m.id not in videos:
                videos[m.id] = m

    creator = Creator(slug=slugify(name), name=name, handle=handle, channel_id=channel_id)
    return creator, list(videos.values())


def latest(handle_or_url: str, n: int = 15, proxy: str | None = None) -> list[VideoMeta]:
    """Cheap daily check: only the newest `n` uploads."""
    return list_channel(handle_or_url, limit=n, proxy=proxy)[1]


def _recency_order(videos: list[VideoMeta]) -> list[VideoMeta]:
    """Newest first. Trust the dates when every video has one; otherwise keep the listing order."""
    if all(v.published for v in videos):
        return sorted(videos, key=lambda v: v.published, reverse=True)  # sorted() is stable
    return list(videos)


def select(videos: list[VideoMeta], scope: Scope, recent_fraction: float = 0.30, top_winners: int = 10,
           include_shorts: bool = False, include_lives: bool = False) -> dict[str, float]:
    """id -> priority. Winners (1000+) come first, then the rest newest first (priority in (0, 1])."""
    eligible = [v for v in videos if (include_shorts or not v.is_short) and (include_lives or not v.is_live)]
    ordered = _recency_order(eligible)
    n = len(ordered)
    if n == 0:
        return {}
    recency = {v.id: i for i, v in enumerate(ordered)}

    by_views = sorted((v for v in ordered if v.views is not None),
                      key=lambda v: (-(v.views or 0), recency[v.id]))
    winners = by_views[:max(0, top_winners)]

    if scope == "full":
        chosen = {v.id for v in ordered}
    else:
        recent_n = min(n, math.ceil(recent_fraction * n))
        chosen = {v.id for v in ordered[:recent_n]} | {v.id for v in winners}

    out = {vid: round(1.0 - recency[vid] / n, 6) for vid in chosen}
    for rank, v in enumerate(winners):
        out[v.id] = 1000.0 + (len(winners) - rank)
    return out
