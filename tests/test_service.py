import json
import threading
import time

import pytest

from cbrain import worker
from cbrain.jobs import WriterLock
from cbrain.models import VideoMeta
from cbrain.service import Brain, parse_scalar, read_frontmatter, update_frontmatter
from tests.rcpt_fakes import install, make_videos, seed_videos


@pytest.fixture
def rec(monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path)


@pytest.fixture
def brain(rec, tmp_path, monkeypatch):
    b = Brain(tmp_path / "vault")
    started = []
    monkeypatch.setattr(worker, "start_background", lambda paths: started.append(1) or True)
    b.started = started
    return b


# ---------------------------------------------------------------- add / update

def test_add_creator_is_fast_and_queues(brain, rec):
    t0 = time.time()
    r = brain.add_creator("@JeremyEthier")
    assert time.time() - t0 < 2
    assert "error" not in r, r
    assert r["queued"] == 3 and r["total_on_channel"] == 10          # recent 30% of 10
    assert r["brain"].startswith("jeremy-ethier · recent · updated")
    assert brain.store.counts("jeremy-ethier")["queued"] == 3
    assert rec.n("list_channel") == 1
    assert len(brain.started) == 1                                   # worker spawned, not awaited
    assert rec.n("write_home") == 1 and rec.n("write_creator_note") == 1
    assert "background" in r["message"]


def test_add_full_queues_everything_and_is_sticky(brain):
    brain.add_creator("@JeremyEthier", full=True)
    assert brain.store.counts("jeremy-ethier")["queued"] == 10
    r = brain.add_creator("@JeremyEthier")                           # re-adding without --full keeps full scope
    assert r["scope"] == "full"


