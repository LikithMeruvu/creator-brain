"""The receptionist: one façade that the CLI and the MCP server both call.

Every public method returns a JSON-able dict with a human `message`, never raises (errors become
{"error", "hint"}) and never blocks on transcript fetching: slow work is queued for the background worker.
"""

from __future__ import annotations

import copy
import difflib
import functools
import importlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import worker
from .config import Settings
from .jobs import JobStore, WriterLock, now_iso
from .models import Creator, slugify
from .paths import VaultPaths, default_vault

DEFAULT_LEGACY_DB = ""  # optional: point CBRAIN_LEGACY_DB at a v1 knowledge.db to reuse its transcripts
PROFILE_KEYS = ("goal", "level", "age", "weight", "injuries", "equipment", "days_per_week", "diet", "notes")
PROFILE_ALIASES = {"days": "days_per_week", "days_a_week": "days_per_week", "injury": "injuries",
                   "target": "goal", "experience": "level"}
SNIPPET_CHARS = 350
_CLAIM_FIELDS = ("creator", "topic", "claim", "quote", "kind", "date", "url", "t", "video_id")


def _mod(name: str):
    return importlib.import_module(name)


class BrainError(Exception):
    """A problem the user can fix; carries a plain-English hint."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def _hint_for(e: Exception) -> str:
    name = type(e).__name__
    if isinstance(e, ModuleNotFoundError | ImportError):
        return f"A Creator Brain component is missing ({e}). Run `brain doctor`, or reinstall with `pip install -e .`."
    if isinstance(e, sqlite3.OperationalError):
        return "The vault database is busy or locked. Wait a few seconds and try again."
    if name in ("DownloadError", "ExtractorError", "UnavailableVideoError"):
        return "YouTube did not return that channel. Check the handle (like @JeremyEthier) or paste the channel URL, then try again."
    if isinstance(e, TimeoutError | ConnectionError) or name in ("URLError", "ClientError", "HTTPError"):
        return "Network problem. Check your internet connection and try again in a minute."
    if name == "NoLLM":
        return "No Groq key is set. Run `brain setup` to add one (free), or ask your AI agent to extract claims itself."
    return "Run `brain doctor` to check the vault, then retry. Details are in .brain/worker.log."


def _safe(fn: Callable) -> Callable:
    """Serialise calls on one Brain and turn exceptions into {"error", "hint"} dicts."""

    @functools.wraps(fn)
    def wrapper(self: "Brain", *a, **kw):
        with self._lock:
            try:
                return fn(self, *a, **kw)
            except BrainError as e:
                return {"error": str(e), "hint": e.hint}
            except Exception as e:  # noqa: BLE001 - the façade must never raise
                worker.log_line(self.paths, f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
                return {"error": f"{type(e).__name__}: {e}", "hint": _hint_for(e)}

    return wrapper


def _fmt_ts(t: float) -> str:
    t = int(t or 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    if seconds < 90 * 60:
        return f"about {round(seconds / 60)} min"
    return f"about {seconds / 3600:.1f} hours"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _trim(text: str, n: int = SNIPPET_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ Me.md frontmatter (line-based, body-preserving)

_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")


def _split_list(inner: str) -> list[str]:
    items, buf, quote = [], "", ""
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        else:
            buf += ch
    items.append(buf)
    return [i.strip() for i in items if i.strip()]


def parse_scalar(v: str) -> Any:
    v = v.strip()
    if v.startswith('"'):
        try:
            return json.loads(v)
        except ValueError:
            return v.strip('"')
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if v.startswith("[") and v.endswith("]"):
        return [parse_scalar(i) for i in _split_list(v[1:-1])]
    if v in ("", "~", "null"):
        return ""
    if v in ("true", "false"):
        return v == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _dump_scalar(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, list | tuple):
        return "[" + ", ".join(_dump_scalar(i) for i in v) + "]"
    s = str(v)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9 _./+()-]*", s) and s.lower() not in ("true", "false", "null", "yes", "no"):
        return s
    return json.dumps(s, ensure_ascii=False)


def _split_frontmatter(text: str) -> tuple[list[str] | None, list[str]]:
    """-> (frontmatter lines or None, body lines). Lines have no line ending."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return lines[1:i], lines[i + 1:]
    return None, lines


def _fm_blocks(fm: list[str]) -> list[list]:
    """[[key|None, [lines...]], ...] — continuation lines (indented / '- x') stay with their key."""
    blocks: list[list] = []
    for line in fm:
        m = _KEY_RE.match(line)
        if m:
            blocks.append([m.group(1), [line]])
        elif blocks and blocks[-1][0] is not None and (line.startswith((" ", "\t", "- ")) or line.strip() == "-"):
            blocks[-1][1].append(line)
        else:
            blocks.append([None, [line]])
    return blocks


def _block_value(block: list) -> Any:
    first = _KEY_RE.match(block[1][0]).group(2)  # type: ignore[union-attr]
    if first.strip() == "" and len(block[1]) > 1:
        return [parse_scalar(l.strip()[1:].strip()) for l in block[1][1:] if l.strip().startswith("-")]
    return parse_scalar(first)


def read_frontmatter(path: Path) -> dict:
    if not path.exists():
        return {}
    fm, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
    return {b[0]: _block_value(b) for b in _fm_blocks(fm or []) if b[0]}


