"""Audio fallback: smallest audio via yt-dlp (may use the lane's proxy), then Groq Whisper or local faster-whisper.

The Groq API key is only ever sent to api.groq.com over a direct connection (proxies are bypassed explicitly).
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yt_dlp

from ..config import Settings
from ..models import Segment, Transcript
from .captions import WATCH_URL, ydl_opts
from .errors import FetchError, Permanent, Retryable, classify

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 24 * 1024 * 1024
CHUNK_SECONDS = 600
MAX_RETRY_AFTER = 90          # longer waits are handed back to the queue instead of blocking a lane
AUDIO_FORMAT = "139/worstaudio[ext=m4a]/worstaudio/worst"

_sleep = time.sleep           # patched in tests
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never routes through a proxy
_whisper_model = None
_whisper_lock = threading.Lock()


def download_audio(video_id: str, dest: Path, proxy: str | None = None, timeout: int = 30) -> Path:
    opts = ydl_opts(proxy, timeout, format=AUDIO_FORMAT, outtmpl=str(dest / "audio.%(ext)s"),
                    ignore_no_formats_error=False)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([WATCH_URL.format(video_id)])
    except Exception as e:
        raise classify(e, proxied=bool(proxy)) from e
    files = sorted(p for p in dest.glob("audio.*") if p.suffix not in (".part", ".ytdl"))
    if not files:
        raise Retryable("audio download produced no file")
    return files[0]


def split_audio(path: Path, dest: Path) -> list[tuple[Path, float]]:
    """(chunk, offset_seconds) pairs. Uses ffmpeg only if present and needed."""
    if path.stat().st_size <= MAX_UPLOAD_BYTES:
        return [(path, 0.0)]
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise Permanent("audio larger than 24 MB and ffmpeg is not installed")
    pattern = dest / f"chunk_%04d{path.suffix}"
    cmd = [ffmpeg, "-v", "error", "-y", "-i", str(path), "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
           "-c", "copy", "-reset_timestamps", "1", str(pattern)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError) as e:
        raise Retryable(f"ffmpeg split failed: {e}") from e
    chunks = sorted(dest.glob(f"chunk_*{path.suffix}"))
    if not chunks:
        raise Retryable("ffmpeg produced no chunks")
    return [(c, i * float(CHUNK_SECONDS)) for i, c in enumerate(chunks)]


def _multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def groq_transcribe_file(path: Path, settings: Settings, offset: float = 0.0,
                         max_retries: int = 3) -> tuple[list[Segment], str | None]:
    body, ctype = _multipart(
        {"model": settings.asr_model, "response_format": "verbose_json", "temperature": "0"},
        "file", path.name, path.read_bytes())
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(GROQ_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {settings.groq_api_key}", "Content-Type": ctype,
            "User-Agent": "creator-brain/0.1"})
        try:
            with _direct.open(req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(e.headers.get("retry-after", "") or 20)
                except ValueError:
                    wait = 20.0
                if wait > MAX_RETRY_AFTER or attempt == max_retries:
                    raise Retryable(f"Groq rate limited (retry-after {wait:.0f}s)") from e
                _sleep(wait + 0.5)
                continue
            if e.code in (500, 502, 503, 504) and attempt < max_retries:
                _sleep(2 ** attempt * 2)
                continue
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if e.code in (401, 403):
                raise FetchError(f"Groq rejected the API key ({e.code})") from e
            raise Retryable(f"Groq HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt < max_retries:
                _sleep(2 ** attempt * 2)
                continue
            raise Retryable(f"Groq request failed: {e}") from e
        segs = [Segment(start=offset + float(s["start"]), end=offset + float(s["end"]), text=" ".join(s["text"].split()))
                for s in data.get("segments") or [] if str(s.get("text", "")).strip()]
        if not segs and str(data.get("text", "")).strip():
            segs = [Segment(start=offset, end=offset, text=" ".join(data["text"].split()))]
        return segs, data.get("language")
    raise Retryable("Groq retries exhausted")


def local_transcribe_file(path: Path) -> tuple[list[Segment], str | None]:
    global _whisper_model
    try:
        from faster_whisper import WhisperModel  # lazy: optional dependency
    except ImportError as e:
        raise FetchError("faster-whisper is not installed (pip install faster-whisper)") from e
    with _whisper_lock:
        if _whisper_model is None:
            _whisper_model = WhisperModel("small", device="auto", compute_type="int8")
        segments, info = _whisper_model.transcribe(str(path), vad_filter=True)
        segs = [Segment(start=float(s.start), end=float(s.end), text=" ".join(s.text.split()))
                for s in segments if s.text.strip()]
    return segs, getattr(info, "language", None)


def transcribe(video_id: str, settings: Settings, proxy: str | None = None) -> Transcript:
    """Groq Whisper first (if a key exists), then local faster-whisper (if enabled)."""
    if not settings.groq_api_key and not settings.local_whisper:
        raise FetchError("no ASR available: set a Groq key or enable local_whisper")
    with tempfile.TemporaryDirectory(prefix="cbrain-asr-") as tmp:
        tmpdir = Path(tmp)
        audio = download_audio(video_id, tmpdir, proxy)
        if settings.groq_api_key:
            try:
                all_segs: list[Segment] = []
                lang = None
                for chunk, offset in split_audio(audio, tmpdir):
                    segs, lang = groq_transcribe_file(chunk, settings, offset)
                    all_segs.extend(segs)
                if all_segs:
                    return Transcript(video_id=video_id, segments=all_segs, source="groq_whisper", language=lang)
                raise Retryable("Groq returned no speech")
            except FetchError:
                if not settings.local_whisper:
                    raise
        segs, lang = local_transcribe_file(audio)
        if not segs:
            raise Retryable("local whisper returned no speech")
        return Transcript(video_id=video_id, segments=segs, source="local_whisper", language=lang)
