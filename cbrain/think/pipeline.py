"""Thinker: WRITTEN jobs -> claims (extract, verify) -> claims.jsonl -> topic registry -> topic notes + index -> DONE."""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Callable

from ..config import Settings
from ..jobs import JobStore, VideoRow
from ..models import Claim, Creator, JobState, Transcript
from ..paths import VaultPaths
from .changes import detect_changes
from .extract import (agent_batch_path, agent_claims_path, archive_agent_files, extract_claims,
                      ingest_agent_batch, write_agent_batch)
from .llm import LLMClient, RateLimited
from .topics import TopicRegistry
from .verify import normalize, verify_claims

log = logging.getLogger("cbrain.think")

WORKERS = 4
LEASE_SECONDS = 1800
FALLBACK_CHARS_PER_SECOND = 15  # when a transcript file is unreadable: ~150 wpm of speech


class Thinker:
    def __init__(self, store: JobStore, paths: VaultPaths, settings: Settings, writer, index,
                 llm: LLMClient | None = None, registry: TopicRegistry | None = None):
        self.store, self.paths, self.settings = store, paths, settings
        self.writer, self.index = writer, index
        self.llm = llm or LLMClient(settings)
        self.registry = registry or TopicRegistry(paths)
        self.totals: Counter = Counter()
        self._lock = threading.RLock()
        self._loaded = False
        self._claims: dict[str, Claim] = {}
        self._quote_keys: set[tuple[str, str]] = set()
        self._by_topic: dict[str, dict[str, Claim]] = {}
        self._conflicts: dict[str, list[dict]] = {}
        self._new_changes: list[dict] = []

    # ---- state on disk --------------------------------------------------
    @property
    def conflicts_path(self):
        return self.paths.brain / "conflicts.json"

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded:
                return
            if self.paths.claims.exists():
                with self.paths.claims.open(encoding="utf-8") as f:
                    for n, line in enumerate(f, 1):
                        if not line.strip():
                            continue
                        try:
                            c = Claim.from_dict(json.loads(line))
                        except (ValueError, TypeError):
                            log.warning("claims.jsonl line %d is unreadable; skipped", n)
                            continue
                        self._remember(replace(c, topic=self.registry.canonical(c.topic)))
            if self.conflicts_path.exists():
                try:
                    self._conflicts = json.loads(self.conflicts_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self._conflicts = {}
            self._loaded = True

    def _remember(self, c: Claim) -> None:
        self._claims[c.id] = c
        self._quote_keys.add((c.video_id, normalize(c.quote)))
        self._by_topic.setdefault(c.topic, {})[c.id] = c

    def _append_claims(self, claims: list[Claim]) -> None:
        if not claims:
            return
        self.paths.claims.parent.mkdir(parents=True, exist_ok=True)
        data = "".join(json.dumps(c.to_dict(), ensure_ascii=False) + "\n" for c in claims)
        with self.paths.claims.open("a", encoding="utf-8", newline="\n") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def _save_conflicts(self) -> None:
        tmp = self.conflicts_path.with_name("conflicts.json.tmp")
        tmp.write_text(json.dumps(self._conflicts, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.conflicts_path)

    def _load_transcript(self, video_id: str) -> Transcript:
        with gzip.open(self.paths.transcript(video_id), "rt", encoding="utf-8") as f:
            return Transcript.from_dict(json.load(f))

    def _creator(self, row: VideoRow) -> Creator:
        return self.store.get_creator(row.creator_slug) or Creator(
            slug=row.creator_slug, name=row.creator_slug, handle="", channel_id="")

    # ---- one video ------------------------------------------------------
    def process(self, row: VideoRow, creator: Creator | None = None, raw_claims: list[Claim] | None = None) -> list[Claim]:
        """extract -> verify -> append claims.jsonl -> topics -> topic notes + index. Returns the kept claims."""
        creator = creator or self._creator(row)
        meta = row.meta()
        transcript = self._load_transcript(row.id)
        self._ensure_loaded()
        if raw_claims is None:
            raw_claims = extract_claims(creator, meta, transcript, self.llm, known_topics=self.registry.all_topics())
        else:
            raw_claims = [replace(c, creator=creator.slug, video_id=row.id, date=meta.published) for c in raw_claims]
        kept, rejected = verify_claims(raw_claims, transcript)
        with self._lock:
            self.totals["rejected"] += len(rejected)
        if rejected:
            log.info("%s: rejected %d of %d claims (quote not found in transcript)", row.id, len(rejected),
                     len(raw_claims))
        return self._commit(kept)

    def _commit(self, kept: list[Claim]) -> list[Claim]:
        unique = list({c.id: c for c in kept}.values())
        if not unique:
            return []
        with self._lock:
            grouped = self.registry.add_claims(unique)
            effective: list[Claim] = []
            fresh: list[Claim] = []
            for c in (c for cs in grouped.values() for c in cs):
                if c.id in self._claims:
                    effective.append(self._claims[c.id])
                elif (c.video_id, normalize(c.quote)) in self._quote_keys:
                    continue  # same quote already stored for this video under different wording
                else:
                    fresh.append(c)
                    effective.append(c)
            self._append_claims(fresh)
            for c in fresh:
                self._remember(c)
            topics = sorted({c.topic for c in effective})
            snapshot = {t: list(self._by_topic[t].values()) for t in topics}
            focus = {t: {c.id for c in effective if c.topic == t} for t in topics}
        changed = {t: detect_changes(t, snapshot[t], self.llm if self.llm.available() else None, focus[t])
                   for t in topics}
        with self._lock:
            for t in topics:
                have = {(x["old"]["id"], x["new"]["id"]) for x in self._conflicts.get(t, [])}
                for x in changed[t]:
                    if (x["old"]["id"], x["new"]["id"]) not in have:
                        self._conflicts.setdefault(t, []).append(x)
                        self._new_changes.append(x)
            if any(changed.values()):
                self._save_conflicts()
            for t in topics:
                ordered = sorted(snapshot[t], key=lambda c: (c.date or "", c.creator, c.video_id, c.t), reverse=True)
                self.writer.write_topic_note(t, ordered, self.registry.aliases(t), self._conflicts.get(t, []))
            self.index.index_claims(effective)
            self.totals["claims"] += len(fresh)
        return effective

    # ---- batch run ------------------------------------------------------
    def _paused(self, stop: threading.Event | None) -> bool:
        return bool(stop is not None and stop.is_set()) or self.paths.pause_flag.exists()

    def run(self, stop: threading.Event | None = None, max_videos: int | None = None,
            on_done: Callable[[VideoRow, list[Claim], list[dict]], None] | None = None) -> dict:
        """Claim WRITTEN jobs -> EXTRACTING -> DONE (4 in parallel). Without a Groq key, switches to agent batches:
        writes pending_extraction/<id>.md for each video, ingests any <id>.claims.json the agent has written, and
        leaves the rest WRITTEN (see "pending_agent_batches" in the result)."""
        before = Counter(self.totals)
        self._new_changes = []
        stats: dict = {"mode": "llm", "processed": 0, "failed": 0, "claims": 0, "rejected": 0, "changed": [],
                       "pending_agent_batches": 0, "errors": []}
        if self.llm.available():
            self._run_llm(stop, max_videos, on_done, stats)
        else:
            stats["mode"] = "agent"
            self._run_agent(stop, max_videos, on_done, stats)
        stats["claims"] = self.totals["claims"] - before["claims"]
        stats["rejected"] = self.totals["rejected"] - before["rejected"]
        stats["changed"] = list(self._new_changes)
        return stats

    def _fail(self, row: VideoRow, err: Exception, stats: dict) -> None:
        msg = f"{type(err).__name__}: {err}"
        if isinstance(err, RateLimited):  # quota, not a broken video: retry later without using up attempts
            self.store.defer(row.id, err.retry_in, back_to=JobState.WRITTEN)
            stats.setdefault("rate_limited", 0)
            stats["rate_limited"] += 1
            return
        log.warning("thinker failed on %s: %s", row.id, msg)
        self.store.fail(row.id, msg, back_to=JobState.WRITTEN, max_attempts=self.settings.max_attempts)
        stats["failed"] += 1
        stats["errors"].append(f"{row.id}: {msg}"[:300])

    def _finish(self, row: VideoRow, kept: list[Claim], on_done, stats: dict) -> None:
        self.store.mark(row.id, JobState.DONE)
        stats["processed"] += 1
        if on_done:
            on_done(row, kept, list(self._new_changes))

    def _run_llm(self, stop, max_videos, on_done, stats: dict) -> None:
        budget = max_videos if max_videos is not None else float("inf")
        creators: dict[str, Creator] = {}
        inflight: dict = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            while True:
                while len(inflight) < WORKERS and budget > 0 and not self._paused(stop):
                    rows = self.store.claim(states=(JobState.WRITTEN.value,), limit=1,
                                            lease_seconds=LEASE_SECONDS, to_state=JobState.EXTRACTING.value)
                    if not rows:
                        break
                    row = rows[0]
                    creator = creators.get(row.creator_slug) or creators.setdefault(row.creator_slug, self._creator(row))
                    inflight[pool.submit(self.process, row, creator)] = row
                    budget -= 1
                if not inflight:
                    break
                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:  # store is only touched from this thread
                    row = inflight.pop(fut)
                    try:
                        kept = fut.result()
                    except Exception as e:  # noqa: BLE001 - any failure sends the job back for a retry
                        self._fail(row, e, stats)
                    else:
                        self._finish(row, kept, on_done, stats)

    def _run_agent(self, stop, max_videos, on_done, stats: dict) -> None:
        creators: dict[str, Creator] = {}
        handled = 0
        for row in self.store.videos(state=JobState.WRITTEN.value):
            if self._paused(stop) or (max_videos is not None and handled >= max_videos):
                break
            creator = creators.get(row.creator_slug) or creators.setdefault(row.creator_slug, self._creator(row))
            answer = agent_claims_path(self.paths, row.id)
            if answer.exists():
                try:
                    raw = ingest_agent_batch(self.paths, answer)
                except (ValueError, OSError) as e:
                    bad = answer.with_name(f"{row.id}.claims.invalid.json")
                    os.replace(answer, bad)
                    stats["errors"].append(f"{row.id}: {e} (moved to {bad.name}; the agent should redo it)")
                    stats["pending_agent_batches"] += 1
                    continue
                handled += 1
                self.store.mark(row.id, JobState.EXTRACTING)
                try:
                    kept = self.process(row, creator, raw)
                except Exception as e:  # noqa: BLE001
                    self._fail(row, e, stats)
                    continue
                archive_agent_files(self.paths, row.id)
                self._finish(row, kept, on_done, stats)
            else:
                if not agent_batch_path(self.paths, row.id).exists():
                    handled += 1
                    try:
                        write_agent_batch(self.paths, creator, row.meta(), self._load_transcript(row.id),
                                          self.registry.all_topics())
                    except Exception as e:  # noqa: BLE001
                        stats["errors"].append(f"{row.id}: cannot write batch: {e}"[:300])
                        continue
                stats["pending_agent_batches"] += 1

    # ---- estimate -------------------------------------------------------
    def estimate(self, creator_slug: str | None = None) -> dict:
        """Pending thinker work: videos in WRITTEN/EXTRACTING, transcript characters, rough USD."""
        rows = [r for s in (JobState.WRITTEN.value, JobState.EXTRACTING.value)
                for r in self.store.videos(creator_slug, s)]
        chars = 0
        for r in rows:
            try:
                chars += len(self._load_transcript(r.id).text())
            except (OSError, ValueError, KeyError):
                chars += (r.duration or 0) * FALLBACK_CHARS_PER_SECOND
        usd = self.llm.estimate_cost_usd(chars)
        upstream = sum(len(self.store.videos(creator_slug, s)) for s in
                       (JobState.QUEUED.value, JobState.FETCHING.value, JobState.FETCHED.value))
        return {"pending_videos": len(rows), "chars": chars, "est_usd": round(usd, 4),
                "needs_confirm": usd > self.settings.cost_confirm_usd, "llm_available": self.llm.available(),
                "not_yet_written": upstream}