def test_add_uses_legacy_import_when_old_db_exists(brain, rec, tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    old.write_bytes(b"x")
    monkeypatch.setenv("CBRAIN_LEGACY_DB", str(old))
    r = brain.add_creator("@JeremyEthier")
    assert r["imported"] == 7 and rec.n("legacy_import") == 1
    assert "Reused 7" in r["message"]


def test_add_error_becomes_dict(brain, rec):
    rec.list_error = RuntimeError("boom")
    r = brain.add_creator("@Nope")
    assert r["error"].startswith("RuntimeError") and r["hint"]
    assert "queued" not in r
    assert brain.add_creator("  ")["hint"]


def test_update_queues_only_new_videos(brain, rec):
    brain.add_creator("@JeremyEthier")
    new = VideoMeta(id="brandnew", title="Fresh upload", published="2026-10-28", views=5)
    rec.latest = [new, VideoMeta(id="vid00", title="Video 0")]
    r = brain.update("jeremy-ethier")
    assert r["new_videos"] == 1 and r["updated"][0]["titles"] == ["Fresh upload"]
    assert brain.store.get("brandnew").state == "queued"
    assert brain.store.get_creator("jeremy-ethier").last_checked_at
    assert rec.n("list_channel") == 1                                # scope=recent -> no full re-list
    assert worker.read_manifest(brain.paths)["last_daily_check"]


def test_update_full_scope_with_gap_relists(brain, rec):
    brain.add_creator("@JeremyEthier", full=True)
    rec.latest = [VideoMeta(id=f"gap{i}", title=f"Gap {i}") for i in range(15)]   # none known -> gap > 15
    rec.videos = make_videos(10) + rec.latest
    brain.update()
    assert rec.n("list_channel") == 2


def test_update_survives_one_bad_creator(brain, rec, monkeypatch):
    brain.add_creator("@JeremyEthier")
    monkeypatch.setitem(__import__("sys").modules["cbrain.discovery"].__dict__, "latest",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net down")))
    r = brain.update()
    assert "error" not in r and r["updated"][0]["error"].startswith("RuntimeError")
    assert "Could not check" in r["message"]


# ---------------------------------------------------------------- daily check

def test_daily_check_runs_once_per_window(brain, monkeypatch):
    brain.add_creator("@JeremyEthier")
    calls = []
    monkeypatch.setattr(Brain, "update", lambda self, creator=None: calls.append(1) or {"message": "ok"})
    first = brain.maybe_daily_check()
    assert first and first["started"]
    brain._daily_thread.join(5)
    assert len(calls) == 1
    assert brain.maybe_daily_check() is None                          # still inside the 24h window
    brain._daily_thread.join(5)
    assert len(calls) == 1
    # window expired -> runs again
    worker.update_manifest(brain.paths, last_daily_check="2000-01-01T00:00:00+00:00")
    assert brain.maybe_daily_check()
    brain._daily_thread.join(5)
    assert len(calls) == 2


def test_daily_check_returns_immediately_and_skips_empty_brain(brain, monkeypatch):
    assert brain.maybe_daily_check() is None                          # no creators yet
    brain.add_creator("@JeremyEthier")
    gate = threading.Event()
    monkeypatch.setattr(Brain, "update", lambda self, creator=None: gate.wait(5) and {"message": "ok"})
    t0 = time.time()
    assert brain.maybe_daily_check()
    assert time.time() - t0 < 1
    gate.set()
    brain._daily_thread.join(5)


# ---------------------------------------------------------------- status

def test_status_math_and_eta(brain, rec):
    seed_videos(brain.store, queued=30, fetching=0, fetched=5, written=10, done=15, parked=4)
    brain.store.db.execute("UPDATE videos SET last_error='no captions available for this video' WHERE state='parked'")
    s = brain.status()
    t = s["totals"]
    assert t["total"] == 64 and t["built"] == 25 and s["percent_built"] == 39      # (10+15)/64
    assert s["percent_analyzed"] == 23
    assert s["pending_jobs"] == 45 and s["worker_running"] is False and s["paused"] is False
    assert s["eta"]["seconds"] == 30 * 15 and s["eta"]["estimate"] is True          # 1 lane, no proxies
    assert "estimate" in s["eta"]["text"]
    assert s["parked_reasons"] == [{"reason": "no captions available for this video", "count": 4}]
    assert s["creators"][0]["percent_built"] == 39 and "jeremy-ethier" in s["brain"]
    assert "39% searchable" in s["message"]


def test_status_eta_uses_lanes_when_proxies_healthy(brain, rec):
    seed_videos(brain.store, queued=120)
    rec.add("healthy", 20)
    s = brain.status("jeremy-ethier")
    assert s["eta"]["lanes_assumed"] == brain.settings.lanes
    assert s["eta"]["seconds"] == round(120 * 15 / brain.settings.lanes)
    assert s["proxy"]["healthy"] == 20


def test_status_worker_running_and_paused(brain):
    seed_videos(brain.store, queued=1)
    lock = WriterLock(brain.paths.writer_lock)
    assert lock.acquire()
    try:
        assert brain.status()["worker_running"] is True
    finally:
        lock.release()
    brain.pause()
    s = brain.status()
    assert s["paused"] is True and "Paused" in s["message"]


def test_status_unknown_creator(brain):
    r = brain.status("nobody")
    assert "nobody" in r["error"] and r["hint"]


def test_status_empty_brain(brain):
    assert "empty" in brain.status()["message"].lower()


# ---------------------------------------------------------------- search / topic / compare

def test_search_returns_snippets_and_completeness(brain, rec):
    seed_videos(brain.store, written=3, queued=1)
    rec.hits = [{"creator": "jeremy-ethier", "video_id": "abc", "title": "Protein 101", "published": "2026-01-01",
                 "t": 125.0, "url": "", "text": "x " * 500, "score": 3.14159, "kind": "chunks"}]
    r = brain.search("protein", limit=5)
    hit = r["results"][0]
    assert hit["timestamp"] == "02:05" and hit["url"] == "https://youtu.be/abc?t=125"
    assert len(hit["snippet"]) <= 350
    comp = r["brain_completeness"]["jeremy-ethier"]
    assert comp["percent_built"] == 75 and comp["still_processing"] == 4
    assert "75%" in r["message"] and r["cite"]
    assert rec.calls["search"][0][1] == ()                            # no creator filter passed when none requested


def test_search_filters_and_resolves_creator(brain, rec):
    seed_videos(brain.store, written=1)
    brain.search("x", creators=["@JeremyEthier"])
    assert rec.calls["search"][0][1] == ("jeremy-ethier",)
    assert "error" in brain.search("x", creators=["ghost"])
    assert "error" in brain.search("   ")


def test_search_empty_brain(brain):
    assert brain.search("x")["results"] == []


def _topics(brain):
    brain.paths.topics_json.write_text(json.dumps(
        {"Protein Intake": {"aliases": ["protein requirements"], "claims": []}}), encoding="utf-8")
    lines = [
        {"creator": "jeremy-ethier", "video_id": "a", "t": 10, "claim": "Eat 1.6 g/kg", "quote": "q q q q q q",
         "topic": "Protein Intake", "date": "2025-01-01", "verified": True},
        {"creator": "jeremy-ethier", "video_id": "b", "t": 20, "claim": "Newer: 2 g/kg", "quote": "q q q q q q",
         "topic": "Protein Intake", "date": "2026-01-01", "verified": True},
        {"creator": "other", "video_id": "c", "t": 5, "claim": "unrelated", "quote": "z", "topic": "Sleep"},
    ]
    brain.paths.claims.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")


def test_topic_matches_alias_plural_and_sorts_newest_first(brain):
    _topics(brain)
    for name in ("protein intake", "Protein requirement", "protein intakes"):
        r = brain.topic(name)
        assert r["found"] and r["topic"] == "Protein Intake", name
        assert [c["claim"] for c in r["claims"]] == ["Newer: 2 g/kg", "Eat 1.6 g/kg"]
        assert r["claims"][0]["url"] == "https://youtu.be/b?t=20" and r["claims"][0]["timestamp"] == "00:20"


def test_topic_unknown_is_not_an_error(brain, rec):
    seed_videos(brain.store, written=1)
    r = brain.topic("underwater basket weaving")
    assert r["found"] is False and "error" not in r and r["claims"] == []


def test_compare(brain, rec):
    seed_videos(brain.store, written=1)
    _topics(brain)
    r = brain.compare("protein intake", ["jeremy-ethier"])
    assert r["topic"] == "Protein Intake" and r["by_creator"]["jeremy-ethier"]["claim_count"] == 2
    assert "error" in brain.compare("x", ["ghost"])


# ---------------------------------------------------------------- profile / outputs

ME = "---\ngoal: lose fat\nlevel:\nequipment:\n  - barbell\n  - bench\ncustom_key: keep me\n---\n# Me\n\nBody line 1\n\n## Notes\n- a: b\n"


def test_profile_round_trip_keeps_body_and_other_keys(brain):
    brain.paths.me.write_text(ME, encoding="utf-8")
    r = brain.profile({"goal": "build muscle", "days": "4", "injuries": ["left knee", "shoulder"], "age": "31", "bogus": 1})
    assert r["profile"]["goal"] == "build muscle" and r["profile"]["days_per_week"] == 4
    assert r["profile"]["age"] == 31 and r["profile"]["injuries"] == ["left knee", "shoulder"]
    assert r["profile"]["equipment"] == ["barbell", "bench"]
    assert "bogus" in r["message"] and "Still unknown" in r["message"]
    text = brain.paths.me.read_text(encoding="utf-8")
    assert text.endswith("---\n# Me\n\nBody line 1\n\n## Notes\n- a: b\n")
    assert "custom_key: keep me" in text and "goal: build muscle" in text
    again = brain.profile()
    assert again["profile"] == r["profile"] and again["updated"] == []


def test_profile_creates_me_and_preserves_crlf(brain, tmp_path):
    brain.paths.me.unlink()
    r = brain.profile({"goal": "run a 10k: fast"})
    assert r["profile"]["goal"] == "run a 10k: fast"
    p = tmp_path / "crlf.md"
    p.write_bytes(b"---\r\ngoal: x\r\n---\r\nbody\r\n")
    update_frontmatter(p, {"goal": "y"})
    assert p.read_bytes() == b"---\r\ngoal: y\r\n---\r\nbody\r\n"


def test_frontmatter_without_block_gets_one(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("just a body\n", encoding="utf-8")
    update_frontmatter(p, {"goal": "g"})
    assert p.read_text(encoding="utf-8") == "---\ngoal: g\n---\njust a body\n"
    assert read_frontmatter(p) == {"goal": "g"}


def test_parse_scalar():
    assert parse_scalar('"a: b"') == "a: b" and parse_scalar("[a, 'b c', 3]") == ["a", "b c", 3]
    assert parse_scalar("12") == 12 and parse_scalar("1.5") == 1.5 and parse_scalar("") == ""


def test_save_output(brain, rec):
    r = brain.save_output("Leg Day Plan", "# Plan\n- squat", ["https://youtu.be/a?t=1"])
    assert r["vault_path"] == "outputs/2026-10-29 Leg Day Plan.md" and (brain.paths.root / r["vault_path"]).exists()
    assert r["link"] == "[[2026-10-29 Leg Day Plan]]"
    assert "error" in brain.save_output("", "x")


# ---------------------------------------------------------------- suggestions / control

def test_suggest_creators_groups_ranks_and_excludes(brain, monkeypatch):
    seed_videos(brain.store, queued=1)                                  # jeremy-ethier / UC1 already added
    entries = [
        {"channel_id": "UC1", "channel": "Jeremy Ethier", "view_count": 999999, "title": "mine"},
        {"channel_id": "UC2", "channel": "Small", "uploader_id": "@small", "view_count": 100, "title": "s1"},
        {"channel_id": "UC3", "channel": "Big", "uploader_id": "@big", "view_count": 5000, "title": "b1", "channel_url": "https://youtube.com/channel/UC3"},
        {"channel_id": "UC3", "channel": "Big", "view_count": 7000, "title": "b2"},
        {"channel_id": "UC2", "channel": "Small", "view_count": 50, "title": "s2"},
    ]
    monkeypatch.setattr(Brain, "_ytsearch", lambda self, q, n=30: entries)
    r = brain.suggest_creators("training", n=5)
    names = [s["name"] for s in r["suggestions"]]
    assert names == ["Big", "Small"]
    assert r["suggestions"][0]["total_views"] == 12000 and r["suggestions"][0]["add_with"] == "@big"
    assert brain.suggest_creators("training", n=1)["suggestions"][0]["name"] == "Big"
    assert len(brain.suggest_creators("training", n=1)["suggestions"]) == 1


def test_suggest_creators_network_error(brain, monkeypatch):
    def boom(self, q, n=30):
        raise TimeoutError("slow")
    monkeypatch.setattr(Brain, "_ytsearch", boom)
    r = brain.suggest_creators("x")
    assert "error" in r and "Network" in r["hint"]


def test_pause_resume_retry(brain):
    seed_videos(brain.store, queued=1, parked=2)
    assert brain.pause()["paused"] and brain.paths.pause_flag.exists()
    r = brain.resume()
    assert not brain.paths.pause_flag.exists() and r["worker_started"] is True
    r = brain.retry_parked("jeremy-ethier")
    assert r["retried"] == 2 and brain.store.counts()["parked"] == 0
    assert brain.retry_parked()["retried"] == 0


def test_ensure_worker_not_started_when_paused_or_no_work(brain):
    assert brain._ensure_worker(force=True) is False                    # nothing queued
    seed_videos(brain.store, queued=1)
    brain.pause()
    assert brain._ensure_worker(force=True) is False
    brain.paths.pause_flag.unlink()
    assert brain._ensure_worker(force=True) is True
    assert brain._ensure_worker() is False                              # throttled for 30s


def test_written_rows_do_not_respawn_worker_without_llm(brain):
    seed_videos(brain.store, written=3)
    assert brain._ensure_worker(force=True) is False


def test_doctor_wrapper(brain):
    r = brain.doctor()
    assert "issues" in r and "ok" in r


def test_add_videos_from_catalog_by_match_and_top(tmp_path):
    """Videos outside the default scope stay in the catalog until the user asks for them by title or views."""
    from cbrain.jobs import JobStore
    from cbrain.models import Creator, VideoMeta
    store = JobStore(tmp_path / "j.sqlite")
    store.upsert_creator(Creator("jeff", "Jeff Nippard", "@JeffNippard", "UC1"))
    vids = [VideoMeta("a", "Best Protein Sources", views=10), VideoMeta("b", "Full Body Workout", views=99),
            VideoMeta("c", "Protein Timing Myths", views=5), VideoMeta("d", "Leg Day", views=1)]
    store.add_videos("jeff", vids, {"d": 1.0})
    pool = {r.id for r in store.videos("jeff", selected_only=False) if not r.selected}
    assert pool == {"a", "b", "c"}
    assert store.select_more("jeff", ["a", "c"]) == ["a", "c"]
    assert store.select_more("jeff", ["a"]) == []          # already in the brain
    assert {r.id for r in store.videos("jeff")} == {"a", "c", "d"}
