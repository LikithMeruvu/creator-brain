import gzip
import json
import sys

import pytest

from cbrain import doctor, worker
from cbrain.jobs import JobStore, WriterLock
from cbrain.models import Creator, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths
from tests.rcpt_fakes import CREATOR, install


@pytest.fixture
def rec(monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path)


@pytest.fixture
def paths(tmp_path):
    p = VaultPaths(tmp_path / "vault")
    p.ensure_dirs()
    return p


@pytest.fixture
def store(paths):
    s = JobStore(paths.jobs_db)
    s.upsert_creator(Creator(**CREATOR.__dict__))
    s.touch_checked(CREATOR.slug)
    return s


def checks(report):
    return {i["check"]: i for i in report["issues"]}


def add_row(store, paths, vid, state, with_transcript=True, title="T"):
    store.add_videos(CREATOR.slug, [VideoMeta(id=vid, title=title, published="2026-01-02")], {vid: 1.0})
    store.db.execute("UPDATE videos SET state=? WHERE id=?", (state, vid))
    if with_transcript:
        tr = Transcript(video_id=vid, segments=[Segment(0, 2, "words")], source="auto_caption")
        with gzip.open(paths.transcript(vid), "wt", encoding="utf-8") as f:
            json.dump(tr.to_dict(), f)


def test_healthy_vault(paths, store, rec):
    worker.update_manifest(paths)
    r = doctor.check(paths, store, None)
    assert r["ok"] and r["issues"] == [] and "All good" in r["message"]


def test_broken_links_and_orphans(paths, store, rec):
    worker.update_manifest(paths)
    (paths.root / "Home.md").write_text("[[Existing]] [[Missing Note|alias]] [[Aliased Name]] [[Existing#Heading]]",
                                        encoding="utf-8")
    (paths.topics / "Existing.md").write_text("---\naliases: [Aliased Name]\n---\ntext", encoding="utf-8")
    (paths.topics / "Lonely.md").write_text("no one links here", encoding="utf-8")
    r = checks(doctor.check(paths, store, None))
    assert r["broken_links"]["count"] == 1 and "Missing Note" in r["broken_links"]["detail"][0]
    assert r["orphan_notes"]["detail"] == ["Lonely"]


def test_missing_note_and_transcript_with_fix(paths, store, rec):
    worker.update_manifest(paths)
    add_row(store, paths, "a1", "written")                         # transcript, no note
    add_row(store, paths, "b2", "done", with_transcript=False)     # row says done but transcript is gone
    r = doctor.check(paths, store, None)
    c = checks(r)
    assert c["video_notes_missing"]["count"] == 1 and c["transcripts_missing"]["count"] == 1
    assert not r["ok"]
    index = sys.modules["cbrain.vault.index"].SearchIndex(paths.search_db)
    r = doctor.check(paths, store, index, fix=True)
    assert r["fixed"] == 2
    assert store.get("b2").state == "queued"
    assert rec.n("write_video_note") == 1 and rec.n("index_transcript") == 1
    assert "video_notes_missing" not in checks(doctor.check(paths, store, None))


def test_parked_with_reasons_and_unpark(paths, store, rec):
    worker.update_manifest(paths)
    add_row(store, paths, "p1", "parked", with_transcript=False)
    add_row(store, paths, "p2", "parked", with_transcript=False)
    store.db.execute("UPDATE videos SET last_error='members only'")
    p = checks(doctor.check(paths, store, None))["parked_videos"]
    assert p["count"] == 2 and p["detail"] == ["2 x members only"]
    r = doctor.check(paths, store, None, fix=True)
    assert store.counts()["parked"] == 0 and store.counts()["queued"] == 2 and r["fixed"] == 2


def test_unverified_claims_and_duplicate_topics(paths, store, rec):
    worker.update_manifest(paths)
    paths.claims.write_text(json.dumps({"verified": True}) + "\n" + json.dumps({"verified": False}) + "\n",
                            encoding="utf-8")
    paths.topics_json.write_text(json.dumps({"Protein Intake": {}, "Protein Intakes": {}, "Sleep": {}}), encoding="utf-8")
    c = checks(doctor.check(paths, store, None))
    assert c["unverified_claims"]["count"] == 1
    assert c["duplicate_topics"]["count"] == 1 and "Protein" in c["duplicate_topics"]["detail"][0]


def test_stale_creators(paths, store, rec):
    worker.update_manifest(paths)
    store.db.execute("UPDATE creators SET last_checked_at='2000-01-01T00:00:00+00:00'")
    c = checks(doctor.check(paths, store, None))
    assert c["stale_creators"]["detail"] == ["jeremy-ethier"]


def test_stale_leases_fixed_only_without_worker(paths, store, rec):
    worker.update_manifest(paths)
    add_row(store, paths, "l1", "fetching", with_transcript=False)
    store.db.execute("UPDATE videos SET lease_until=1")
    lock = WriterLock(paths.writer_lock)
    assert lock.acquire()
    try:
        assert checks(doctor.check(paths, store, None, fix=True))["stale_leases"]["fixed"] == 0
    finally:
        lock.release()
    assert checks(doctor.check(paths, store, None, fix=True))["stale_leases"]["fixed"] == 1
    assert store.get("l1").state == "queued"


def test_manifest_version_fix(paths, store, rec):
    r = doctor.check(paths, store, None)
    assert "manifest" in checks(r)
    doctor.check(paths, store, None, fix=True)
    assert worker.read_manifest(paths)["schema_version"] == 1
    assert "manifest" not in checks(doctor.check(paths, store, None))


def test_paused_is_reported(paths, store, rec):
    worker.update_manifest(paths)
    paths.pause_flag.write_text("x")
    assert "paused" in checks(doctor.check(paths, store, None))
