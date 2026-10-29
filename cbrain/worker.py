"""Background worker: the single writer of a vault.

`start_background(paths)` spawns it detached; `main()` is its entry point (`python -m cbrain.worker --vault X`).
It fetches transcripts, writes notes, extracts claims and exits when no due work is left.
Sibling modules are imported lazily so this file imports even while they are unfinished.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import gzip
import importlib
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .jobs import JobStore, WriterLock, now_iso
from .models import Claim, JobState, Transcript, VideoMeta
from .paths import VaultPaths, default_vault

MANIFEST_VERSION = 1
LOG_MAX_BYTES = 5 * 1024 * 1024
MAX_DEFERRED_WAIT = 3 * 60 * 60  # keep waiting through Groq quota rests (deferred jobs) before exiting
OVERVIEW_REFRESH_SECONDS = 300   # Home / creator notes / All Videos tables refresh this often during a run
LOG_NAME = "cbrain.worker"

log = logging.getLogger(LOG_NAME)


def _mod(name: str):
    return importlib.import_module(name)


# ---------------------------------------------------------------- thread-safe store

class _LockedConn:
    """sqlite connection proxy: statements from different threads never interleave with an open transaction."""

    def __init__(self, conn, lock):
        self._conn, self._lock = conn, lock

    def execute(self, *a, **kw):
        with self._lock:
            return self._conn.execute(*a, **kw)

    def executescript(self, *a, **kw):
        with self._lock:
            return self._conn.executescript(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class SafeJobStore(JobStore):
    """JobStore whose single connection can be shared by fetch lanes, the thinker and note writers.

    jobs.JobStore.tx() issues BEGIN IMMEDIATE on a shared connection, which raises "cannot start a transaction
    within a transaction" when two threads claim at once. An RLock around every statement and transaction fixes it.
    """

    def __init__(self, db_path: Path):
        super().__init__(db_path)
        self._rlock = threading.RLock()
        self.db = _LockedConn(self.db, self._rlock)

    @contextlib.contextmanager
    def tx(self):
        with self._rlock:
            with super().tx():
                yield


# ---------------------------------------------------------------- manifest / log helpers

def read_manifest(paths: VaultPaths) -> dict:
    try:
        return json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_manifest(paths: VaultPaths, **fields) -> dict:
    """Merge fields into manifest.json (atomic write)."""
    data = read_manifest(paths)
    data.update(fields)
    data["schema_version"] = MANIFEST_VERSION
    paths.brain.mkdir(parents=True, exist_ok=True)
    tmp = paths.manifest.with_name(paths.manifest.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, paths.manifest)
    return data


def rotate_log(paths: VaultPaths, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Rotate before any process holds the file open (Windows cannot rename an open file)."""
    try:
        if paths.log.exists() and paths.log.stat().st_size > max_bytes:
            os.replace(paths.log, paths.log.with_name(paths.log.name + ".1"))
    except OSError:
        pass


class _SafeRotatingHandler(logging.handlers.RotatingFileHandler):
    """Rotating handler that never raises when the OS refuses to rename an open log (Windows)."""

    def doRollover(self) -> None:  # noqa: N802
        try:
            super().doRollover()
        except OSError:
            self.maxBytes = 0  # stop trying; start-up rotation will clean up later
            if self.stream is None:
                self.stream = self._open()


def setup_logging(paths: VaultPaths) -> None:
    paths.brain.mkdir(parents=True, exist_ok=True)
    for h in list(log.handlers):
        log.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = _SafeRotatingHandler(paths.log, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr is not None and sys.stderr.isatty():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)
    log.propagate = False


