"""Fakes for the sibling modules the Receptionist calls (discovery, writer, index, runner, thinker, proxies...)."""

from __future__ import annotations

import gzip
import json
import math
import sys
import time
import types
from pathlib import Path

from cbrain.jobs import JobStore
from cbrain.models import Creator, JobState, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths, safe_filename

CREATOR = Creator(slug="jeremy-ethier", name="Jeremy Ethier", handle="@JeremyEthier", channel_id="UC1")


def make_videos(n: int = 10, prefix: str = "vid") -> list[VideoMeta]:
    return [VideoMeta(id=f"{prefix}{i:02d}", title=f"Video {i}", published=None, views=1000 * (n - i),
                      duration=600, channel_id="UC1") for i in range(n)]


class Rec:
    """Everything the fakes were asked to do, plus knobs tests can turn."""

    def __init__(self) -> None:
        self.calls: dict[str, list] = {}
        self.videos = make_videos(10)
        self.latest: list[VideoMeta] = []
        self.hits: list[dict] = []
        self.info: dict[str, dict] = {}
        self.block_runner = False
        self.write_fail: set[str] = set()
        self.claims_per_video = 0
        self.changes: list[dict] = []
        self.list_error: Exception | None = None

    def add(self, name: str, *args) -> None:
        self.calls.setdefault(name, []).append(args)

    def n(self, name: str) -> int:
        return len(self.calls.get(name, []))


