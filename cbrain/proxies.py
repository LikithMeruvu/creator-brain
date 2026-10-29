"""Proxy pool: stream-check public (and user) proxies against YouTube, lease the best ones to fetch lanes.

Reality check (measured): of ~90k public proxies only ~4.5% answer youtube.com and ~20% of those can
fetch captions. So the pool streams shuffled candidates, stops as soon as it has enough healthy ones,
scores proxies, evicts fast and refills in the background. Proxies are only for public YouTube requests:
no cookies or API keys are ever sent through them, and TLS verification stays on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from .config import Settings
from .paths import VaultPaths

log = logging.getLogger("cbrain.proxies")

_GH = "https://raw.githubusercontent.com"
FREE_SOURCES: list[str] = [
    *(f"{_GH}/TheSpeedX/PROXY-List/master/{p}.txt" for p in ("http", "socks4", "socks5")),
    *(f"{_GH}/monosans/proxy-list/main/proxies/{p}.txt" for p in ("http", "socks4", "socks5")),
    *(f"{_GH}/proxifly/free-proxy-list/main/proxies/protocols/{p}/data.txt" for p in ("http", "socks4", "socks5")),
]

CHECK_URL = "https://www.youtube.com/"
MIN_BODY_BYTES = 2048
REFRESH_MIN_INTERVAL = 30 * 60      # seconds: skip a non-forced refresh younger than this
LOW_WATER = 10                      # start a background refresh below this many healthy proxies
USER_COOLDOWN = 10 * 60             # seconds a failing user proxy rests
DEAD_MEMORY = 6 * 3600              # remember evicted free proxies this long (don't re-check them)
BAD_MEMORY = 3600                   # in-memory only: candidates that failed a check
MAX_SCORE = 50.0

CheckFn = Callable[[str, float], Awaitable[bool]]
FetchFn = Callable[[str], str]

_PROXY_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks4a?|socks5h?)://)?"
    r"(?:(?P<auth>[^\s:@/]+:[^\s@/]+)@)?"
    r"(?P<host>(?:\d{1,3}\.){3}\d{1,3}|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})"
    r":(?P<port>\d{2,5})(?![\d])",
    re.IGNORECASE,
)
_SCHEME_MAP = {"http": "http", "https": "http", "socks4": "socks4", "socks4a": "socks4",
               "socks5": "socks5", "socks5h": "socks5"}


def parse_proxies(text: str, default_scheme: str = "http") -> list[str]:
    """Extract normalized proxy URLs (scheme://[user:pass@]host:port) from free-form lines; deduped, ordered."""
    out: dict[str, None] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "//")):
            continue
        m = _PROXY_RE.match(line)
        if not m:
            continue
        host, port = m["host"].lower(), int(m["port"])
        if not 1 <= port <= 65535:
            continue
        if host[0].isdigit() and host.replace(".", "").isdigit() and any(int(o) > 255 for o in host.split(".")):
            continue
        scheme = _SCHEME_MAP[(m["scheme"] or default_scheme).lower()]
        auth = f"{m['auth']}@" if m["auth"] else ""
        out[f"{scheme}://{auth}{host}:{port}"] = None
    return list(out)


def _scheme_for_source(url: str) -> str:
    low = url.lower()
    return "socks5" if "socks5" in low else "socks4" if "socks4" in low else "http"


# --------------------------------------------------------------------------- real network bits

def _quiet_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Free proxies reset connections constantly; Windows' Proactor loop logs each one. Swallow them."""
    log.debug("checker loop noise: %s", context.get("message"))


