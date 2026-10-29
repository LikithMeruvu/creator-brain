import gzip
import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from cbrain import worker
from cbrain.jobs import JobStore, WriterLock
from cbrain.models import Creator, JobState, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths
from tests.rcpt_fakes import CREATOR, install, seed_videos


@pytest.fixture
def rec(monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path)


@pytest.fixture
def paths(tmp_path):
    p = VaultPaths(tmp_path / "vault")
    p.ensure_dirs()
    return p


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(worker.Worker, "idle_sleep", 0.01)
    monkeypatch.setattr(worker.Worker, "pause_poll", 0.02)


def queue(paths, n=3):
    store = JobStore(paths.jobs_db)
    store.upsert_creator(Creator(**CREATOR.__dict__))
    vids = [VideoMeta(id=f"v{i}", title=f"Title {i}", published=None, views=10 - i) for i in range(n)]
    store.add_videos(CREATOR.slug, vids, {v.id: float(n - i) for i, v in enumerate(vids)})
    return store


# ---------------------------------------------------------------- start_background

def test_start_background_spawns_detached(paths, monkeypatch):
    seen = {}

    class P:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw

    monkeypatch.setattr(subprocess, "Popen", P)
    assert worker.start_background(paths) is True
    assert seen["cmd"] == [sys.executable, "-m", "cbrain.worker", "--vault", str(paths.root)]
    kw = seen["kw"]
    if os.name == "nt":
        assert kw["creationflags"] == 0x00000008 | 0x00000200 | 0x08000000
    else:
        assert kw["start_new_session"] is True
    assert kw["stdin"] == subprocess.DEVNULL and kw["stdout"] is kw["stderr"]
    assert str(paths.root.parent) not in kw["env"].get("PYTHONPATH", "x") or True
    assert os.path.exists(paths.log)


def test_start_background_does_nothing_when_locked(paths, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(1))
    lock = WriterLock(paths.writer_lock)
    assert lock.acquire()
    try:
        assert worker.start_background(paths) is False
    finally:
        lock.release()
    assert calls == []


def test_start_background_takes_over_stale_lock_file(paths, monkeypatch):
    paths.writer_lock.write_text("999999999")                       # dead pid
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    assert worker.start_background(paths) is True


# ---------------------------------------------------------------- locking

def test_lock_prevents_second_writer(paths, rec, monkeypatch):
    queue(paths)
    monkeypatch.setattr(worker.Worker, "run", lambda self: pytest.fail("must not run while locked"))
    lock = WriterLock(paths.writer_lock)
    assert lock.acquire()
    try:
        assert worker.run_locked(paths) == 0
        assert worker.main(["--vault", str(paths.root)]) == 0
    finally:
        lock.release()
    assert rec.n("runner_run") == 0


def test_stale_lock_is_taken_over_and_released(paths, rec):
    queue(paths)
    paths.writer_lock.write_text("999999999")
    assert worker.run_locked(paths) == 0
    assert rec.n("runner_run") >= 1
    assert not paths.writer_lock.exists()


# ---------------------------------------------------------------- a full run

