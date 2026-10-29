"""Eyes: thumbnails, storyboard frames and Groq vision descriptions (all optional, never fatal)."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import Settings
from .paths import VaultPaths

log = logging.getLogger("cbrain.visual")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
THUMB_URL = "https://i.ytimg.com/vi/{id}/{name}.jpg"
MAX_IMAGES = 40               # frames/mosaics kept per video
MAX_TOTAL_BYTES = 12 * 1024 * 1024
BATCH_SIZE = 5                # Groq vision limit: 5 images per request
MAX_RETRIES = 4
MAX_RETRY_SLEEP = 60.0
UA = "Mozilla/5.0 (compatible; creator-brain)"

# Direct connection: ignores HTTP(S)_PROXY env vars, so secrets/own-IP traffic never goes through a proxy.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(url: str, data: bytes | None = None, headers: dict | None = None,
          proxy: str | None = None, timeout: float = 20) -> bytes:
    """One HTTP request (TLS verified). `proxy` only for public YouTube images; Groq calls never pass it."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = _DIRECT
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- thumbnail

def thumbnail(video_id: str, paths: VaultPaths) -> Path | None:
    """Cache the thumbnail at paths.visual/<id>.jpg (maxres, falling back to hq). None on failure."""
    dest = paths.visual / f"{video_id}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    for name in ("maxresdefault", "hqdefault"):
        try:
            data = _http(THUMB_URL.format(id=video_id, name=name))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            log.warning("thumbnail %s: HTTP %s", video_id, e.code)
            return None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log.warning("thumbnail %s: %s", video_id, e)
            return None
        if data:
            _atomic_write(dest, data)
            return dest
    return None


# ---------------------------------------------------------------- storyboards

def _extract_info(video_id: str, proxy: str | None) -> dict:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreconfig": True,
            "proxy": proxy or "", "cookiefile": None, "socket_timeout": 20}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def _pick_storyboard(info: dict) -> dict | None:
    fmts = [f for f in (info.get("formats") or []) if f.get("fragments")
            and (f.get("format_note") == "storyboard" or f.get("ext") == "mhtml"
                 or str(f.get("format_id", "")).startswith("sb"))]
    for f in fmts:
        if f.get("format_id") == "sb0":
            return f
    return max(fmts, key=lambda f: f.get("width") or 0, default=None)