async def check_proxy(url: str, timeout: float = 10.0) -> bool:
    """True if GET https://www.youtube.com/ via `url` gives 200 with a body over 2 KB (TLS verified)."""
    import aiohttp

    async def inner() -> bool:
        is_socks = url.startswith("socks")
        if is_socks:
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(url, force_close=True)
        else:
            connector = aiohttp.TCPConnector(force_close=True)
        client_timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=timeout)
        async with aiohttp.ClientSession(
            connector=connector, timeout=client_timeout, trust_env=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
        ) as session:
            async with session.get(CHECK_URL, proxy=None if is_socks else url,
                                   allow_redirects=True, max_redirects=5) as resp:
                if resp.status != 200:
                    return False
                host = resp.url.host or ""
                if not host.endswith(("youtube.com", "google.com")):
                    return False
                size = 0
                async for chunk in resp.content.iter_chunked(4096):
                    size += len(chunk)
                    if size > MIN_BODY_BYTES:
                        return True
                return False

    try:
        return await asyncio.wait_for(inner(), timeout + 2)
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def _http_get_text(url: str) -> str:
    """Download a list directly (never through a proxy)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "creator-brain/1.0"})
    with opener.open(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------- pool

@dataclass
class _Entry:
    url: str
    source: str                 # "user" | "free"
    score: float = 1.0
    fails: int = 0
    last_ok: float = 0.0
    last_checked: float = 0.0
    state: str = "healthy"      # "healthy" | "evicted"
    cooldown_until: float = 0.0
    last_leased: float = 0.0

    def to_json(self) -> dict:
        return {"url": self.url, "source": self.source, "score": round(self.score, 3), "fails": self.fails,
                "last_ok": self.last_ok, "last_checked": self.last_checked, "state": self.state,
                "cooldown_until": self.cooldown_until}


class ProxyPool:
    def __init__(self, paths: VaultPaths, settings: Settings, *, check_fn: CheckFn | None = None,
                 fetch_fn: FetchFn | None = None, sources: list[str] | None = None,
                 concurrency: int = 400, timeout: float = 10.0, bg_min_interval: float = 300.0,
                 auto_refill: bool = True,
                 now: Callable[[], float] = time.time):
        self.paths = paths
        self.settings = settings
        self._check_fn: CheckFn = check_fn or check_proxy
        self._fetch_fn: FetchFn = fetch_fn or _http_get_text
        self._sources = list(FREE_SOURCES if sources is None else sources)
        self._concurrency = concurrency
        self._timeout = timeout
        self._bg_min_interval = bg_min_interval
        self._auto_refill = auto_refill
        self._save_lock = threading.Lock()
        self._now = now

        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}      # live proxies (healthy; user ones may be cooling)
        self._dead: dict[str, _Entry] = {}         # evicted free proxies (remembered for DEAD_MEMORY)
        self._bad: dict[str, float] = {}           # candidates that failed a check (memory only)
        self._leased: set[str] = set()
        self._last_refresh = 0.0
        self._last_save = 0.0
        self._refresh_lock = threading.Lock()
        self._bg_lock = threading.Lock()
        self._bg_thread: threading.Thread | None = None
        self._bg_last_end = 0.0

        self._load()
        self._sync_user_proxies()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            data = json.loads(self.paths.proxies_json.read_text(encoding="utf-8"))
            rows = data["proxies"]
            self._last_refresh = float(data.get("last_refresh") or 0.0)
        except (OSError, ValueError, KeyError, TypeError):
            return
        now = self._now()
        for r in rows:
            try:
                e = _Entry(url=r["url"], source=r.get("source", "free"), score=float(r.get("score", 1.0)),
                           fails=int(r.get("fails", 0)), last_ok=float(r.get("last_ok", 0.0)),
                           last_checked=float(r.get("last_checked", 0.0)), state=r.get("state", "healthy"),
                           cooldown_until=float(r.get("cooldown_until", 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
            if e.state == "evicted":
                if e.source == "free" and now - e.last_checked < DEAD_MEMORY:
                    self._dead[e.url] = e
            else:
                self._entries[e.url] = e

    def save(self) -> None:
        """Atomically write paths.proxies_json."""
        with self._lock:
            now = self._now()
            rows = [e.to_json() for e in self._entries.values()]
            rows += [e.to_json() for e in self._dead.values() if now - e.last_checked < DEAD_MEMORY]
            payload = {"version": 1, "last_refresh": self._last_refresh, "proxies": rows}
            self._last_save = now
        path = self.paths.proxies_json
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, indent=1)
        with self._save_lock:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            for attempt in range(5):             # Windows: Obsidian/antivirus may hold the target briefly
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1 * (attempt + 1))

    def _maybe_save(self) -> None:
        if self._now() - self._last_save > 15:
            try:
                self.save()
            except OSError:
                log.warning("could not save proxies.json", exc_info=True)

    # ------------------------------------------------------------------ user proxies
    def _read_user_proxies(self) -> list[str]:
        pf = (self.settings.proxy_file or "").strip()
        if not pf:
            return []
        try:
            return parse_proxies(Path(pf).expanduser().read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return []

    def _sync_user_proxies(self) -> list[str]:
        """Make `_entries` match the user's proxy file (new ones usable at once, removed ones dropped)."""
        urls = self._read_user_proxies()
        with self._lock:
            for u in [u for u, e in self._entries.items() if e.source == "user" and u not in urls]:
                del self._entries[u]
            for u in urls:
                e = self._entries.get(u)
                if e is None:
                    self._entries[u] = _Entry(u, "user", score=10.0)
                else:
                    e.source = "user"
        return urls

    def _has_sources(self) -> bool:
        return bool(self.settings.use_free_proxies) or bool(self._read_user_proxies())

    # ------------------------------------------------------------------ counting
    def _is_usable(self, e: _Entry, now: float) -> bool:
        return e.state == "healthy" and now >= e.cooldown_until

    def _healthy_count(self) -> int:
        now = self._now()
        with self._lock:
            return sum(1 for e in self._entries.values() if self._is_usable(e, now))

    def _fresh_count(self, since: float) -> int:
        now = self._now()
        with self._lock:
            return sum(1 for e in self._entries.values() if self._is_usable(e, now) and e.last_checked >= since)

    # ------------------------------------------------------------------ refresh
    def refresh(self, target_healthy: int = 60, force: bool = False) -> dict:
        """Verify candidates until `target_healthy` proxies are fresh-healthy. Runs its own event loop in
        the calling thread (safe from worker threads)."""
        started = self._now()
        if not self._refresh_lock.acquire(blocking=False):
            return {"skipped": "already_running", **self.stats()}
        try:
            if (not force and self._now() - self._last_refresh < REFRESH_MIN_INTERVAL
                    and self._healthy_count() >= LOW_WATER):
                return {"skipped": "recent", **self.stats()}

            user_urls = self._sync_user_proxies()
            if not user_urls and not self.settings.use_free_proxies:
                return {"skipped": "no_sources", **self.stats()}

            t0 = time.monotonic()
            fresh_since = started if force else started - REFRESH_MIN_INTERVAL
            candidates, list_info = self._build_candidates(user_urls, fresh_since)
            healthy_before = self._healthy_count()
            checked = self._run_checks(candidates, target_healthy, fresh_since)

            with self._lock:
                self._last_refresh = self._now()
                self._prune()
            self.save()
            return {
                "skipped": None, "candidates": len(candidates), "checked": checked,
                "healthy": self._healthy_count(), "new_healthy": max(0, self._healthy_count() - healthy_before),
                "lists_ok": list_info["ok"], "lists_failed": list_info["failed"],
                "seconds": round(time.monotonic() - t0, 1),
            }
        finally:
            self._refresh_lock.release()

    def _prune(self) -> None:
        now = self._now()
        for u in [u for u, e in self._dead.items() if now - e.last_checked >= DEAD_MEMORY]:
            del self._dead[u]
        for u in [u for u, t in self._bad.items() if now - t >= BAD_MEMORY]:
            del self._bad[u]

    def _download_free(self) -> tuple[list[str], dict]:
        info = {"ok": 0, "failed": 0}

        def one(src: str) -> list[str]:
            try:
                urls = parse_proxies(self._fetch_fn(src), _scheme_for_source(src))
                info["ok"] += 1
                return urls
            except Exception as exc:
                info["failed"] += 1
                log.warning("proxy list %s failed: %s", src, exc)
                return []

        with ThreadPoolExecutor(max_workers=max(1, min(9, len(self._sources)))) as ex:
            lists = list(ex.map(one, self._sources))
        seen: dict[str, None] = {}
        for lst in lists:
            for u in lst:
                seen[u] = None
        return list(seen), info

    def _build_candidates(self, user_urls: list[str], fresh_since: float) -> tuple[list[tuple[str, str]], dict]:
        """[(url, source)] ordered: user proxies, stale known free ones, then shuffled new ones."""
        info = {"ok": 0, "failed": 0}
        now0 = self._now()
        with self._lock:
            due = [u for u in user_urls
                   if not (u in self._entries and now0 < self._entries[u].cooldown_until)]
        out: list[tuple[str, str]] = [(u, "user") for u in due]
        taken = set(user_urls)
        with self._lock:
            stale = [e.url for e in self._entries.values()
                     if e.source == "free" and e.last_checked < fresh_since and e.url not in taken]
        out += [(u, "free") for u in stale]
        taken.update(stale)
        if self.settings.use_free_proxies:
            free, info = self._download_free()
            now = self._now()
            with self._lock:
                skip = taken | {u for u, e in self._entries.items() if e.last_checked >= fresh_since}
                skip |= {u for u, e in self._dead.items() if now - e.last_checked < DEAD_MEMORY}
                skip |= {u for u, t in self._bad.items() if now - t < BAD_MEMORY}
            new = [u for u in free if u not in skip]
            random.shuffle(new)
            out += [(u, "free") for u in new]
        return out, info

    def _run_checks(self, candidates: list[tuple[str, str]], target: int, fresh_since: float) -> int:
        if not candidates:
            return 0
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(_quiet_handler)
        try:
            return loop.run_until_complete(self._check_all(candidates, target, fresh_since))
        finally:
            try:
                loop.run_until_complete(asyncio.sleep(0.25))  # let transports finish closing
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                log.debug("checker loop shutdown noise", exc_info=True)
            finally:
                loop.close()

    async def _check_all(self, candidates: list[tuple[str, str]], target: int, fresh_since: float) -> int:
        it = iter(candidates)
        stop = asyncio.Event()
        checked = 0

        async def worker() -> None:
            nonlocal checked
            while not stop.is_set():
                try:
                    url, source = next(it)
                except StopIteration:
                    return
                try:
                    ok = bool(await self._check_fn(url, self._timeout))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    ok = False
                checked += 1
                self._record_check(url, source, ok)
                if ok and self._fresh_count(fresh_since) >= target:
                    stop.set()

        if self._fresh_count(fresh_since) >= target and not any(s == "user" for _, s in candidates):
            return 0
        tasks = [asyncio.create_task(worker()) for _ in range(min(self._concurrency, len(candidates)))]
        done_all = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
        stopper = asyncio.ensure_future(stop.wait())
        await asyncio.wait({done_all, stopper}, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
        return checked

    def _record_check(self, url: str, source: str, ok: bool) -> None:
        now = self._now()
        with self._lock:
            e = self._entries.get(url)
            if ok:
                if e is None:
                    e = self._dead.pop(url, None) or _Entry(url, source)
                    e.score = max(e.score, 1.0)
                    self._entries[url] = e
                e.state, e.fails, e.cooldown_until = "healthy", 0, 0.0
                e.last_ok = e.last_checked = now
                self._bad.pop(url, None)
                return
            self._bad[url] = now
            if e is not None:
                e.last_checked = now
                self._evict(e)

    def _evict(self, e: _Entry) -> None:
        """Caller holds the lock. User proxies rest; free ones are dropped (and remembered)."""
        now = self._now()
        if e.source == "user":
            e.cooldown_until = now + USER_COOLDOWN
            e.fails = 0
            e.score = max(1.0, e.score * 0.5)
            return
        self._entries.pop(e.url, None)
        e.state = "evicted"
        e.last_checked = now
        self._dead[e.url] = e

    # ------------------------------------------------------------------ leasing
    def acquire(self) -> str | None:
        """Best-scored healthy proxy not currently leased (user proxies first), or None."""
        now = self._now()
        with self._lock:
            best: _Entry | None = None
            best_key: tuple | None = None
            for e in self._entries.values():
                if e.url in self._leased or not self._is_usable(e, now):
                    continue
                key = (e.source != "user", -e.score, e.last_leased)
                if best_key is None or key < best_key:
                    best, best_key = e, key
            if best is not None:
                self._leased.add(best.url)
                best.last_leased = now
        if best is None or self._healthy_count() < LOW_WATER:
            self._maybe_background_refresh()
        return best.url if best is not None else None

    def release(self, proxy: str, ok: bool, blocked: bool = False) -> None:
        with self._lock:
            self._leased.discard(proxy)
            e = self._entries.get(proxy)
            if e is not None:
                now = self._now()
                if ok and not blocked:
                    e.score = min(MAX_SCORE, e.score + 1.0)
                    e.fails = 0
                    e.last_ok = now
                else:
                    e.fails += 1
                    e.score *= 0.5
                    if blocked or e.fails >= 2:
                        self._evict(e)
        self._maybe_save()
        if self._healthy_count() < LOW_WATER:
            self._maybe_background_refresh()

    # ------------------------------------------------------------------ background refill
    def _maybe_background_refresh(self) -> bool:
        """Start ONE background refresh thread when the pool is low. Returns True if a thread was started."""
        with self._bg_lock:
            if self._bg_thread is not None and self._bg_thread.is_alive():
                return False
            if not self._auto_refill or not self._has_sources():
                return False
            if self._bg_last_end and self._now() - self._bg_last_end < self._bg_min_interval:
                return False
            self._bg_thread = threading.Thread(target=self._bg_run, name="proxy-refresh", daemon=True)
            self._bg_thread.start()
            return True

    def _bg_run(self) -> None:
        try:
            self.refresh()
        except RuntimeError:                     # interpreter shutting down (daemon thread)
            log.debug("background refresh interrupted", exc_info=True)
        except Exception:
            log.exception("background proxy refresh failed")
        finally:
            self._bg_last_end = self._now()

    def background_running(self) -> bool:
        t = self._bg_thread
        return t is not None and t.is_alive()

    # ------------------------------------------------------------------ info
    def stats(self) -> dict:
        now = self._now()
        with self._lock:
            usable = [e for e in self._entries.values() if self._is_usable(e, now)]
            cooling = sum(1 for e in self._entries.values() if e.source == "user" and now < e.cooldown_until)
            last = (datetime.fromtimestamp(self._last_refresh, timezone.utc).isoformat(timespec="seconds")
                    if self._last_refresh else None)
            return {
                "healthy": len(usable),
                "leased": len(self._leased),
                "evicted": len(self._dead) + cooling,
                "user": sum(1 for e in self._entries.values() if e.source == "user"),
                "free": sum(1 for e in usable if e.source == "free"),
                "last_refresh": last,
            }