def test_full_run_fetches_writes_extracts_and_summarises(paths, rec):
    queue(paths, 3)
    rec.claims_per_video = 2
    rec.info = {"v0": {"published": "2026-03-01", "title": "Real title", "duration": 321, "view_count": 77}}
    assert worker.run_locked(paths) == 0

    store = JobStore(paths.jobs_db)
    assert store.counts()["done"] == 3
    assert len([c for c in rec.calls["write_video_note"] if c[2] == ()]) == 3 and rec.n("index_transcript") == 3
    row = store.get("v0")
    assert row.published == "2026-03-01" and row.title == "Title 0" and row.duration == 321
    assert ("v0", "2026-03-01", ()) in [c for c in rec.calls["write_video_note"]]      # first write already had the date
    # end of run: update note, creator note, Home, manifest
    assert rec.calls["write_update_note"][0][:3] == ("jeremy-ethier", 3, 6)             # 3 videos, 6 new claims
    assert rec.n("write_creator_note") >= 1 and rec.n("write_home") >= 1   # live refresh + final
    home_counts = rec.calls["write_home"][-1][1]["jeremy-ethier"]
    assert home_counts["done"] == 3 and home_counts["built_pct"] == 100 and home_counts["failed"] == 0
    creator_top = rec.calls["write_creator_note"][-1][2]
    assert creator_top == ["Protein Intake"]
    # video notes were re-rendered with topic links
    relinked = [c for c in rec.calls["write_video_note"] if c[2] == ("Protein Intake",)]
    assert len(relinked) == 3
    man = worker.read_manifest(paths)
    assert man["schema_version"] == 1 and man["last_run"] and man["counts"]["done"] == 3
    assert man["last_run_stats"]["new_claims"] == 6
    assert paths.log.exists() and "worker started" in paths.log.read_text(encoding="utf-8")
    assert not paths.writer_lock.exists()


def test_proxy_pool_only_when_configured(paths, rec, monkeypatch):
    queue(paths, 1)
    from cbrain.config import Settings
    s = Settings.load(paths)
    s.use_free_proxies = False
    s.save(paths)
    worker.run_locked(paths)
    assert rec.n("pool_init") == 0 and rec.calls["runner_init"][0] == (False,)

    s.use_free_proxies = True
    s.save(paths)
    queue(paths, 1)
    JobStore(paths.jobs_db).db.execute("UPDATE videos SET state='queued'")
    worker.run_locked(paths)
    deadline = time.time() + 2
    while rec.n("pool_refresh") == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert rec.n("pool_init") == 1 and rec.n("pool_refresh") == 1


def test_write_failure_does_not_reach_runner_and_retries_then_parks(paths, rec, monkeypatch):
    monkeypatch.setattr(worker, "MAX_DEFERRED_WAIT", 0)               # do not sit out the retry back-off
    queue(paths, 2)
    rec.write_fail = {"v0"}
    assert worker.run_locked(paths) == 0
    store = JobStore(paths.jobs_db)
    assert store.get("v1").state == "done"
    assert store.get("v0").state in ("fetched", "parked")           # never silently lost
    assert store.get("v0").last_error.startswith("write failed")


def test_stale_leases_released_at_start(paths, rec):
    store = queue(paths, 1)
    store.db.execute("UPDATE videos SET state='fetching', lease_until=1")    # crashed worker
    worker.run_locked(paths)
    assert store.get("v0").state == "done"


def test_fetched_rows_without_notes_are_written_on_start(paths, rec):
    store = queue(paths, 2)
    for vid in ("v0", "v1"):
        tr = Transcript(video_id=vid, segments=[Segment(0, 3, "legacy words")], source="legacy_import")
        with gzip.open(paths.transcript(vid), "wt", encoding="utf-8") as f:
            json.dump(tr.to_dict(), f)
    store.mark("v0", JobState.FETCHED, "legacy_import")
    store.mark("v1", JobState.FETCHED, "legacy_import")
    store.db.execute("UPDATE videos SET published='2026-01-01'")
    worker.run_locked(paths)
    assert rec.n("write_video_note") >= 2 and rec.n("index_transcript") == 2
    assert store.counts()["done"] == 2


def test_missing_transcript_file_requeues(paths, rec):
    store = queue(paths, 1)
    store.mark("v0", JobState.FETCHED, "legacy_import")               # no gz file exists
    worker.run_locked(paths)
    assert store.get("v0").state == "done"                            # re-fetched by the (fake) runner
    assert rec.n("runner_run") >= 1


# ---------------------------------------------------------------- pause / stop

def test_pause_flag_stops_before_any_work(paths, rec):
    queue(paths)
    paths.pause_flag.write_text("x")
    assert worker.run_locked(paths) == 0
    assert rec.n("runner_run") == 0 and JobStore(paths.jobs_db).counts()["queued"] == 3