def log_line(paths: VaultPaths, message: str) -> None:
    """Append one timestamped line to worker.log from any process (no rotation)."""
    try:
        paths.brain.mkdir(parents=True, exist_ok=True)
        with open(paths.log, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- shared helpers

def load_transcript(paths: VaultPaths, video_id: str) -> Transcript:
    with gzip.open(paths.transcript(video_id), "rt", encoding="utf-8") as f:
        return Transcript.from_dict(json.load(f))


def counts_dict(counts: dict) -> dict:
    """store.counts() plus the convenience keys the vault writer shows on Home."""
    c = dict(counts)
    total = c.get("total", 0)
    built = sum(c.get(k, 0) for k in ("written", "extracting", "done"))
    c["failed"] = c.get("parked", 0)
    c["built"] = built
    c["built_pct"] = round(100 * built / total) if total else 0
    c["analyzed_pct"] = round(100 * c.get("done", 0) / total) if total else 0
    return c


def has_pending_work(store: JobStore, settings: Settings | None = None, due_only: bool = False) -> bool:
    """True when the worker still has something to do. Written videos only count when claims can be extracted."""
    t = time.time() if due_only else 1e18
    states = ["queued", "fetched"]
    if settings is None or settings.groq_api_key:
        states.append("written")
    marks = ",".join("?" * len(states))
    row = store.db.execute(
        f"SELECT 1 FROM videos WHERE selected=1 AND state IN ({marks}) AND next_try_at<=? LIMIT 1", (*states, t)
    ).fetchone()
    return row is not None


def _next_deferred_in(store: JobStore) -> float | None:
    now = time.time()
    r = store.db.execute(
        "SELECT MIN(next_try_at) m FROM videos WHERE selected=1 AND state IN ('queued','fetched') AND next_try_at>?",
        (now,)).fetchone()
    return (r["m"] - now) if r and r["m"] else None


def write_overview(paths: VaultPaths, store: JobStore, writer, creators=None) -> None:
    """Refresh every creator note and Home from the job store + claims."""
    creators = creators if creators is not None else store.list_creators()
    topic_counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for c in iter_claim_dicts(paths):
        topic_counts[c.get("creator", "")][c.get("topic", "")] += 1
    by_creator: dict[str, dict] = {}
    for cr in creators:
        counts = counts_dict(store.counts(cr.slug))
        by_creator[cr.slug] = counts
        if cr.name != cr.slug:
            by_creator[cr.name] = counts   # tolerate writers keying by name
        top = [t for t, _ in topic_counts[cr.slug].most_common(8) if t]
        try:
            ups = sorted((p.stem for p in paths.updates_dir(cr.name).glob("*.md")), reverse=True)[:5]
        except OSError:
            ups = []
        writer.write_creator_note(cr, counts, top, ups)
        if hasattr(writer, "write_catalog"):
            writer.write_catalog(cr, store.videos(cr.slug, selected_only=False))
    try:
        all_topics = sorted(json.loads(paths.topics_json.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        all_topics = []
    writer.write_home(creators, by_creator, all_topics)


def iter_claim_dicts(paths: VaultPaths, start: int = 0):
    """Stream claims.jsonl lines as dicts (bad lines skipped). Never loads the whole file."""
    try:
        f = open(paths.claims, "rb")
    except OSError:
        return
    with f:
        if start:
            f.seek(start)
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


# ---------------------------------------------------------------- spawning

def start_background(paths: VaultPaths) -> bool:
    """Spawn a detached worker unless one already holds the vault lock. Returns True when one was started."""
    if WriterLock(paths.writer_lock).is_locked():
        return False
    paths.brain.mkdir(parents=True, exist_ok=True)
    rotate_log(paths)
    cmd = [sys.executable, "-m", "cbrain.worker", "--vault", str(paths.root)]
    env = dict(os.environ)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    logf = open(paths.log, "ab")
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, env=env,
                         close_fds=True, **kwargs)
    finally:
        logf.close()
    return True


# ---------------------------------------------------------------- the worker

class Worker:
    """One run of the pipeline. Create after the WriterLock is held."""

    pause_poll = 2.0     # seconds between pause-flag checks
    idle_sleep = 2.0     # thinker poll while the fetcher is still producing

    def __init__(self, paths: VaultPaths, settings: Settings | None = None, stop: threading.Event | None = None):
        self.paths = paths
        self.settings = settings or Settings.load(paths)
        self.stop = stop or threading.Event()
        self.store = SafeJobStore(paths.jobs_db)
        self._wlock = threading.Lock()                       # serialises note + index writes
        self.new_videos: dict[str, list[VideoMeta]] = collections.defaultdict(list)
        self._creators: dict[str, object] = {}
        self.stats: dict = {"fetched": 0, "written": 0, "write_errors": 0}
        self.writer = self.index = self.runner = self.thinker = self.pool = None
        self.changes: list[dict] = []

    # ---- setup
    def _build(self) -> None:
        s, p = self.settings, self.paths
        self.writer = _mod("cbrain.vault.writer").VaultWriter(p)
        self.writer.ensure_vault()
        self.index = _mod("cbrain.vault.index").SearchIndex(p.search_db)
        if s.use_free_proxies or s.proxy_file:
            try:
                self.pool = _mod("cbrain.proxies").ProxyPool(p, s)
            except Exception:
                log.exception("proxy pool unavailable; fetching on own IP only")
        self.runner = _mod("cbrain.fetch.runner").FetchRunner(self.store, p, s, self.pool, self._on_fetched)
        self.thinker = _mod("cbrain.think.pipeline").Thinker(self.store, p, s, self.writer, self.index)

    def _refresh_creators(self) -> None:
        self._creators = {c.slug: c for c in self.store.list_creators()}

    # ---- note writing
    def _on_fetched(self, row, transcript: Transcript, info: dict | None = None) -> None:
        """Called by fetch lanes (threads). Must never raise into the runner."""
        try:
            with self._wlock:
                self._write_one(row, transcript, info or {})
        except Exception as e:
            self.stats["write_errors"] += 1
            log.exception("writing %s failed", row.id)
            with contextlib.suppress(Exception):
                self.store.fail(row.id, f"write failed: {e}", back_to=JobState.FETCHED,
                                max_attempts=self.settings.max_attempts)

    def _write_one(self, row, transcript: Transcript, info: dict) -> None:
        creator = self._creators.get(row.creator_slug) or self.store.get_creator(row.creator_slug)
        if creator is None:
            raise RuntimeError(f"unknown creator {row.creator_slug}")
        self._creators[creator.slug] = creator
        meta = row.meta()
        meta.channel_id = creator.channel_id
        self._fill_missing(meta, info)
        self.writer.write_video_note(creator, meta, transcript)
        self.index.index_transcript(creator, meta, transcript)
        cur = self.store.get(row.id)
        if cur is None or cur.state in (JobState.FETCHING.value, JobState.FETCHED.value, JobState.QUEUED.value):
            self.store.mark(row.id, JobState.WRITTEN)
        self.new_videos[creator.slug].append(meta)
        self.stats["written"] += 1

    def _fill_missing(self, meta: VideoMeta, info: dict) -> None:
        """Flat listings often lack date/duration; take them from the full info fetched with the captions."""
        published = info.get("published") or info.get("upload_date")
        if published and len(str(published)) == 8 and str(published).isdigit():
            published = f"{published[:4]}-{published[4:6]}-{published[6:]}"
        title = (info.get("title") or "").strip()
        duration, views = info.get("duration"), info.get("view_count", info.get("views"))
        if not meta.published and published:
            meta.published = str(published)
            self.store.db.execute("UPDATE videos SET published=? WHERE id=? AND published IS NULL", (meta.published, meta.id))
        if title and (not meta.title or meta.title == meta.id):
            meta.title = title
            self.store.db.execute("UPDATE videos SET title=? WHERE id=?", (title, meta.id))
        if meta.duration is None and duration:
            meta.duration = int(duration)
            self.store.db.execute("UPDATE videos SET duration=? WHERE id=? AND duration IS NULL", (meta.duration, meta.id))
        if meta.views is None and views is not None:
            meta.views = int(views)
            self.store.db.execute("UPDATE videos SET views=? WHERE id=? AND views IS NULL", (meta.views, meta.id))

    def _write_pending(self) -> int:
        """Fetched-but-unwritten rows (crash recovery, legacy import): write notes + index them."""
        done = 0
        while not self.stop.is_set():
            rows = [self.store._row(r) for r in self.store.db.execute(
                "SELECT * FROM videos WHERE state='fetched' AND selected=1 AND next_try_at<=? LIMIT 100", (time.time(),))]
            if not rows:
                break
            progressed = False
            for r in rows:
                if self.stop.is_set():
                    break
                if not self.paths.transcript(r.id).exists():
                    log.warning("transcript file missing for %s; re-queueing", r.id)
                    self.store.mark(r.id, JobState.QUEUED)
                    progressed = True
                    continue
                try:
                    tr = load_transcript(self.paths, r.id)
                except Exception:
                    log.exception("unreadable transcript for %s; re-queueing", r.id)
                    self.store.mark(r.id, JobState.QUEUED)
                    progressed = True
                    continue
                self._on_fetched(r, tr, {})
                cur = self.store.get(r.id)
                if cur is None or cur.state != JobState.FETCHED.value:
                    progressed = True
                    done += 1
            if not progressed:
                break
        return done

    # ---- running
    def _snapshot(self) -> tuple:
        c = self.store.counts()
        return tuple(c.get(k, 0) for k in ("queued", "fetched", "written", "extracting", "done", "parked"))

    def _queued_due(self) -> bool:
        return self.store.db.execute(
            "SELECT 1 FROM videos WHERE state='queued' AND selected=1 AND next_try_at<=? LIMIT 1", (time.time(),)
        ).fetchone() is not None

    def _sleep(self, seconds: float) -> None:
        self.stop.wait(seconds)

    def _run_parallel(self) -> None:
        fetch_done = threading.Event()

        def fetch() -> None:
            try:
                if not self._queued_due():     # an idle FetchRunner with a proxy pool can wait ~15 s; skip it
                    return
                st = self.runner.run(stop=self.stop)
                if isinstance(st, dict):
                    self.stats["fetch"] = st
                    self.stats["fetched"] += int(st.get("fetched", st.get("ok", 0)) or 0)
            except Exception:
                log.exception("fetch runner crashed")
            finally:
                fetch_done.set()

        def think() -> None:
            last_written = None
            while not self.stop.is_set():
                try:
                    st = self.thinker.run(stop=self.stop)
                    if isinstance(st, dict):
                        self.stats["think"] = st
                        self.changes.extend(st.get("changed") or [])
                except Exception:
                    log.exception("thinker crashed; continuing without claim extraction")
                    break
                written = self.store.counts().get("written", 0)
                if fetch_done.is_set() and (written == 0 or written == last_written):
                    break
                last_written = written
                if not fetch_done.is_set():
                    self._sleep(self.idle_sleep)

        threads = [threading.Thread(target=fetch, name="fetch", daemon=True),
                   threading.Thread(target=think, name="think", daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            while t.is_alive():
                t.join(0.5)

    def _overview_loop(self, done: threading.Event) -> None:
        """Keep Home, creator notes and All-Videos tables live while a long run is in progress."""
        while True:
            try:
                write_overview(self.paths, self.store, self.writer)
            except Exception:  # noqa: BLE001 - a dashboard refresh must never stop the run
                log.exception("overview refresh failed")
            if done.wait(OVERVIEW_REFRESH_SECONDS):
                return

    def _monitor_pause(self, done: threading.Event) -> None:
        while not done.is_set():
            if self.paths.pause_flag.exists():
                log.info("pause flag found; stopping after current videos")
                self.stop.set()
                return
            done.wait(self.pause_poll)

    def run(self) -> int:
        paths, store = self.paths, self.store
        if paths.pause_flag.exists():
            log.info("paused (%s); nothing to do", paths.pause_flag.name)
            return 0
        claims_offset = paths.claims.stat().st_size if paths.claims.exists() else 0
        started = now_iso()
        update_manifest(paths, worker_started_at=started)
        released = store.release_stale_leases()
        if released:
            log.info("released %d stale leases", released)
        self._build()
        self._refresh_creators()
        if self.pool is not None:
            threading.Thread(target=self._safe_refresh_pool, name="proxy-refresh", daemon=True).start()
        monitor_done = threading.Event()
        threading.Thread(target=self._monitor_pause, args=(monitor_done,), name="pause-monitor", daemon=True).start()
        threading.Thread(target=self._overview_loop, args=(monitor_done,), name="overview", daemon=True).start()
        waited = 0.0
        idle = 0
        try:
            while not self.stop.is_set():
                self._refresh_creators()
                self._write_pending()
                before = self._snapshot()
                if any(before[:4]):  # queued / fetched / written / extracting
                    # (written rows still get a Thinker pass without a Groq key: it queues agent batches)
                    self._run_parallel()
                    self._write_pending()
                after = self._snapshot()
                if not has_pending_work(store, self.settings, due_only=True):
                    wait = _next_deferred_in(store)
                    if wait is not None and waited < MAX_DEFERRED_WAIT:
                        nap = min(max(wait, 1.0), 60.0)
                        log.info("only backed-off jobs remain; waiting %.0fs", nap)
                        self._sleep(nap)
                        waited += nap
                        continue
                    break
                if after == before:
                    idle += 1
                    if idle >= 2:
                        log.info("no progress in two passes; leaving the rest for the next run")
                        break
                    self._sleep(5.0)
                else:
                    idle = 0
        except KeyboardInterrupt:
            log.info("interrupted; finishing up")
            self.stop.set()
        finally:
            monitor_done.set()
        self._finalize(claims_offset)
        return 0

    def _safe_refresh_pool(self) -> None:
        try:
            self.pool.refresh()
        except Exception:
            log.exception("proxy refresh failed")

    # ---- end of run
    def _finalize(self, claims_offset: int) -> None:
        paths, store = self.paths, self.store
        date = datetime.now().strftime("%Y-%m-%d")
        new_claims: dict[str, list[Claim]] = collections.defaultdict(list)
        for d in iter_claim_dicts(paths, claims_offset):
            with contextlib.suppress(Exception):
                c = Claim.from_dict(d)
                new_claims[c.creator].append(c)
        changes = list(self.changes)
        self._refresh_creators()
        try:
            for slug in set(self.new_videos) | set(new_claims):
                cr = self._creators.get(slug)
                if cr is None:
                    continue
                ch = [c for c in changes if c.get("creator") in (slug, cr.name)]
                self.writer.write_update_note(cr, date, self.new_videos.get(slug, []), new_claims.get(slug, []), ch)
        except Exception:
            log.exception("writing update notes failed")
        try:
            self._relink_topics(new_claims)
        except Exception:
            log.exception("refreshing video topic links failed")
        try:
            write_overview(paths, store, self.writer, list(self._creators.values()))
        except Exception:
            log.exception("writing creator notes / Home failed")
        counts = counts_dict(store.counts())
        update_manifest(paths, last_run=now_iso(), counts=counts,
                        last_run_stats={"videos_written": self.stats["written"],
                                        "write_errors": self.stats["write_errors"],
                                        "new_claims": sum(len(v) for v in new_claims.values())})
        log.info("run finished: %s", {k: counts[k] for k in ("total", "queued", "done", "parked") if k in counts})

    def _relink_topics(self, new_claims: dict[str, list[Claim]]) -> None:
        """Re-render video notes that gained claims so their 'Topics:' line links the topic notes."""
        by_video: dict[str, list[str]] = collections.defaultdict(list)
        for claims in new_claims.values():
            for c in claims:
                if c.topic and c.topic not in by_video[c.video_id]:
                    by_video[c.video_id].append(c.topic)
        for vid, topics in list(by_video.items())[:500]:
            row = self.store.get(vid)
            cr = self._creators.get(row.creator_slug) if row else None
            if row is None or cr is None or not self.paths.transcript(vid).exists():
                continue
            meta = row.meta()
            meta.channel_id = cr.channel_id
            with self._wlock:
                self.writer.write_video_note(cr, meta, load_transcript(self.paths, vid), topics=topics)


def run_locked(paths: VaultPaths, stop: threading.Event | None = None) -> int:
    """Acquire the writer lock and run one worker pass. Exit code 0 if another worker already holds the lock."""
    paths.ensure_dirs()
    rotate_log(paths)
    setup_logging(paths)
    lock = WriterLock(paths.writer_lock)
    if not lock.acquire():
        log.info("another worker holds the lock; exiting")
        return 0
    try:
        log.info("worker started (pid %d) on %s", os.getpid(), paths.root)
        return Worker(paths, stop=stop).run()
    except Exception:
        log.exception("worker failed")
        return 1
    finally:
        lock.release()
        log.info("worker stopped")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cbrain.worker", description="Creator Brain background worker")
    ap.add_argument("--vault", default=None, help="vault folder (default: $CBRAIN_VAULT or ~/CreatorBrain)")
    args = ap.parse_args(argv)
    paths = VaultPaths(Path(args.vault).expanduser() if args.vault else default_vault())
    stop = threading.Event()

    def _handler(signum, frame):  # noqa: ANN001
        stop.set()

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                with contextlib.suppress(Exception):
                    signal.signal(sig, _handler)
    try:
        return run_locked(paths, stop)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
