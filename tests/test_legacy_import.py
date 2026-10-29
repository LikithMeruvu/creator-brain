import gzip
import hashlib
import json
import sqlite3

import pytest

from cbrain.jobs import JobStore
from cbrain.legacy_import import import_old_creator_brain, parse_markdown, read_old_transcripts
from cbrain.models import Creator, JobState, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths

CID = "UCERm5yFZ1SptUEU4wZ2vJvw"
OLD_ID = "ea365adc-0000-0000-0000-000000000001"
OTHER_ID = "bbbbbbbb-0000-0000-0000-000000000002"

MD = """---
video_id: "mdvideo0001"
title: "From markdown"
---

# From markdown

## 00:00

Hello and welcome

## 00:04

to the show.

## 61:05

late line
"""


def build_old(tmp_path):
    root = tmp_path / "creators"
    db = root / "knowledge.db"
    (root / "creators" / OLD_ID / "videos").mkdir(parents=True)
    (root / "creators" / OLD_ID / "videos" / "mdvideo0001.md").write_text(MD, encoding="utf-8")
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE creators (id TEXT PRIMARY KEY, channel_id TEXT, handle TEXT, name TEXT);
    CREATE TABLE videos (video_id TEXT PRIMARY KEY, creator_id TEXT, title TEXT, published_at TEXT,
        duration_seconds INTEGER, language TEXT, status TEXT, transcript_source TEXT, markdown_path TEXT);
    CREATE TABLE caption_imports (video_id TEXT, language TEXT, kind TEXT, content_hash TEXT, payload TEXT);
    """)
    c.execute("INSERT INTO creators VALUES (?,?,?,?)", (OLD_ID, CID, "@JeremyEthier", "Jeremy Ethier"))
    c.execute("INSERT INTO creators VALUES (?,?,?,?)", (OTHER_ID, "UCother", "@other", "Other"))
    rows = [
        ("payload0001", OLD_ID, "From payload", "2026-01-02T10:00:00", 100, "en", "indexed", "browser_caption", None),
        ("mdvideo0001", OLD_ID, "From markdown", None, 400, "en", "indexed", "public_caption", None),
        ("whisper0001", OLD_ID, "Whisper", None, 50, "en", "indexed", "faster_whisper_small", None),
        ("pending0001", OLD_ID, "Not indexed", None, 50, "en", "caption_pending", None, None),
        ("nodata00001", OLD_ID, "Indexed but empty", None, 50, "en", "indexed", "public_caption", None),
        ("other000001", OTHER_ID, "Other creator", None, 50, "en", "indexed", "public_caption", None),
    ]
    c.executemany("INSERT INTO videos VALUES (?,?,?,?,?,?,?,?,?)", rows)
    payload = {"videoId": "payload0001", "segments": [
        {"startMs": 0, "endMs": 1500, "text": "first line"}, {"startMs": 1500, "endMs": 4200, "text": " second  line "},
        {"startMs": 5000, "endMs": 6000, "text": ""}]}
    c.execute("INSERT INTO caption_imports VALUES (?,?,?,?,?)", ("payload0001", "en", "unknown", "h", json.dumps(payload)))
    wp = {"segments": [{"startMs": 0, "endMs": 1000, "text": "whisper words"}]}
    c.execute("INSERT INTO caption_imports VALUES (?,?,?,?,?)", ("whisper0001", "en", "unknown", "h", json.dumps(wp)))
    c.commit()
    c.close()
    return db


@pytest.fixture
def vault(tmp_path):
    paths = VaultPaths(tmp_path / "vault")
    paths.ensure_dirs()
    return paths, JobStore(paths.jobs_db)


def creator(**kw):
    return Creator(**{"slug": "jeremy-ethier", "name": "Jeremy Ethier", "handle": "@JeremyEthier", "channel_id": CID, **kw})


def load(paths, vid):
    return Transcript.from_dict(json.load(gzip.open(paths.transcript(vid), "rt", encoding="utf-8")))


def test_parse_markdown():
    segs = parse_markdown(MD, duration=400)
    assert [(s.start, s.end, s.text) for s in segs] == [
        (0.0, 4.0, "Hello and welcome"), (4.0, 3665.0, "to the show."), (3665.0, 3668.0, "late line")]
    assert parse_markdown("## 1:02:03\n\nx\n", duration=4000)[0].start == 3723.0
    assert parse_markdown("no headings") == []


def test_import_counts_sources_and_timestamps(tmp_path, vault):
    paths, store = vault
    db = build_old(tmp_path)
    n = import_old_creator_brain(db, store, paths, creator())
    assert n == 3
    assert store.counts("jeremy-ethier")["fetched"] == 3 and store.counts("jeremy-ethier")["queued"] == 0
    t = load(paths, "payload0001")
    assert t.source == "legacy_import" and t.language == "en"
    assert [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 1.5, "first line"), (1.5, 4.2, "second line")]
    assert load(paths, "whisper0001").source == "local_whisper"
    assert store.get("whisper0001").transcript_source == "local_whisper"
    assert load(paths, "mdvideo0001").segments[0].text == "Hello and welcome"
    assert store.get("payload0001").title == "From payload" and store.get("payload0001").duration == 100
    assert store.get("payload0001").published == "2026-01-02"
    assert store.get("other000001") is None and store.get("pending0001") is None and store.get("nodata00001") is None
    assert not list(paths.transcripts.glob("*.tmp"))


def test_import_matches_by_handle_when_channel_id_differs(tmp_path, vault):
    paths, store = vault
    db = build_old(tmp_path)
    assert import_old_creator_brain(db, store, paths, creator(channel_id="UCnew")) == 3
    assert import_old_creator_brain(db, store, paths, creator(slug="x", name="Nope", handle="@nobody", channel_id="UCz")) == 0


def test_import_is_idempotent_and_respects_existing_progress(tmp_path, vault):
    paths, store = vault
    db = build_old(tmp_path)
    store.upsert_creator(creator())
    store.add_videos("jeremy-ethier", [VideoMeta(id="payload0001", title="t"), VideoMeta(id="mdvideo0001", title="m")],
                     {"payload0001": 5.0, "mdvideo0001": 4.0})
    store.mark("mdvideo0001", JobState.DONE)                      # finished elsewhere: must not be reset
    assert import_old_creator_brain(db, store, paths, creator()) == 2   # payload (was queued) + whisper (new)
    assert store.get("mdvideo0001").state == "done"
    assert store.get("payload0001").state == "fetched" and store.get("payload0001").priority == 5.0
    assert import_old_creator_brain(db, store, paths, creator()) == 0   # second run: nothing left to import


def test_import_keeps_better_existing_transcript(tmp_path, vault):
    paths, store = vault
    db = build_old(tmp_path)
    store.upsert_creator(creator())
    store.add_videos("jeremy-ethier", [VideoMeta(id="payload0001", title="t")], {"payload0001": 1.0})
    from cbrain.fetch.runner import save_transcript
    save_transcript(paths.transcript("payload0001"), Transcript("payload0001", [Segment(0, 1, "manual")], "manual_caption"))
    import_old_creator_brain(db, store, paths, creator())
    assert load(paths, "payload0001").source == "manual_caption"


def test_source_db_untouched_and_opened_readonly(tmp_path, vault):
    paths, store = vault
    db = build_old(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    import_old_creator_brain(db, store, paths, creator())
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not list(db.parent.glob("knowledge.db-*"))  # no journal/wal created next to the source
    from cbrain.legacy_import import _open_ro
    conn = _open_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO creators VALUES ('x','y','z','w')")
    conn.close()
    assert [m.id for m, _ in read_old_transcripts(db, creator())] == ["mdvideo0001", "payload0001", "whisper0001"]


def test_missing_db(tmp_path, vault):
    paths, store = vault
    assert import_old_creator_brain(tmp_path / "nope.db", store, paths, creator()) == 0