def update_frontmatter(path: Path, updates: dict[str, Any]) -> None:
    """Change only the given keys; every other line (including the whole body) stays byte-for-byte."""
    text = path.read_bytes().decode("utf-8") if path.exists() else ""
    crlf = "\r\n" in text
    fm, body = _split_frontmatter(text)
    blocks = _fm_blocks(fm or [])
    for key, value in updates.items():
        new = [f"{key}: {_dump_scalar(value)}".rstrip()]
        for b in blocks:
            if b[0] == key:
                b[1] = new
                break
        else:
            blocks.append([key, new])
    fm_lines = [l for b in blocks for l in b[1]]
    out = ["---", *fm_lines, "---", *body] if (fm is not None or fm_lines) else body
    eol = "\r\n" if crlf else "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(eol.join(out), encoding="utf-8", newline="")
    os.replace(tmp, path)


# ------------------------------------------------------------------ the façade

class Brain:
    def __init__(self, vault: Path | str | None = None):
        root = Path(vault).expanduser() if vault else default_vault()
        self.paths = VaultPaths(root)
        self.paths.ensure_dirs()
        self.settings = Settings.load(self.paths)
        self.store = worker.SafeJobStore(self.paths.jobs_db)
        self._lock = threading.RLock()
        self._daily_lock = threading.Lock()
        self._daily_thread: threading.Thread | None = None
        self._last_spawn = 0.0
        self.init_warning = ""
        try:
            _mod("cbrain.vault.writer").VaultWriter(self.paths).ensure_vault()
        except Exception as e:  # noqa: BLE001 - a half-built sibling must not stop the façade
            self.init_warning = f"vault templates not written yet: {e}"
            worker.log_line(self.paths, f"WARN {self.init_warning}")

    # ---- small helpers -------------------------------------------------
    def _writer(self):
        return _mod("cbrain.vault.writer").VaultWriter(self.paths)

    def _index(self):
        return _mod("cbrain.vault.index").SearchIndex(self.paths.search_db)

    def _resolve(self, key: str) -> Creator:
        c = self.store.get_creator(key) or self.store.get_creator(slugify(key))
        if c is None:
            known = ", ".join(x.slug for x in self.store.list_creators()) or "none yet"
            raise BrainError(f"I don't know a creator called '{key}'.", f"Creators in this brain: {known}. Add one with add_creator.")
        return c

    def _resolve_many(self, keys: list[str] | None) -> list[Creator]:
        return [self._resolve(k) for k in keys] if keys else self.store.list_creators()

    def _label(self, creator: Creator, counts: dict | None = None) -> str:
        counts = counts if counts is not None else self.store.counts(creator.slug)
        updated = (creator.last_checked_at or now_iso())[:10]
        try:
            return _mod("cbrain.vault.writer").brain_label(creator, counts, updated)
        except Exception:  # noqa: BLE001
            return f"{creator.slug} · {creator.scope} · updated {updated} · {counts.get('total', 0)} videos"

    def _counts_by_creator(self) -> dict[str, dict]:
        out: dict[str, dict] = defaultdict(lambda: {"queued": 0, "fetching": 0, "fetched": 0, "written": 0,
                                                    "extracting": 0, "done": 0, "parked": 0})
        for r in self.store.db.execute(
                "SELECT creator_slug, state, COUNT(*) n FROM videos WHERE selected=1 GROUP BY creator_slug, state"):
            out[r["creator_slug"]][r["state"]] = r["n"]
        for d in out.values():
            d["total"] = sum(d.values())
        return out

    def _completeness(self, creator: Creator, counts: dict) -> dict:
        c = worker.counts_dict(counts)
        pending = sum(c.get(k, 0) for k in ("queued", "fetching", "fetched", "written", "extracting"))
        return {"name": creator.name, "brain": self._label(creator, counts), "percent_built": c["built_pct"],
                "percent_analyzed": c["analyzed_pct"], "videos_total": c["total"], "videos_searchable": c["built"],
                "still_processing": pending, "failed": c["failed"]}

    def _worker_running(self) -> bool:
        return WriterLock(self.paths.writer_lock).is_locked()

    def _paused(self) -> bool:
        return self.paths.pause_flag.exists()

    def _ensure_worker(self, force: bool = False) -> bool:
        """Start the background worker when there is something to do and none is running. Never blocks."""
        try:
            if self._paused() or not worker.has_pending_work(self.store, self.settings):
                return False
            if not force and time.monotonic() - self._last_spawn < 30:
                return False
            started = worker.start_background(self.paths)
            if started:
                self._last_spawn = time.monotonic()
            return started
        except Exception as e:  # noqa: BLE001
            worker.log_line(self.paths, f"WARN could not start worker: {e}")
            return False

    def _legacy_import(self, creator: Creator) -> int:
        legacy = os.environ.get("CBRAIN_LEGACY_DB", DEFAULT_LEGACY_DB)
        old = Path(legacy) if legacy else None
        if old is None or not old.exists():
            return 0
        try:
            n = int(_mod("cbrain.legacy_import").import_old_creator_brain(old, self.store, self.paths, creator) or 0)
            self.store.include_downloaded(creator.slug)  # transcripts already on disk cost nothing: keep them all
            return n
        except Exception as e:  # noqa: BLE001 - old data is a bonus, never a blocker
            worker.log_line(self.paths, f"WARN legacy import failed: {type(e).__name__}: {e}")
            return 0

    def _refresh_overview(self) -> None:
        try:
            worker.write_overview(self.paths, self.store, self._writer())
        except Exception as e:  # noqa: BLE001
            worker.log_line(self.paths, f"WARN overview not refreshed: {type(e).__name__}: {e}")

    def _iter_claims(self):
        yield from worker.iter_claim_dicts(self.paths)

    @staticmethod
    def _compact_claim(d: dict) -> dict:
        out = {k: d.get(k) for k in _CLAIM_FIELDS}
        if not out.get("url") and d.get("video_id"):
            out["url"] = f"https://youtu.be/{d['video_id']}?t={int(d.get('t') or 0)}"
        out["timestamp"] = _fmt_ts(d.get("t") or 0)
        return out

    # ---- adding + updating --------------------------------------------
    @_safe
    def add_creator(self, handle: str, full: bool = False, shorts: bool = False) -> dict:
        handle = (handle or "").strip()
        if not handle:
            raise BrainError("No creator given.", "Pass a handle like @JeremyEthier or a channel URL.")
        disc = _mod("cbrain.discovery")
        s = self.settings
        creator, videos = disc.list_channel(handle)
        existing = self.store.get_creator(creator.slug)
        creator.scope = "full" if (full or (existing and existing.scope == "full")) else "recent"
        creator.added_at = existing.added_at if existing and existing.added_at else now_iso()
        prio = disc.select(videos, creator.scope, recent_fraction=s.recent_fraction, top_winners=s.top_winners,
                           include_shorts=shorts or s.include_shorts, include_lives=s.include_lives)
        self.store.upsert_creator(creator)
        queued = self.store.add_videos(creator.slug, videos, prio)
        imported = self._legacy_import(creator)
        self.store.touch_checked(creator.slug)
        self._refresh_overview()
        started = self._ensure_worker(force=True)
        running = started or self._worker_running()
        creator = self.store.get_creator(creator.slug) or creator
        counts = self.store.counts(creator.slug)
        label = self._label(creator, counts)
        bits = [f"Added {creator.name}. Queued {len(queued)} of {len(videos)} videos "
                f"({'every video' if creator.scope == 'full' else 'newest 30% plus the top winners'})."]
        if imported:
            bits.append(f"Reused {imported} transcripts you already had.")
        if running:
            bits.append("Transcripts are downloading in the background; search works on whatever is ready, "
                        "and brain_status shows progress.")
        elif self._paused():
            bits.append("The worker is paused; run resume to start downloading.")
        else:
            bits.append("Nothing left to download.")
        return {"brain": label, "creator": creator.slug, "name": creator.name, "queued": len(queued),
                "imported": imported, "total_on_channel": len(videos), "scope": creator.scope,
                "worker_running": running, "message": " ".join(bits)}

    def _known_ids(self, ids: list[str]) -> set[str]:
        known: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            known |= {r[0] for r in self.store.db.execute(f"SELECT id FROM videos WHERE id IN ({marks})", chunk)}
        return known

    def _update_one(self, disc, c: Creator) -> dict:
        s = self.settings
        ident = c.handle or f"https://www.youtube.com/channel/{c.channel_id}"
        latest = disc.latest(ident, 15)
        known = self._known_ids([v.id for v in latest])
        new = [v for v in latest if v.id not in known]
        videos = latest
        if c.scope == "full" and latest and not known and self.store.counts(c.slug)["total"]:
            _, videos = disc.list_channel(ident)         # missed more than 15 videos: re-list everything
            new = [v for v in videos if v.id not in self._known_ids([x.id for x in videos])]
        prio = disc.select(new, "full", include_shorts=s.include_shorts, include_lives=s.include_lives) if new else {}
        queued = self.store.add_videos(c.slug, videos, prio)
        self.store.touch_checked(c.slug)
        titles = {v.id: v.title for v in videos}
        return {"creator": c.slug, "name": c.name, "new_videos": len(queued),
                "titles": [titles[i] for i in queued[:5] if i in titles]}

    @_safe
    def update(self, creator: str | None = None) -> dict:
        creators = [self._resolve(creator)] if creator else self.store.list_creators()
        if not creators:
            return {"updated": [], "new_videos": 0, "message": "No creators yet. Add one with add_creator."}
        disc = _mod("cbrain.discovery")
        results = []
        for c in creators:
            try:
                results.append(self._update_one(disc, c))
            except Exception as e:  # noqa: BLE001 - one bad channel must not stop the rest
                worker.log_line(self.paths, f"WARN update {c.slug}: {type(e).__name__}: {e}")
                results.append({"creator": c.slug, "name": c.name, "new_videos": 0, "error": f"{type(e).__name__}: {e}"})
        worker.update_manifest(self.paths, last_daily_check=now_iso())
        total = sum(r["new_videos"] for r in results)
        failed = [r["name"] for r in results if r.get("error")]
        started = self._ensure_worker(force=True) if total else False
        msg = (f"Checked {len(creators)} creator(s): {total} new video(s) found"
               + (" and queued for download." if total else ". Everything is up to date."))
        if failed:
            msg += f" Could not check: {', '.join(failed)} (will retry next time)."
        return {"updated": results, "new_videos": total, "worker_started": started, "message": msg}

    def maybe_daily_check(self) -> dict | None:
        """Non-blocking: starts `update` in a daemon thread at most once per daily_check_hours. Safe to call often."""
        try:
            self._ensure_worker()
            if not self.store.list_creators():
                return None
            last = _parse_iso(worker.read_manifest(self.paths).get("last_daily_check"))
            hours = self.settings.daily_check_hours
            if last is not None and (datetime.now(timezone.utc) - last).total_seconds() < hours * 3600:
                return None
            with self._daily_lock:
                if self._daily_thread is not None and self._daily_thread.is_alive():
                    return None
                worker.update_manifest(self.paths, last_daily_check=now_iso())   # claim the window first
                t = threading.Thread(target=self._daily_run, name="daily-check", daemon=True)
                self._daily_thread = t
                t.start()
            return {"started": True, "message": "Checking your creators for new videos in the background."}
        except Exception as e:  # noqa: BLE001
            worker.log_line(self.paths, f"WARN daily check not started: {type(e).__name__}: {e}")
            return None

    def _daily_run(self) -> None:
        other = copy.copy(self)                      # own DB connection: sqlite connections must not be shared by writers
        other._lock = threading.RLock()
        try:
            other.store = worker.SafeJobStore(self.paths.jobs_db)
            res = other.update()
            worker.log_line(self.paths, f"daily check: {res.get('message') or res.get('error')}")
        except Exception as e:  # noqa: BLE001
            worker.log_line(self.paths, f"WARN daily check failed: {type(e).__name__}: {e}")
        finally:
            store = getattr(other, "store", None)
            if store is not None and store is not self.store:
                try:
                    store.db.close()
                except Exception:  # noqa: BLE001
                    pass

    # ---- status --------------------------------------------------------
    def _parked_reasons(self, slug: str | None = None) -> list[dict]:
        q = "SELECT substr(COALESCE(last_error,'unknown'),1,80) r, COUNT(*) n FROM videos WHERE selected=1 AND state='parked'"
        args: list = []
        if slug:
            q, args = q + " AND creator_slug=?", [slug]
        return [{"reason": r["r"], "count": r["n"]} for r in self.store.db.execute(q + " GROUP BY r ORDER BY n DESC LIMIT 5", args)]

    def _proxy_stats(self) -> dict | None:
        if not (self.settings.use_free_proxies or self.settings.proxy_file):
            return None
        try:
            return dict(_mod("cbrain.proxies").ProxyPool(self.paths, self.settings).stats())
        except Exception:  # noqa: BLE001
            return None

    def _eta(self, remaining: int, proxy: dict | None) -> dict:
        s = self.settings
        healthy = 0
        if proxy:
            healthy = int(proxy.get("healthy", proxy.get("alive", proxy.get("healthy_count", 0))) or 0)
        lanes = max(1, s.lanes if healthy > 0 else 1)
        secs = remaining * s.own_ip_interval_seconds / lanes
        return {"seconds": round(secs), "lanes_assumed": lanes, "estimate": True,
                "text": f"{_fmt_eta(secs)} (rough estimate)" if remaining else "nothing left to download"}

    @_safe
    def status(self, creator: str | None = None) -> dict:
        creators = [self._resolve(creator)] if creator else self.store.list_creators()
        by = self._counts_by_creator()
        rows, tot = [], defaultdict(int)
        for c in creators:
            counts = by.get(c.slug) or {k: 0 for k in ("queued", "fetching", "fetched", "written", "extracting", "done", "parked", "total")}
            comp = self._completeness(c, counts)
            comp.update(slug=c.slug, last_checked_at=c.last_checked_at, counts=counts)
            rows.append(comp)
            for k, v in counts.items():
                tot[k] += v
        totals = worker.counts_dict(dict(tot)) if tot else worker.counts_dict({"total": 0})
        remaining = tot["queued"] + tot["fetching"]
        pending = remaining + tot["fetched"] + tot["written"] + tot["extracting"]
        proxy = self._proxy_stats()
        eta = self._eta(remaining, proxy)
        running, paused = self._worker_running(), self._paused()
        if len(rows) == 1:
            brain = rows[0]["brain"]
        else:
            brain = f"{len(rows)} creator(s) · {totals['built_pct']}% built"
        if not rows:
            msg = "The brain is empty. Add a creator with add_creator."
        else:
            msg = (f"{brain}. {totals['built_pct']}% searchable ({totals['built']} of {totals['total']} videos), "
                   f"{totals['analyzed_pct']}% analysed. ")
            msg += ("Paused. " if paused else "Worker is running. " if running else
                    "Worker is idle. " if not pending else "Worker is not running; it will restart on the next request. ")
            if remaining and not paused:
                msg += f"Transcripts left: {remaining}, {eta['text']}. "
            if totals["failed"]:
                msg += f"{totals['failed']} video(s) could not be fetched (run retry to try again)."
        return {"brain": brain, "creators": rows, "totals": totals, "percent_built": totals["built_pct"],
                "percent_analyzed": totals["analyzed_pct"], "pending_jobs": pending, "worker_running": running,
                "paused": paused, "parked_reasons": self._parked_reasons(creators[0].slug if creator and creators else None),
                "proxy": proxy, "eta": eta, "message": msg.strip()}

    # ---- search / topics -----------------------------------------------
    @_safe
    def search(self, query: str, creators: list[str] | None = None, limit: int = 8) -> dict:
        q = (query or "").strip()
        if not q:
            raise BrainError("Empty search.", "Pass some words to look for, e.g. 'protein per day'.")
        targets = self._resolve_many(creators)
        if not targets:
            return {"query": q, "results": [], "brain_completeness": {}, "message": "The brain is empty. Add a creator with add_creator first."}
        limit = max(1, min(int(limit or 8), 30))
        idx = self._index()
        hits = idx.search(q, creators=[c.slug for c in targets] if creators else None, limit=limit)
        results = [self._compact_hit(h) for h in hits]
        by = self._counts_by_creator()
        comp = {c.slug: self._completeness(c, by.get(c.slug) or {"total": 0}) for c in targets}
        low = [f"{v['name']} {v['percent_built']}%" for v in comp.values() if v["percent_built"] < 100]
        msg = f"{len(results)} match(es) for '{q}'." if results else f"No matches for '{q}'."
        if low:
            msg += " Brain not fully built yet (" + ", ".join(low) + "): say so, results may be missing videos."
        return {"query": q, "results": results, "brain_completeness": comp, "message": msg,
                "cite": "Cite the video title and the timestamp URL for every point you use."}

    @staticmethod
    def _compact_hit(h: dict) -> dict:
        t = h.get("t") or 0
        url = h.get("url") or (f"https://youtu.be/{h.get('video_id')}?t={int(t)}" if h.get("video_id") else "")
        return {"creator": h.get("creator"), "title": h.get("title"), "published": h.get("published"), "t": t,
                "timestamp": _fmt_ts(t), "url": url, "snippet": _trim(h.get("text", "")), "kind": h.get("kind"),
                "score": round(float(h.get("score") or 0), 3)}

    @staticmethod
    def _topic_key(s: str) -> str:
        words = re.sub(r"[^a-z0-9]+", " ", s.lower()).split()
        return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)

    def _find_topic(self, name: str) -> tuple[str | None, list[str], list[str]]:
        """-> (canonical, its aliases, suggestions). Read-only: never registers a new topic."""
        try:
            data = json.loads(self.paths.topics_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        keymap: dict[str, str] = {}
        for canon, info in data.items():
            keymap.setdefault(self._topic_key(canon), canon)
            for a in (info or {}).get("aliases", []):
                keymap.setdefault(self._topic_key(a), canon)
        k = self._topic_key(name)
        canon = keymap.get(k)
        if canon is None and k:
            partial = sorted((kk for kk in keymap if k in kk or kk in k), key=len)
            canon = keymap[partial[0]] if partial else None
        sugg = [keymap[m] for m in difflib.get_close_matches(k, list(keymap), n=5, cutoff=0.5)]
        return canon, list((data.get(canon) or {}).get("aliases", [])) if canon else [], list(dict.fromkeys(sugg))

    def _claims_for(self, canon: str, aliases: list[str], slugs: set[str] | None = None) -> list[dict]:
        names = {canon.lower(), *[a.lower() for a in aliases]}
        out = [d for d in self._iter_claims()
               if str(d.get("topic", "")).lower() in names and (slugs is None or d.get("creator") in slugs)]
        out.sort(key=lambda d: d.get("date") or "", reverse=True)
        return out

    @_safe
    def topic(self, name: str) -> dict:
        canon, aliases, sugg = self._find_topic(name)
        if canon is None:
            hits = self._index().search(name, limit=5) if self.store.list_creators() else []
            return {"topic": name, "found": False, "claims": [], "suggestions": sugg,
                    "transcript_matches": [self._compact_hit(h) for h in hits],
                    "message": f"No topic note called '{name}' yet."
                               + (f" Did you mean: {', '.join(sugg)}?" if sugg else "")
                               + " Transcript matches are included; use search_brain for more."}
        claims = self._claims_for(canon, aliases)
        note = ""
        try:
            note = self.paths.topic_note(canon).read_text(encoding="utf-8")[:3000]
        except OSError:
            pass
        creators = sorted({d.get("creator") for d in claims if d.get("creator")})
        return {"topic": canon, "found": True, "aliases": aliases, "creators": creators, "claim_count": len(claims),
                "claims": [self._compact_claim(d) for d in claims[:30]], "note": note,
                "note_path": str(self.paths.topic_note(canon)),
                "message": f"{len(claims)} verified claim(s) about {canon} from {len(creators)} creator(s). "
                           "Cite the timestamp URLs."}

    @_safe
    def compare(self, topic: str, creators: list[str] | None = None) -> dict:
        targets = self._resolve_many(creators)
        if not targets:
            raise BrainError("No creators to compare.", "Add creators first with add_creator.")
        canon, aliases, _ = self._find_topic(topic)
        slugs = {c.slug for c in targets}
        claims = self._claims_for(canon, aliases, slugs) if canon else []
        idx = self._index()
        by: dict[str, dict] = {}
        for c in targets:
            mine = [d for d in claims if d.get("creator") == c.slug]
            hits = idx.search(topic, creators=[c.slug], limit=3, kind="chunks")
            by[c.slug] = {"name": c.name, "claims": [self._compact_claim(d) for d in mine[:8]],
                          "claim_count": len(mine), "transcript_matches": [self._compact_hit(h) for h in hits]}
        silent = [v["name"] for v in by.values() if not v["claims"] and not v["transcript_matches"]]
        msg = f"Compared {len(targets)} creator(s) on '{canon or topic}'."
        if silent:
            msg += f" Nothing found from: {', '.join(silent)}."
        return {"topic": canon or topic, "topic_found": canon is not None, "by_creator": by, "message": msg,
                "cite": "Quote each creator with the timestamp URL so differences can be checked."}

    # ---- profile / outputs --------------------------------------------
    @_safe
    def profile(self, updates: dict | None = None) -> dict:
        path = self.paths.me
        if not path.exists():
            path.write_text("---\n---\n# Me\n\nTell me about yourself here; your AI agent reads this to personalise plans.\n",
                            encoding="utf-8")
        applied, ignored = {}, []
        for k, v in (updates or {}).items():
            key = PROFILE_ALIASES.get(str(k).strip().lower().replace(" ", "_"), str(k).strip().lower().replace(" ", "_"))
            if key not in PROFILE_KEYS:
                ignored.append(k)
                continue
            if isinstance(v, str) and key in ("age", "weight", "days_per_week"):
                p = parse_scalar(v)
                v = p if isinstance(p, int | float) else v
            applied[key] = v
        if applied:
            update_frontmatter(path, applied)
        fm = read_frontmatter(path)
        prof = {k: fm.get(k, "") for k in PROFILE_KEYS}
        missing = [k for k, v in prof.items() if v in ("", [], None)]
        msg = (f"Profile has {len(PROFILE_KEYS) - len(missing)} of {len(PROFILE_KEYS)} fields filled."
               + (f" Still unknown: {', '.join(missing)}. Ask the user before personalising." if missing else ""))
        if applied:
            msg = f"Updated {', '.join(applied)}. " + msg
        if ignored:
            msg += f" Ignored unknown fields: {', '.join(map(str, ignored))} (allowed: {', '.join(PROFILE_KEYS)})."
        return {"profile": prof, "path": str(path), "updated": list(applied), "missing": missing, "message": msg}

    @_safe
    def save_output(self, title: str, markdown: str, sources: list[str] | None = None) -> dict:
        if not (title or "").strip() or not (markdown or "").strip():
            raise BrainError("Both a title and some markdown are needed.", "Pass title and markdown (the plan/answer text).")
        path = Path(self._writer().write_output(title.strip(), markdown, list(sources or [])))
        try:
            rel = path.relative_to(self.paths.root).as_posix()
        except ValueError:
            rel = str(path)
        return {"path": str(path), "vault_path": rel, "link": f"[[{path.stem}]]",
                "message": f"Saved to {rel}. Open the vault in Obsidian to see it linked to its sources."}

    # ---- discovery of new creators ------------------------------------
    def _ytsearch(self, query: str, n: int = 30) -> list[dict]:
        from yt_dlp import YoutubeDL
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
                "ignoreconfig": True, "socket_timeout": 15}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
        return list((info or {}).get("entries") or [])

    @_safe
    def suggest_creators(self, topic: str, n: int = 5) -> dict:
        topic = (topic or "").strip()
        if not topic:
            raise BrainError("No topic given.", "Pass a topic like 'hypertrophy training'.")
        have_ids = {c.channel_id for c in self.store.list_creators()}
        have_names = {c.name.lower() for c in self.store.list_creators()}
        groups: dict[str, dict] = {}
        for e in self._ytsearch(topic):
            cid = e.get("channel_id") or e.get("uploader_id") or e.get("channel") or e.get("uploader")
            name = e.get("channel") or e.get("uploader") or ""
            if not cid or cid in have_ids or name.lower() in have_names:
                continue
            g = groups.setdefault(cid, {"name": name, "channel_id": e.get("channel_id"),
                                        "url": e.get("channel_url") or e.get("uploader_url") or "",
                                        "handle": "", "total_views": 0, "videos_in_results": 0, "example_titles": []})
            uid = e.get("uploader_id") or ""
            if isinstance(uid, str) and uid.startswith("@"):
                g["handle"] = uid
            g["total_views"] += int(e.get("view_count") or 0)
            g["videos_in_results"] += 1
            if len(g["example_titles"]) < 3 and e.get("title"):
                g["example_titles"].append(e["title"])
        ranked = sorted(groups.values(), key=lambda g: g["total_views"], reverse=True)[: max(1, int(n))]
        for g in ranked:
            g["add_with"] = g["handle"] or g["url"] or g["name"]
        msg = (f"Top {len(ranked)} channel(s) for '{topic}' not in your brain yet. Ask the user which to add, then call add_creator."
               if ranked else f"No new channels found for '{topic}'.")
        return {"topic": topic, "suggestions": ranked, "message": msg}

    # ---- control -------------------------------------------------------
    @_safe
    def pause(self) -> dict:
        self.paths.brain.mkdir(parents=True, exist_ok=True)
        self.paths.pause_flag.write_text(now_iso(), encoding="utf-8")
        return {"paused": True, "message": "Paused. The worker will stop after the videos it is on; nothing is lost."}

    @_safe
    def resume(self) -> dict:
        try:
            self.paths.pause_flag.unlink()
        except FileNotFoundError:
            pass
        started = self._ensure_worker(force=True)
        return {"paused": False, "worker_started": started,
                "message": "Resumed. Downloading continues in the background." if started or self._worker_running()
                else "Resumed. Nothing is waiting to be downloaded."}

    @_safe
    def add_videos(self, creator: str, match: str | None = None, video_ids: list[str] | None = None,
                   top: int | None = None, dry_run: bool = False) -> dict:
        """Bring more of a creator's catalog into the brain: by title words, explicit ids, or top-N by views."""
        cr = self._resolve(creator)
        pool = [r for r in self.store.videos(cr.slug, selected_only=False) if not r.selected]
        picked = []
        if video_ids:
            want = {v.strip() for v in video_ids if v and v.strip()}
            picked += [r for r in pool if r.id in want]
        if match:
            words = [w for w in re.findall(r"\w+", match.lower()) if w]
            picked += [r for r in pool if words and all(w in (r.title or "").lower() for w in words)]
        if top:
            picked += sorted(pool, key=lambda r: -(r.views or 0))[:top]
        seen, unique = set(), []
        for r in picked:
            if r.id not in seen:
                seen.add(r.id)
                unique.append(r)
        preview = [{"id": r.id, "title": r.title, "views": r.views, "url": f"https://www.youtube.com/watch?v={r.id}"}
                   for r in unique[:50]]
        if dry_run or not unique:
            msg = (f"{len(unique)} video(s) match and are not in the brain yet." if unique
                   else "No matching videos outside the brain. Check the creator's 'All Videos' note.")
            return {"matched": len(unique), "videos": preview, "added": 0, "message": msg}
        added = self.store.select_more(cr.slug, [r.id for r in unique])
        started = self._ensure_worker(force=True) if added else False
        self._refresh_overview()
        return {"matched": len(unique), "added": len(added), "videos": preview, "worker_started": started,
                "message": f"Added {len(added)} video(s) to {cr.name}'s brain; they download in the background."}

    # ---- agent mode: the user's own AI does the reading, code does the checking ------------------------
    @_safe
    def analysis_next(self, creator: str | None = None) -> dict:
        """Lease the next video that still needs claims and hand its transcript + rules to the agent."""
        slug = self._resolve(creator).slug if creator else None
        rows = self.store.claim(states=("written",), limit=1, lease_seconds=1800, to_state="extracting",
                                ignore_backoff=True, creator_slug=slug)  # the agent needs no Groq quota
        if not rows:
            counts = self.store.counts(slug)
            return {"video": None, "message": "Nothing left to analyse." if not counts.get("written") else
                    "Remaining videos are leased by another worker; try again in a few minutes.", "counts": counts}
        row = rows[0]
        cr = self.store.get_creator(row.creator_slug)
        ex = _mod("cbrain.think.extract")
        transcript = _mod("cbrain.think.pipeline").Thinker(self.store, self.paths, self.settings, None, None
                                                          )._load_transcript(row.id)
        known = sorted(_mod("cbrain.think.topics").TopicRegistry(self.paths).all_topics())[:150]
        task = ex.write_agent_batch(self.paths, cr, row.meta(), transcript, known).read_text(encoding="utf-8")
        head, _, rest = task.partition("## What to write")
        if rest:  # drop only the file-writing instructions (they don't apply over MCP); keep the transcript
            task = head + "## Transcript" + rest.split("## Transcript", 1)[-1]
        left = self.store.counts(slug).get("written", 0)
        return {"video_id": row.id, "title": row.title, "creator": cr.name, "videos_left": left, "task": task,
                "message": f"Read the task, then call submit_claims('{row.id}', claims). {left} more after this."}

    @_safe
    def analysis_submit(self, video_id: str, claims: list) -> dict:
        """Verify the agent's claims against the transcript (code, not AI) and add the kept ones to the brain."""
        row = self.store.get(video_id)
        if row is None:
            return {"error": f"unknown video {video_id}", "hint": "use the video_id from next_video_to_analyse"}
        cr = self.store.get_creator(row.creator_slug)
        ex = _mod("cbrain.think.extract")
        raw = ex.parse_claims_payload({"claims": claims}, cr.slug, row.id, row.published)
        thinker = _mod("cbrain.think.pipeline").Thinker(self.store, self.paths, self.settings, self._writer(),
                                                         self._index())
        kept = thinker.process(row, cr, raw_claims=raw)
        self.store.mark(row.id, "done")
        _mod("cbrain.vault.relink").relink(self.paths, self.store, self._writer(), only_videos={row.id})
        rejected = len(raw) - len(kept)
        return {"video_id": row.id, "submitted": len(claims), "kept": len(kept), "rejected": rejected,
                "topics": sorted({c.topic for c in kept}),
                "message": f"Kept {len(kept)} verified claims" + (f"; {rejected} rejected because the quote is not in "
                           "the transcript word for word" if rejected else "") + "."}

    @_safe
    def topic_overview(self) -> dict:
        """Every topic with its claim count and creators, so the agent can plan merges and names."""
        counts: dict = {}
        for c in self._iter_claims():
            t = c.get("topic") or ""
            d = counts.setdefault(t, {"claims": 0, "creators": set()})
            d["claims"] += 1
            d["creators"].add(c.get("creator"))
        rows = sorted(({"topic": t, "claims": d["claims"], "creators": sorted(d["creators"])} for t, d in counts.items()),
                      key=lambda r: -r["claims"])
        return {"topics": rows, "message": f"{len(rows)} topics."}

    @_safe
    def apply_topic_groups(self, groups: dict) -> dict:
        """Merge topics the agent grouped ({new name: [old names]}); code checks names and relinks every note."""
        if self._worker_running():
            return {"error": "the background worker is running", "hint": "call pause_brain first, then retry"}
        mg = _mod("cbrain.think.merge")
        known = {r["topic"] for r in self.topic_overview()["topics"]}
        clean = {}
        for name, members in (groups or {}).items():
            ms = [m for m in members if m in known]
            if name and ms:
                clean[name] = ms
        res = mg.apply_groups(self.paths, self.store, self._writer(), self._index(), clean)
        res.update(_mod("cbrain.vault.relink").relink(self.paths, self.store, self._writer()))
        self._refresh_overview()
        res["message"] = f"Merged {res.get('merged_topics', 0)} topic names; {res.get('topics_now')} topics now."
        return res

    @_safe
    def rebuild(self, merge: bool = True) -> dict:
        """Re-render the whole vault: readable names, key points, topic + related links, dashboards.
        With merge=True (and a Groq key) narrow topics are folded together first."""
        if self._worker_running():
            return {"error": "the background worker is running", "hint": "run `brain pause`, wait a minute, retry"}
        out: dict = {}
        if merge and _mod("cbrain.think.llm").LLMClient(self.settings).available():
            m = self.merge_topics()
            out["merge"] = {k: m.get(k) for k in ("topics_before", "topics_now", "merged_topics", "error")}
        writer = self._writer()
        out.update(_mod("cbrain.vault.relink").relink(self.paths, self.store, writer, rename_all=True))
        _mod("cbrain.vault.graph").write_obsidian_config(self.paths)
        self._refresh_overview()
        out["doctor"] = {i["check"]: i["count"] for i in self.doctor().get("issues", [])}
        out["message"] = (f"Rebuilt {out['videos_relinked']} video notes and {out['topics_relinked']} topic notes."
                          + (f" Topics: {out['merge'].get('topics_before')} -> {out['merge'].get('topics_now')}."
                             if out.get("merge", {}).get("topics_now") else ""))
        return out

    def graph_url(self, port: int = 8787) -> dict:
        """Start (once) the live graph page in a background thread and return its URL."""
        if not getattr(self, "_graph_thread", None):
            lg = _mod("cbrain.livegraph")
            server = lg.make_server(self.paths.root, port)
            self._graph_thread = threading.Thread(target=server.serve_forever, name="livegraph", daemon=True)
            self._graph_thread.start()
            self._graph_port = server.server_address[1]
        url = f"http://127.0.0.1:{self._graph_port}/"
        return {"url": url, "message": f"Live brain graph: {url} (updates every 2 s; Replay animates how it was built)."}

    @_safe
    def merge_topics(self, dry_run: bool = False) -> dict:
        """Fold narrow topics into broader ones (LLM proposes groups of existing names; code applies them)."""
        if self._worker_running() and not dry_run:
            return {"error": "the background worker is running", "hint": "run `brain pause`, wait a minute, retry"}
        llm = _mod("cbrain.think.llm").LLMClient(self.settings)
        if not llm.available():
            return {"error": "no Groq key", "hint": "run `brain setup` or set GROQ_API_KEY"}
        res = _mod("cbrain.think.merge").consolidate(self.paths, self.store, self._writer(), self._index(), llm,
                                                     dry_run=dry_run)
        if not dry_run:
            self._refresh_overview()
        return res

    @_safe
    def backfill_dates(self, creator: str | None = None) -> dict:
        """Look up missing publish dates (imported transcripts) through proxies and rename/relink their notes."""
        if self._worker_running():
            return {"error": "the background worker is running", "hint": "run `brain pause`, wait a minute, retry"}
        slug = self._resolve(creator).slug if creator else None
        res = _mod("cbrain.backfill").run(self.paths, self.store, self.settings, slug)
        self._refresh_overview()
        return res

    @_safe
    def retry_parked(self, creator: str | None = None) -> dict:
        slug = self._resolve(creator).slug if creator else None
        n = self.store.unpark(slug)
        started = self._ensure_worker(force=True) if n else False
        return {"retried": n, "worker_started": started,
                "message": f"Re-queued {n} failed video(s)." if n else "No failed videos to retry."}

    @_safe
    def doctor(self, fix: bool = False) -> dict:
        try:
            idx = self._index()
        except Exception:  # noqa: BLE001
            idx = None
        return _mod("cbrain.doctor").check(self.paths, self.store, idx, fix=fix)
