"""Durable job queue (SQLite, WAL). Every video is one row; a crash never loses work."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Creator, JobState, VideoMeta

SCHEMA = """
CREATE TABLE IF NOT EXISTS creators (
  slug TEXT PRIMARY KEY, name TEXT NOT NULL, handle TEXT NOT NULL, channel_id TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'recent', added_at TEXT NOT NULL DEFAULT '', last_checked_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY, creator_slug TEXT NOT NULL REFERENCES creators(slug),
  title TEXT NOT NULL, published TEXT, duration INTEGER, views INTEGER,
  is_short INTEGER NOT NULL DEFAULT 0, is_live INTEGER NOT NULL DEFAULT 0,
  selected INTEGER NOT NULL DEFAULT 1,           -- 0 = known but outside the chosen scope
  priority REAL NOT NULL DEFAULT 0,              -- higher runs first
  state TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  next_try_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
  transcript_source TEXT, added_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_pick ON videos(state, selected, next_try_at, priority);
CREATE INDEX IF NOT EXISTS videos_creator ON videos(creator_slug, state);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class VideoRow:
    id: str
    creator_slug: str
    title: str
    published: str | None
    duration: int | None
    views: int | None
    state: str
    attempts: int
    last_error: str | None
    transcript_source: str | None
    priority: float
    selected: bool = True

    def meta(self) -> VideoMeta:
        return VideoMeta(id=self.id, title=self.title, published=self.published,
                         duration=self.duration, views=self.views)


class _LockedConnection:
    """Serialises every statement on the shared connection; tx() holds the same re-entrant lock."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn, self._lock = conn, lock

    def execute(self, *args):
        with self._lock:
            return self._conn.execute(*args)

    def executescript(self, script: str):
        with self._lock:
            return self._conn.executescript(script)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class JobStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db = _LockedConnection(conn, self.lock)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)

    # ---- creators -------------------------------------------------------
    def upsert_creator(self, c: Creator) -> None:
        self.db.execute(
            """INSERT INTO creators(slug,name,handle,channel_id,scope,added_at,last_checked_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET name=excluded.name, handle=excluded.handle,
                 channel_id=excluded.channel_id, scope=excluded.scope""",
            (c.slug, c.name, c.handle, c.channel_id, c.scope, c.added_at or now_iso(), c.last_checked_at),
        )

    def get_creator(self, key: str) -> Creator | None:
        """Look up by slug, handle (with/without @), channel id or display name."""
        k = key.strip()
        row = self.db.execute(
            """SELECT * FROM creators WHERE slug=? OR lower(handle)=lower(?) OR lower(handle)=lower(?)
               OR channel_id=? OR lower(name)=lower(?)""",
            (k, k, "@" + k.lstrip("@"), k, k),
        ).fetchone()
        return Creator(**dict(row)) if row else None

    def list_creators(self) -> list[Creator]:
        return [Creator(**dict(r)) for r in self.db.execute("SELECT * FROM creators ORDER BY name")]

    def touch_checked(self, slug: str) -> None:
        self.db.execute("UPDATE creators SET last_checked_at=? WHERE slug=?", (now_iso(), slug))

    # ---- videos ---------------------------------------------------------
    def add_videos(self, creator_slug: str, videos: list[VideoMeta], priorities: dict[str, float]) -> list[str]:
        """Insert unknown videos. Ids in `priorities` are selected; others are recorded but not queued.
        Already-known videos that become selected (e.g. --full) are switched on. Returns newly queued ids."""
        ts, queued = now_iso(), []
        with self.tx():
            for v in videos:
                sel = v.id in priorities
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO videos(id,creator_slug,title,published,duration,views,is_short,is_live,
                       selected,priority,state,added_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (v.id, creator_slug, v.title, v.published, v.duration, v.views, int(v.is_short), int(v.is_live),
                     int(sel), priorities.get(v.id, 0.0), JobState.QUEUED.value, ts, ts),
                )
                if cur.rowcount:
                    if sel:
                        queued.append(v.id)
                    continue
                # known video: refresh stats; switch on if newly selected
                self.db.execute("UPDATE videos SET views=COALESCE(?,views), title=? WHERE id=?", (v.views, v.title, v.id))
                if sel:
                    cur = self.db.execute(
                        "UPDATE videos SET selected=1, priority=? WHERE id=? AND selected=0", (priorities[v.id], v.id))
                    if cur.rowcount:
                        queued.append(v.id)
        return queued

    def claim(self, states: tuple[str, ...] = (JobState.QUEUED.value,), limit: int = 1,
              lease_seconds: int = 600, to_state: str = JobState.FETCHING.value,
              ignore_backoff: bool = False, creator_slug: str | None = None) -> list[VideoRow]:
        """Atomically lease the highest-priority due jobs (ignore_backoff: also jobs resting after a quota error)."""
        t = time.time() if not ignore_backoff else float("inf")
        marks = ",".join("?" * len(states))
        with self.tx():
            rows = self.db.execute(
                f"""SELECT * FROM videos WHERE state IN ({marks}) AND selected=1 AND next_try_at<=?
                    AND (? IS NULL OR creator_slug=?) ORDER BY priority DESC, published DESC LIMIT ?""",
                (*states, t, creator_slug, creator_slug, limit),
            ).fetchall()
            for r in rows:
                self.db.execute("UPDATE videos SET state=?, lease_until=?, updated_at=? WHERE id=?",
                                (to_state, time.time() + lease_seconds, now_iso(), r["id"]))
        return [self._row(r, state=to_state) for r in rows]

    def mark(self, video_id: str, state: JobState | str, transcript_source: str | None = None) -> None:
        state = state.value if isinstance(state, JobState) else state
        self.db.execute(
            """UPDATE videos SET state=?, lease_until=0, last_error=NULL,
               transcript_source=COALESCE(?, transcript_source), updated_at=? WHERE id=?""",
            (state, transcript_source, now_iso(), video_id))

    def fail(self, video_id: str, error: str, back_to: JobState | str = JobState.QUEUED,
             max_attempts: int = 3, retry: bool = True) -> str:
        """Record a failure; back off exponentially, park after max_attempts. Returns new state."""
        back_to = back_to.value if isinstance(back_to, JobState) else back_to
        with self.tx():
            row = self.db.execute("SELECT attempts FROM videos WHERE id=?", (video_id,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            parked = (not retry) or attempts >= max_attempts
            state = JobState.PARKED.value if parked else back_to
            self._fail_update(video_id, state, attempts, error)
        return state

    def _fail_update(self, video_id: str, state: str, attempts: int, error: str) -> None:
        self.db.execute(
            """UPDATE videos SET state=?, attempts=?, last_error=?, lease_until=0, next_try_at=?, updated_at=?
               WHERE id=?""",
            (state, attempts, error[:500], time.time() + 60 * 2 ** attempts, now_iso(), video_id))

    def select_more(self, creator_slug: str, ids: list[str], priority: float = 500.0) -> list[str]:
        """Bring catalog videos into the brain. Returns the ids that were newly selected."""
        added = []
        with self.tx():
            for vid in ids:
                cur = self.db.execute(
                    "UPDATE videos SET selected=1, priority=?, next_try_at=0 WHERE id=? AND creator_slug=? AND selected=0",
                    (priority, vid, creator_slug))
                if cur.rowcount:
                    added.append(vid)
        return added

    def include_downloaded(self, creator_slug: str) -> int:
        """Select every video whose transcript is already downloaded, even outside the chosen scope."""
        return self.db.execute(
            "UPDATE videos SET selected=1 WHERE creator_slug=? AND selected=0 AND state NOT IN ('queued','fetching')",
            (creator_slug,)).rowcount

    def update_meta(self, video_id: str, published: str | None = None, views: int | None = None,
                    duration: int | None = None, title: str | None = None) -> None:
        """Fill details that flat listing lacks (publish date, exact views) once the full page is fetched."""
        self.db.execute(
            """UPDATE videos SET published=COALESCE(?,published), views=COALESCE(?,views),
               duration=COALESCE(?,duration), title=COALESCE(?,title), updated_at=? WHERE id=?""",
            (published, views, duration, title, now_iso(), video_id))

    def defer(self, video_id: str, seconds: float, back_to: JobState | str = JobState.QUEUED) -> None:
        """Put a job back without counting an attempt (e.g. all lanes blocked)."""
        back_to = back_to.value if isinstance(back_to, JobState) else back_to
        self.db.execute("UPDATE videos SET state=?, lease_until=0, next_try_at=? WHERE id=?",
                        (back_to, time.time() + seconds, video_id))

    def unpark(self, creator_slug: str | None = None) -> int:
        q = "UPDATE videos SET state='queued', attempts=0, next_try_at=0 WHERE state='parked'"
        args: tuple = ()
        if creator_slug:
            q, args = q + " AND creator_slug=?", (creator_slug,)
        return self.db.execute(q, args).rowcount

    def release_stale_leases(self) -> int:
        """Jobs leased by a crashed worker go back to the state before their lease."""
        t = time.time()
        n = self.db.execute("UPDATE videos SET state='queued', lease_until=0 WHERE state='fetching' AND lease_until<?", (t,)).rowcount
        n += self.db.execute("UPDATE videos SET state='written', lease_until=0 WHERE state='extracting' AND lease_until<?", (t,)).rowcount
        return n

    def get(self, video_id: str) -> VideoRow | None:
        r = self.db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
        return self._row(r) if r else None

    def videos(self, creator_slug: str | None = None, state: str | None = None, selected_only: bool = True) -> list[VideoRow]:
        q, args = "SELECT * FROM videos WHERE 1=1", []
        if creator_slug:
            q += " AND creator_slug=?"; args.append(creator_slug)
        if state:
            q += " AND state=?"; args.append(state)
        if selected_only:
            q += " AND selected=1"
        return [self._row(r) for r in self.db.execute(q + " ORDER BY published DESC", args)]

    def counts(self, creator_slug: str | None = None) -> dict[str, int]:
        q, args = "SELECT state, COUNT(*) n FROM videos WHERE selected=1", []
        if creator_slug:
            q += " AND creator_slug=?"; args.append(creator_slug)
        out = {s.value: 0 for s in JobState}
        for r in self.db.execute(q + " GROUP BY state", args):
            out[r["state"]] = r["n"]
        out["total"] = sum(v for k, v in out.items() if k != "total")
        return out

    @contextlib.contextmanager
    def tx(self) -> Iterator[None]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(r: sqlite3.Row, state: str | None = None) -> VideoRow:
        return VideoRow(id=r["id"], creator_slug=r["creator_slug"], title=r["title"], published=r["published"],
                        duration=r["duration"], views=r["views"], state=state or r["state"], attempts=r["attempts"],
                        last_error=r["last_error"], transcript_source=r["transcript_source"], priority=r["priority"],
                        selected=bool(r["selected"]))


class WriterLock:
    """One writer per vault (many harnesses may share it). Stale locks from dead processes are taken over."""

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                if _pid_alive(self._owner()):
                    return False
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
        return False

    def release(self) -> None:
        if self.held:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            self.held = False

    def is_locked(self) -> bool:
        return self.path.exists() and _pid_alive(self._owner())

    def _owner(self) -> int:
        try:
            return int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def __enter__(self) -> "WriterLock":
        if not self.acquire():
            raise RuntimeError("another Creator Brain worker is writing to this vault")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
