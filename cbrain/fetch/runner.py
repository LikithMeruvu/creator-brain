"""Parallel fetch lanes: lane 0 uses the user's own IP (paced), lanes 1..N each borrow a proxy from the pool.

Ladder per video: captions -> audio ASR (Groq Whisper / local Whisper) -> park. Blocked IPs never burn a
video attempt; they defer the job and move the lane to a fresh proxy (or, for lane 0, back off for 15 minutes).
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from collections import Counter
from typing import Callable

from ..config import Settings
from ..jobs import JobStore, VideoRow
from ..models import SOURCE_RANK, Chapter, JobState, Transcript
from ..paths import VaultPaths
from .asr import transcribe
from .captions import fetch_captions
from .errors import Blocked, FetchError, NoCaptions, Permanent, Retryable
from .sponsorblock import cut, sponsor_spans

log = logging.getLogger("cbrain.fetch")

LEASE_SECONDS = 900


def save_transcript(path, transcript: Transcript) -> None:
    """Atomic gzip-JSON write (tmp + os.replace) so Obsidian/readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(transcript.to_dict(), f, ensure_ascii=False)
    os.replace(tmp, path)


class FetchRunner:
    def __init__(self, store: JobStore, paths: VaultPaths, settings: Settings, pool,
                 on_fetched: Callable[[VideoRow, Transcript, dict], None], *,
                 block_backoff_seconds: float = 900, defer_seconds: float = 30,
                 idle_wait_seconds: float = 5, backoff_poll_seconds: float = 10, caption_timeout: int = 15):
        self.store, self.paths, self.settings, self.pool, self.on_fetched = store, paths, settings, pool, on_fetched
        self.block_backoff = block_backoff_seconds
        self.defer_seconds = defer_seconds
        self.idle_wait = idle_wait_seconds
        self.backoff_poll = backoff_poll_seconds
        self.caption_timeout = caption_timeout
        self._db_lock = threading.Lock()      # JobStore shares one sqlite connection across threads
        self._stats_lock = threading.Lock()
        self._stats: Counter = Counter()
        self._by_source: Counter = Counter()
        self._errors: list[str] = []
        self._stop = threading.Event()
        self._max: int | None = None
        self._inflight = 0                    # reserved + finished videos, for max_videos
        self._upgrade_ids: set[str] = set()
        self._paused = False

    # ---- public ---------------------------------------------------------
    def run(self, stop: threading.Event | None = None, max_videos: int | None = None) -> dict:
        self._stop = stop or threading.Event()
        self._max = max_videos
        self._inflight = 0
        self._upgrade_ids = self._pick_upgrade_ids()
        lanes = [0] + (list(range(1, max(1, self.settings.lanes))) if self.pool is not None else [])
        threads = [threading.Thread(target=self._lane_main, args=(i,), name=f"fetch-lane-{i}", daemon=True)
                   for i in lanes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        out = {
            "fetched": self._stats["fetched"], "parked": self._stats["parked"], "retried": self._stats["retried"],
            "blocked": self._stats["blocked"], "upgraded": self._stats["upgraded"],
            "by_source": dict(self._by_source), "lanes": len(lanes), "paused": self._paused,
            "stopped": self._stop.is_set(), "errors": self._errors[:20], "pool": None,
        }
        if self.pool is not None:
            try:
                out["pool"] = self.pool.stats()
            except Exception:  # stats are informational only
                pass
        return out

    # ---- lane loop ------------------------------------------------------
    def _lane_main(self, idx: int) -> None:
        try:
            self._lane(idx)
        except Exception as e:  # a crashed lane must be visible, never silent
            log.exception("lane %s crashed", idx)
            self._note_error(f"lane {idx} crashed: {e!r}")

    def _lane(self, idx: int) -> None:
        own = idx == 0
        interval = self.settings.own_ip_interval_seconds if own else self.settings.proxy_lane_interval_seconds
        proxy: str | None = None
        last_start = 0.0
        misses = 0
        while not self._stop.is_set():
            if self.paths.pause_flag.exists():
                self._paused = True
                return
            if not own and proxy is None:
                proxy = self.pool.acquire()
                if proxy is None:
                    misses += 1
                    if misses > 6 or self._stop.wait(min(self.idle_wait * 2 ** (misses - 1), 60)):
                        return
                    continue
                misses = 0
            wait = interval - (time.monotonic() - last_start) if last_start else 0
            if wait > 0 and self._stop.wait(wait):
                break
            row = self._reserve()
            if row is None:
                break
            last_start = time.monotonic()
            try:
                outcome = self._handle(row, proxy)
            except Exception as e:
                log.exception("unexpected failure on %s", row.id)
                outcome = ("retry", f"unexpected: {e!r}")
            proxy = self._after(row, outcome, own, proxy)
            if outcome[0] == "blocked" and own:
                if not self._backoff(self.block_backoff):
                    break
        if proxy is not None:
            self.pool.release(proxy, ok=True)

    def _reserve(self) -> VideoRow | None:
        with self._db_lock:
            if self._max is not None and self._inflight >= self._max:
                return None
            rows = self.store.claim(limit=1, lease_seconds=LEASE_SECONDS)
            if not rows:
                return None
            self._inflight += 1
            return rows[0]

    def _backoff(self, seconds: float) -> bool:
        """Sleep after the own IP got blocked; return False if stopped or nothing is left to do."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop.wait(min(self.backoff_poll, max(0.0, end - time.monotonic()))):
                return False
            if self.paths.pause_flag.exists():
                return False
            with self._db_lock:
                if self.store.counts().get(JobState.QUEUED.value, 0) == 0:
                    return False
        return True

    # ---- one video ------------------------------------------------------
    def _handle(self, row: VideoRow, proxy: str | None) -> tuple:
        """Returns ('ok', transcript, info) or (kind, message)."""
        s = self.settings
        asr_ok = bool(s.groq_api_key or s.local_whisper)
        info: dict = {}
        try:
            try:
                transcript, info = fetch_captions(row.id, proxy, self.caption_timeout)
            except NoCaptions as e:
                info = e.info
                if not asr_ok:
                    return ("park", "no captions and no ASR configured (add a Groq key or enable local_whisper, then retry)")
                transcript = transcribe(row.id, s, proxy)
            else:
                if (transcript.source == "auto_caption" and s.groq_api_key and row.id in self._upgrade_ids):
                    transcript = self._try_upgrade(row, transcript, proxy)
        except Blocked as e:
            return ("blocked", str(e))
        except Permanent as e:
            return ("park", str(e))
        except Retryable as e:
            return ("retry", str(e))
        except FetchError as e:
            return ("retry", str(e))
        if not transcript.chapters and info.get("chapters"):
            transcript.chapters = [Chapter(start=c["start"], title=c["title"]) for c in info["chapters"]]
        if s.sponsorblock:
            try:
                transcript = cut(transcript, sponsor_spans(row.id))
            except Exception:  # SponsorBlock is best effort
                log.warning("sponsorblock failed for %s", row.id)
        return ("ok", transcript, info)

    def _try_upgrade(self, row: VideoRow, current: Transcript, proxy: str | None) -> Transcript:
        try:
            better = transcribe(row.id, self.settings, proxy)
        except FetchError as e:  # includes Blocked: the captions we already have are good enough
            log.info("whisper upgrade skipped for %s: %s", row.id, e)
            return current
        if SOURCE_RANK.get(better.source, 0) > SOURCE_RANK.get(current.source, 0) and better.segments:
            better.chapters = better.chapters or current.chapters
            with self._stats_lock:
                self._stats["upgraded"] += 1
            return better
        return current

    def _after(self, row: VideoRow, outcome: tuple, own: bool, proxy: str | None) -> str | None:
        """Record the result in the store; returns the proxy this lane should keep (None = take a new one)."""
        kind = outcome[0]
        if kind == "ok":
            _, transcript, info = outcome
            save_transcript(self.paths.transcript(row.id), transcript)
            with self._db_lock:
                self._apply_info(row.id, info)
                self.store.mark(row.id, JobState.FETCHED, transcript.source)
                fresh = self.store.get(row.id) or row
            self._count("fetched", transcript.source)
            try:
                self.on_fetched(fresh, transcript, info)
            except Exception as e:
                log.exception("on_fetched failed for %s", row.id)
                self._note_error(f"on_fetched {row.id}: {e!r}")
            return self._release(proxy, ok=True)
        if kind == "blocked":
            with self._db_lock:
                self.store.defer(row.id, self.defer_seconds)
                self._inflight -= 1
            self._count("blocked")
            return self._release(proxy, ok=False, blocked=True)
        msg = outcome[1]
        with self._db_lock:
            if kind == "park":
                self.store.fail(row.id, msg, max_attempts=self.settings.max_attempts, retry=False)
            else:
                self.store.fail(row.id, msg, max_attempts=self.settings.max_attempts, retry=True)
        self._count("parked" if kind == "park" else "retried")
        # a parked video says nothing bad about the proxy; a network retry might
        return self._release(proxy, ok=(kind == "park"), blocked=False) if proxy else None

    def _apply_info(self, video_id: str, info: dict) -> None:
        """Fill published date / exact views / duration learned from the full page (flat listing lacks them)."""
        if not info:
            return
        self.store.update_meta(video_id, published=info.get("published"), views=info.get("view_count"),
                               duration=info.get("duration"))

    def _release(self, proxy: str | None, ok: bool, blocked: bool = False) -> str | None:
        if proxy is None:
            return None
        try:
            self.pool.release(proxy, ok=ok, blocked=blocked)
        except Exception as e:
            self._note_error(f"pool.release: {e!r}")
        return None  # always re-acquire: the pool picks the best free proxy each time

    # ---- helpers --------------------------------------------------------
    def _pick_upgrade_ids(self) -> set[str]:
        n = self.settings.whisper_upgrade_top
        if n <= 0 or not self.settings.groq_api_key:
            return set()
        with self._db_lock:
            rows = self.store.videos()
        per: dict[str, list[VideoRow]] = {}
        for r in rows:
            if r.views is not None:
                per.setdefault(r.creator_slug, []).append(r)
        ids: set[str] = set()
        for lst in per.values():
            lst.sort(key=lambda r: (-(r.views or 0), r.id))
            ids.update(r.id for r in lst[:n])
        return ids

    def _count(self, key: str, source: str | None = None) -> None:
        with self._stats_lock:
            self._stats[key] += 1
            if source:
                self._by_source[source] += 1

    def _note_error(self, msg: str) -> None:
        with self._stats_lock:
            if len(self._errors) < 50:
                self._errors.append(msg)