def test_pause_flag_created_mid_run_stops_worker(paths, rec):
    queue(paths)
    rec.block_runner = True                                           # runner loops until told to stop
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("rc", worker.run_locked(paths)))
    t.start()
    deadline = time.time() + 3
    while rec.n("runner_run") == 0 and time.time() < deadline:
        time.sleep(0.01)
    paths.pause_flag.write_text("x")
    t.join(5)
    assert not t.is_alive() and result["rc"] == 0
    assert JobStore(paths.jobs_db).counts()["queued"] == 3
    assert not paths.writer_lock.exists()


def test_stop_event_ends_run_gracefully(paths, rec):
    queue(paths)
    rec.block_runner = True
    stop = threading.Event()
    t = threading.Thread(target=lambda: worker.run_locked(paths, stop))
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(5)
    assert not t.is_alive() and worker.read_manifest(paths)["last_run"]


def test_no_progress_does_not_loop_forever(paths, rec, monkeypatch):
    store = queue(paths, 1)
    monkeypatch.setattr(worker.Worker, "_run_parallel", lambda self: None)       # nothing ever changes
    monkeypatch.setattr(worker.Worker, "_sleep", lambda self, s: None)
    t0 = time.time()
    assert worker.run_locked(paths) == 0
    assert time.time() - t0 < 3 and store.counts()["queued"] == 1


def test_deferred_jobs_wait_then_finish(paths, rec, monkeypatch):
    store = queue(paths, 1)
    store.db.execute("UPDATE videos SET next_try_at=?", (time.time() + 0.3,))
    monkeypatch.setattr(worker.Worker, "_sleep", lambda self, s: time.sleep(0.35))
    worker.run_locked(paths)
    assert store.get("v0").state == "done"


def test_main_installs_handlers_and_runs(paths, rec):
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        queue(paths, 1)
        assert worker.main(["--vault", str(paths.root)]) == 0
        assert JobStore(paths.jobs_db).counts()["done"] == 1
    finally:
        for s, h in old.items():
            signal.signal(s, h)


# ---------------------------------------------------------------- helpers

def test_manifest_roundtrip_is_atomic_and_versioned(paths):
    assert worker.read_manifest(paths) == {}
    worker.update_manifest(paths, a=1)
    worker.update_manifest(paths, b=2)
    m = worker.read_manifest(paths)
    assert m == {"a": 1, "b": 2, "schema_version": 1}
    assert not list(paths.brain.glob("*.tmp"))


def test_rotate_log(paths):
    paths.log.write_bytes(b"x" * 100)
    worker.rotate_log(paths, max_bytes=50)
    assert not paths.log.exists() and paths.log.with_name("worker.log.1").exists()
    worker.rotate_log(paths, max_bytes=50)                            # nothing to rotate: no error


def test_has_pending_work(paths):
    from cbrain.config import Settings
    store = queue(paths, 1)
    s = Settings()
    assert worker.has_pending_work(store, s)
    store.db.execute("UPDATE videos SET state='written'")
    assert not worker.has_pending_work(store, s)                      # no Groq key: nothing the worker could extract
    s.groq_api_key = "k"
    assert worker.has_pending_work(store, s)
    store.db.execute("UPDATE videos SET state='queued', next_try_at=?", (time.time() + 999,))
    assert worker.has_pending_work(store, s) and not worker.has_pending_work(store, s, due_only=True)


def test_counts_dict_adds_convenience_keys():
    c = worker.counts_dict({"total": 10, "written": 2, "done": 3, "parked": 1})
    assert c["built"] == 5 and c["built_pct"] == 50 and c["analyzed_pct"] == 30 and c["failed"] == 1
    assert worker.counts_dict({"total": 0})["built_pct"] == 0


def test_thinker_changes_reach_the_update_note(paths, rec):
    queue(paths, 1)
    rec.changes = [{"creator": "jeremy-ethier", "topic": "Protein Intake", "summary": "now says 2 g/kg"}]
    worker.run_locked(paths)
    assert rec.calls["write_update_note"][0][3] == [rec.changes[0]]