def _pillow():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def _crop_tiles(Image, data: bytes, rows: int, cols: int, picks: list[tuple[int, float]]) -> list[tuple[float, bytes]]:
    """Crop the chosen tile indexes (row-major) out of one mosaic; `picks` = (tile_index, timestamp)."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    tw, th = img.width // cols, img.height // rows
    out = []
    for idx, ts in picks:
        r, c = divmod(min(idx, rows * cols - 1), cols)
        buf = io.BytesIO()
        img.crop((c * tw, r * th, (c + 1) * tw, (r + 1) * th)).save(buf, "JPEG", quality=80)
        out.append((ts, buf.getvalue()))
    return out


def storyboard_frames(video_id: str, proxy: str | None = None, every_seconds: int = 30,
                      info: dict | None = None) -> list[tuple[float, bytes]]:
    """Frames roughly every `every_seconds`: cropped tiles with Pillow, else whole mosaics with their start time."""
    if info is None:
        info = _extract_info(video_id, proxy)
    fmt = _pick_storyboard(info or {})
    if not fmt:
        return []
    frags = fmt["fragments"]
    rows, cols = int(fmt.get("rows") or 1), int(fmt.get("columns") or 1)
    per = max(rows * cols, 1)
    durs = [float(f.get("duration") or 0) for f in frags]
    total = sum(durs)
    if total <= 0:
        return []
    tile_dt = (durs[0] / per) or 1.0
    step = max(float(every_seconds), total / MAX_IMAGES, 1.0)
    starts, acc = [], 0.0
    for d in durs:
        starts.append(acc)
        acc += d

    # tile pick per fragment: {fragment index: [(tile index, timestamp)]}
    wanted: dict[int, list[tuple[int, float]]] = {}
    fi, t = 0, 0.0
    while t < total and sum(len(v) for v in wanted.values()) < MAX_IMAGES:
        while fi < len(frags) - 1 and t >= starts[fi] + durs[fi]:
            fi += 1
        idx = int((t - starts[fi]) / tile_dt)
        wanted.setdefault(fi, []).append((min(idx, per - 1), round(t, 1)))
        t += step

    Image = _pillow()
    out: list[tuple[float, bytes]] = []
    size = 0
    for fi in sorted(wanted):
        if len(out) >= MAX_IMAGES or size >= MAX_TOTAL_BYTES:
            break
        try:
            data = _http(frags[fi]["url"], proxy=proxy)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log.warning("storyboard %s fragment %d: %s", video_id, fi, e)
            continue
        if Image is not None:
            try:
                items = _crop_tiles(Image, data, rows, cols, wanted[fi])
            except Exception as e:  # corrupt mosaic: fall back to the whole image
                log.warning("storyboard %s crop failed (%s); using mosaic", video_id, e)
                items = [(round(starts[fi], 1), data)]
        else:
            items = [(round(starts[fi], 1), data)]
        for ts, img in items:
            if len(out) >= MAX_IMAGES or size + len(img) > MAX_TOTAL_BYTES:
                return out
            out.append((ts, img))
            size += len(img)
    return out


# ---------------------------------------------------------------- vision

PROMPT = (
    "You are looking at frames from a YouTube video, in order. Some images may be contact sheets "
    "(a grid of small frames); treat a sheet as one entry. For EACH image give one short factual line "
    "describing only what is visibly on screen: exercise or action shown, equipment, text on screen, "
    "diagrams or charts, the setting. No guessing, no opinions. Reply with exactly {n} lines in the "
    "format `<number>: <description>`, numbered 1 to {n}."
)


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _data_url(data: bytes) -> str:
    return f"data:{_mime(data)};base64,{base64.b64encode(data).decode()}"


def _chat(settings: Settings, content: list[dict]) -> str | None:
    """One Groq chat call with 429 retry. Direct connection only: the key never touches a proxy."""
    body = json.dumps({
        "model": settings.vision_model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
        "max_tokens": 700,
    }).encode()
    headers = {"Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": "application/json"}
    for attempt in range(MAX_RETRIES):
        try:
            raw = _http(GROQ_URL, data=body, headers=headers, timeout=90)
            return json.loads(raw)["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES - 1:
                try:
                    wait = float(e.headers.get("retry-after", 5)) if e.headers else 5.0
                except (TypeError, ValueError):
                    wait = 5.0
                time.sleep(min(max(wait, 1.0), MAX_RETRY_SLEEP))
                continue
            log.warning("groq vision HTTP %s", e.code)
            return None
        except (urllib.error.URLError, OSError, TimeoutError, KeyError, IndexError, ValueError) as e:
            log.warning("groq vision failed: %s", e)
            return None
    return None


def _parse_lines(text: str, n: int) -> list[str]:
    found: dict[int, str] = {}
    for m in re.finditer(r"^\s*\**(\d+)\**\s*[.:)\-]\s*(.+?)\s*$", text, re.M):
        i = int(m.group(1))
        if 1 <= i <= n and i not in found:
            found[i] = m.group(2).strip()
    if not found and n == 1 and text.strip():
        found[1] = text.strip()
    return [found.get(i, "") for i in range(1, n + 1)]


def describe(frames: list[tuple[float, bytes]], settings: Settings) -> list[tuple[float, str]]:
    """Short on-screen descriptions per frame via Groq vision, batched. [] without a key."""
    if not settings.groq_api_key or not frames:
        return []
    out: list[tuple[float, str]] = []
    for i in range(0, len(frames), BATCH_SIZE):
        batch = frames[i:i + BATCH_SIZE]
        content: list[dict] = [{"type": "text", "text": PROMPT.format(n=len(batch))}]
        for _, img in batch:
            content.append({"type": "image_url", "image_url": {"url": _data_url(img)}})
        text = _chat(settings, content)
        if not text:
            continue
        for (ts, _), desc in zip(batch, _parse_lines(text, len(batch))):
            if desc:
                out.append((ts, desc))
    return out


# ---------------------------------------------------------------- worker entry point

def visual_for_video(video_id: str, paths: VaultPaths, settings: Settings,
                     info: dict | None = None, proxy: str | None = None) -> list[tuple[float, str]]:
    """Cache the thumbnail, then describe storyboard frames. Never raises."""
    try:
        thumbnail(video_id, paths)
        if not settings.groq_api_key:
            return []
        frames = storyboard_frames(video_id, proxy=proxy, info=info)
        return describe(frames, settings)
    except Exception as e:
        log.warning("visual %s failed: %s", video_id, e)
        return []