def _module(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    return m


def install(monkeypatch, tmp_path: Path) -> Rec:
    """Isolate env + replace sibling modules in sys.modules for the duration of the test."""
    monkeypatch.setenv("CBRAIN_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("CBRAIN_LEGACY_DB", str(tmp_path / "no-legacy.db"))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    for k in list(__import__("os").environ):
        if k.startswith("CBRAIN_") and k not in ("CBRAIN_SECRETS", "CBRAIN_LEGACY_DB"):
            monkeypatch.delenv(k)
    rec = Rec()

    # ---- discovery
    def list_channel(handle, limit=None):
        rec.add("list_channel", handle)
        if rec.list_error:
            raise rec.list_error
        return Creator(**CREATOR.__dict__), list(rec.videos)

    def latest(handle, n=15):
        rec.add("latest", handle, n)
        return list(rec.latest)

    def select(videos, scope, recent_fraction=0.30, top_winners=10, include_shorts=False, include_lives=False):
        rec.add("select", scope, len(videos))
        vids = [v for v in videos if (include_shorts or not v.is_short) and (include_lives or not v.is_live)]
        if scope != "full":
            vids = vids[: max(1, math.ceil(len(vids) * recent_fraction))] if len(videos) > 3 else vids
        return {v.id: float(1000 - i) for i, v in enumerate(vids)}

    discovery = _module("cbrain.discovery", list_channel=list_channel, latest=latest, select=select)

    # ---- vault writer
    class FakeVaultWriter:
        def __init__(self, paths: VaultPaths):
            self.paths = paths

        def ensure_vault(self):
            rec.add("ensure_vault")
            self.paths.ensure_dirs()
            if not self.paths.me.exists():
                self.paths.me.write_text("---\ngoal:\nlevel:\n---\n# Me\n\nbody text\n", encoding="utf-8")

        def video_note_path(self, creator, meta):
            return self.paths.videos_dir(creator.name) / f"{meta.published or 'unknown'} {safe_filename(meta.title)}.md"

        def write_video_note(self, creator, meta, transcript, topics=None, on_screen=None):
            if meta.id in rec.write_fail:
                raise RuntimeError("disk full")
            p = self.video_note_path(creator, meta)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {meta.title}\n[[{creator.name}]]\n", encoding="utf-8")
            rec.add("write_video_note", meta.id, meta.published, tuple(topics or ()))
            return p

        def write_creator_note(self, creator, counts, top_topics, recent_updates):
            p = self.paths.creator_note(creator.name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {creator.name}\n", encoding="utf-8")
            rec.add("write_creator_note", creator.slug, dict(counts), list(top_topics), list(recent_updates))
            return p

        def write_topic_note(self, topic, claims, aliases, conflicts):
            rec.add("write_topic_note", topic)
            return self.paths.topic_note(topic)

        def write_update_note(self, creator, date, new_videos, new_claims, changed):
            p = self.paths.updates_dir(creator.name) / f"{creator.name} - Update {date}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("update\n", encoding="utf-8")
            rec.add("write_update_note", creator.slug, len(new_videos), len(new_claims), list(changed))
            return p

        def write_output(self, title, markdown, sources):
            p = self.paths.outputs / f"2026-10-29 {safe_filename(title)}.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(markdown, encoding="utf-8")
            rec.add("write_output", title, tuple(sources))
            return p

        def write_home(self, creators, counts_by_creator, topics):
            self.paths.home.write_text("# Home\n", encoding="utf-8")
            rec.add("write_home", [c.slug for c in creators], dict(counts_by_creator), list(topics))
            return self.paths.home

    def brain_label(creator, counts, updated):
        return f"{creator.slug} · {creator.scope} · updated {updated} · {counts.get('total', 0)} videos"

    vault_writer = _module("cbrain.vault.writer", VaultWriter=FakeVaultWriter, brain_label=brain_label)

    # ---- search index
    class FakeIndex:
        def __init__(self, db_path):
            self.db_path = db_path

        def index_transcript(self, creator, meta, transcript):
            rec.add("index_transcript", meta.id)
            return len(transcript.segments)

        def index_claims(self, claims):
            rec.add("index_claims", len(claims))

        def search(self, query, creators=None, limit=8, kind="any"):
            rec.add("search", query, tuple(creators or ()), limit, kind)
            out = [h for h in rec.hits if not creators or h["creator"] in creators]
            return out[:limit]

        def remove_video(self, vid):
            rec.add("remove_video", vid)

        def stats(self):
            return {}

    vault_index = _module("cbrain.vault.index", SearchIndex=FakeIndex)

    # ---- fetch runner
    class FakeRunner:
        def __init__(self, store, paths, settings, pool, on_fetched):
            self.store, self.paths, self.on_fetched, self.pool = store, paths, on_fetched, pool
            rec.add("runner_init", pool is not None)

        def run(self, stop=None, max_videos=None):
            rec.add("runner_run")
            n = 0
            while not (stop is not None and stop.is_set()):
                if rec.block_runner:
                    time.sleep(0.02)
                    continue
                rows = self.store.claim(limit=1)
                if not rows:
                    break
                r = rows[0]
                tr = Transcript(video_id=r.id, segments=[Segment(0, 5, f"hello from {r.id}")], source="auto_caption")
                self.paths.transcripts.mkdir(parents=True, exist_ok=True)
                with gzip.open(self.paths.transcript(r.id), "wt", encoding="utf-8") as f:
                    json.dump(tr.to_dict(), f)
                self.store.mark(r.id, JobState.FETCHED, "auto_caption")
                self.on_fetched(r, tr, rec.info.get(r.id, {}))
                n += 1
            return {"fetched": n}

    fetch_runner = _module("cbrain.fetch.runner", FetchRunner=FakeRunner)

    # ---- thinker
    class FakeThinker:
        def __init__(self, store, paths, settings, writer, index):
            self.store, self.paths = store, paths

        def run(self, stop=None, max_videos=None):
            rec.add("thinker_run")
            n = 0
            while not (stop is not None and stop.is_set()):
                rows = self.store.claim(states=("written",), to_state="extracting", limit=1)
                if not rows:
                    break
                r = rows[0]
                for k in range(rec.claims_per_video):
                    claim = {"creator": r.creator_slug, "video_id": r.id, "t": 3.0 + k, "claim": f"claim {k} of {r.id}",
                             "quote": "this is a five word quote", "topic": "Protein Intake", "kind": "advice",
                             "date": "2026-01-01", "verified": True}
                    with open(self.paths.claims, "a", encoding="utf-8") as f:
                        f.write(json.dumps(claim) + "\n")
                self.store.mark(r.id, JobState.DONE)
                n += 1
            return {"processed": n, "changed": list(rec.changes) if n else []}

    think_pipeline = _module("cbrain.think.pipeline", Thinker=FakeThinker)

    # ---- proxies, legacy import
    class FakeProxyPool:
        def __init__(self, paths, settings):
            rec.add("pool_init")

        def refresh(self, target_healthy=60, force=False):
            rec.add("pool_refresh")
            return {}

        def stats(self):
            return {"healthy": rec.calls.get("healthy", [(0,)])[-1][0], "total": 100}

    proxies = _module("cbrain.proxies", ProxyPool=FakeProxyPool)

    def import_old(old_db, store, paths, creator):
        rec.add("legacy_import", str(old_db), creator.slug)
        return 7

    legacy = _module("cbrain.legacy_import", import_old_creator_brain=import_old)

    for name, mod in {"cbrain.discovery": discovery, "cbrain.vault.writer": vault_writer,
                      "cbrain.vault.index": vault_index, "cbrain.fetch.runner": fetch_runner,
                      "cbrain.think.pipeline": think_pipeline, "cbrain.proxies": proxies,
                      "cbrain.legacy_import": legacy}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return rec


def seed_videos(store: JobStore, slug: str = "jeremy-ethier", **state_counts: int) -> None:
    """Insert selected videos directly in the given states, e.g. seed_videos(store, queued=3, done=2)."""
    store.upsert_creator(Creator(**CREATOR.__dict__))
    i = 0
    for state, n in state_counts.items():
        vids = [VideoMeta(id=f"{state[:3]}{i + k:03d}", title=f"{state} {k}", published="2026-01-01", views=10) for k in range(n)]
        i += n
        store.add_videos(slug, vids, {v.id: 1.0 for v in vids})
        for v in vids:
            store.db.execute("UPDATE videos SET state=? WHERE id=?", (state, v.id))
