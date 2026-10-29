import gzip
import json
import threading

import pytest

from cbrain.config import Settings
from cbrain.jobs import JobStore
from cbrain.models import Claim, Creator, JobState, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths
from cbrain.think.extract import agent_claims_path
from cbrain.think.pipeline import Thinker

LINE_A = "you should eat about one gram of protein per pound daily"
LINE_B = "make sure you sleep eight hours every single night"


class FakeWriter:
    def __init__(self):
        self.calls = []

    def write_topic_note(self, topic, claims, aliases, conflicts):
        self.calls.append((topic, list(claims), list(aliases), list(conflicts)))


class FakeIndex:
    def __init__(self):
        self.batches = []

    def index_claims(self, claims):
        self.batches.append(list(claims))


class FakeLLM:
    """Extraction: one claim quoting the first line of each window (+ one fabricated). Changes: scripted."""

    def __init__(self, available=True, contradict=False, fail_on=None):
        self._available, self.contradict, self.fail_on = available, contradict, fail_on or set()
        self.extract_calls = 0
        self.change_calls = 0
        self._lock = threading.Lock()

    def available(self):
        return self._available

    def estimate_cost_usd(self, chars):
        return chars / 1_000_000

    def complete_json(self, system, user, max_tokens=4000):
        if "compare what ONE creator" in system:
            self.change_calls += 1
            return {"results": [{"i": 0, "contradicts": self.contradict, "summary": "Target changed."}]}
        with self._lock:
            self.extract_calls += 1
        for vid in self.fail_on:
            if vid in user:
                raise RuntimeError("model exploded")
        body = user.split("TRANSCRIPT WINDOW")[1].splitlines()[1:]
        first = [l for l in body if l.startswith("[")][1]  # the content line, not the intro
        ts, text = first[1:6], first[8:]
        topic = "Sleep Quality" if "sleep" in text else "Daily Protein"
        return {"claims": [
            {"claim": "Speaker's advice.", "quote": text, "t": ts, "topic": topic, "kind": "advice"},
            {"claim": "Fabricated.", "quote": "creatine melts fat while you watch television", "t": ts, "topic": "Creatine"},
        ]}


@pytest.fixture
def env(tmp_path):
    paths = VaultPaths(tmp_path / "vault")
    paths.ensure_dirs()
    store = JobStore(paths.jobs_db)
    store.upsert_creator(Creator(slug="jeremy", name="Jeremy", handle="@J", channel_id="UC1"))
    return paths, store


def add_video(paths, store, vid, line, published="2025-01-01", state=JobState.WRITTEN):
    store.add_videos("jeremy", [VideoMeta(id=vid, title=f"Title {vid}", published=published, duration=600)], {vid: 1.0})
    segs = [Segment(0, 5, "intro hello everyone"), Segment(5, 10, line), Segment(10, 15, "thanks for watching")]
    t = Transcript(vid, segs, "manual_caption")
    with gzip.open(paths.transcript(vid), "wt", encoding="utf-8") as f:
        json.dump(t.to_dict(), f)
    store.mark(vid, state)


def make(env, llm=None, **kw):
    paths, store = env
    writer, index = FakeWriter(), FakeIndex()
    th = Thinker(store, paths, Settings(groq_api_key="k"), writer, index, llm=llm or FakeLLM(), **kw)
    return th, writer, index


def claims_lines(paths):
    return [json.loads(l) for l in paths.claims.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_process_extracts_verifies_and_persists(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    th, writer, index = make(env)
    kept = th.process(store.get("v1"))
    assert len(kept) == 1 and kept[0].verified and kept[0].topic == "Protein Intake"  # canonical, fabricated one dropped
    assert kept[0].t == 5.0 and kept[0].date == "2025-01-01" and kept[0].creator == "jeremy"
    rows = claims_lines(paths)
    assert len(rows) == 1 and rows[0]["id"] == kept[0].id and rows[0]["verified"] is True
    topics = json.loads(paths.topics_json.read_text(encoding="utf-8"))
    assert list(topics) == ["Protein Intake"] and topics["Protein Intake"]["aliases"] == ["Daily Protein"]
    topic, claims, aliases, conflicts = writer.calls[-1]
    assert topic == "Protein Intake" and aliases == ["Daily Protein"] and conflicts == [] and claims[0].id == kept[0].id
    assert [c.id for c in index.batches[-1]] == [kept[0].id]
    assert th.totals["rejected"] == 1


def test_claims_jsonl_dedupes_across_runs_and_instances(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    th, _, _ = make(env)
    th.process(store.get("v1"))
    th.process(store.get("v1"))
    th2, _, _ = make(env)  # fresh process reloads the file
    th2.process(store.get("v1"))
    assert len(claims_lines(paths)) == 1


def test_same_quote_with_reworded_claim_is_not_duplicated(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    th, _, _ = make(env)
    th.process(store.get("v1"))
    row = store.get("v1")
    reworded = Claim(creator="jeremy", video_id="v1", t=5.0, claim="A different wording.", quote=LINE_A,
                     topic="Protein Intake")
    th.process(row, raw_claims=[reworded])
    assert len(claims_lines(paths)) == 1


def test_run_moves_jobs_to_done_and_reports(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    add_video(paths, store, "v2", LINE_B, published="2025-02-01")
    th, writer, index = make(env)
    done = []
    stats = th.run(on_done=lambda row, kept, changes: done.append((row.id, len(kept))))
    assert stats["mode"] == "llm" and stats["processed"] == 2 and stats["failed"] == 0
    assert stats["claims"] == 2 and stats["rejected"] == 2
    assert {r.state for r in store.videos()} == {"done"}
    assert sorted(done) == [("v1", 1), ("v2", 1)]
    assert sorted(t for t, *_ in writer.calls) == ["Protein Intake", "Sleep"]
    assert len(claims_lines(paths)) == 2


def test_failure_returns_job_to_written_with_error(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    add_video(paths, store, "v2", LINE_B)
    th, _, _ = make(env, llm=FakeLLM(fail_on={"Title v2"}))
    stats = th.run()
    assert stats["processed"] == 1 and stats["failed"] == 1
    bad = store.get("v2")
    assert bad.state == "written" and bad.attempts == 1 and "model exploded" in bad.last_error
    assert store.get("v1").state == "done"


def test_repeated_failures_park_job(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    th, _, _ = make(env, llm=FakeLLM(fail_on={"Title v1"}))
    th.settings.max_attempts = 1
    th.run()
    assert store.get("v1").state == "parked"


def test_missing_transcript_fails_cleanly(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    paths.transcript("v1").unlink()
    th, _, _ = make(env)
    stats = th.run()
    assert stats["failed"] == 1 and store.get("v1").state == "written"


def test_max_videos_and_pause_and_stop(env):
    paths, store = env
    for i in range(3):
        add_video(paths, store, f"v{i}", LINE_A, published=f"2025-01-0{i + 1}")
    th, _, _ = make(env)
    assert th.run(max_videos=2)["processed"] == 2
    paths.pause_flag.write_text("x")
    assert th.run()["processed"] == 0
    paths.pause_flag.unlink()
    stop = threading.Event()
    stop.set()
    assert th.run(stop=stop)["processed"] == 0
    assert th.run()["processed"] == 1


def test_changes_are_detected_stored_and_passed_to_topic_note(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A, published="2024-01-01")
    add_video(paths, store, "v2", "you should eat about point seven grams of protein per pound daily", published="2025-03-01")
    th, writer, _ = make(env, llm=FakeLLM(contradict=True))
    th.process(store.get("v1"))
    assert writer.calls[-1][3] == []
    th.process(store.get("v2"))
    topic, claims, aliases, conflicts = writer.calls[-1]
    assert len(claims) == 2 and len(conflicts) == 1
    assert conflicts[0]["summary"] == "Target changed." and conflicts[0]["new"]["video_id"] == "v2"
    assert json.loads((paths.brain / "conflicts.json").read_text(encoding="utf-8"))["Protein Intake"]
    th.process(store.get("v2"))  # reprocessing must not duplicate the conflict
    assert len(writer.calls[-1][3]) == 1


def test_run_reports_changes(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A, published="2024-01-01")
    add_video(paths, store, "v2", "you should eat about point seven grams of protein per pound daily", published="2025-03-01")
    th, _, _ = make(env, llm=FakeLLM(contradict=True))
    stats = th.run()
    assert len(stats["changed"]) == 1 and stats["changed"][0]["creator"] == "jeremy"


# ---- no-Groq agent mode ----------------------------------------------------------------------------------------

def test_agent_mode_round_trip(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    add_video(paths, store, "v2", LINE_B)
    th, writer, index = make(env, llm=FakeLLM(available=False))

    stats = th.run()
    assert stats["mode"] == "agent" and stats["pending_agent_batches"] == 2 and stats["processed"] == 0
    assert (paths.pending_extraction / "v1.md").exists() and (paths.pending_extraction / "v2.md").exists()
    assert {r.state for r in store.videos()} == {"written"} and not writer.calls

    agent_claims_path(paths, "v1").write_text(json.dumps({"video_id": "v1", "claims": [
        {"claim": "Eat a gram of protein per pound.", "quote": LINE_A, "t": "00:05", "topic": "Protein Intake"},
        {"claim": "Made up.", "quote": "an utterly invented sentence about nothing", "t": "00:06", "topic": "X"}]}),
        encoding="utf-8")
    agent_claims_path(paths, "v2").write_text("garbage", encoding="utf-8")

    stats = th.run()
    assert stats["processed"] == 1 and stats["pending_agent_batches"] == 1 and stats["claims"] == 1
    assert stats["rejected"] == 1 and len(stats["errors"]) == 1
    assert store.get("v1").state == "done" and store.get("v2").state == "written"
    assert not (paths.pending_extraction / "v1.md").exists()
    assert (paths.pending_extraction / "ingested" / "v1.claims.json").exists()
    assert (paths.pending_extraction / "v2.claims.invalid.json").exists() and (paths.pending_extraction / "v2.md").exists()
    assert [c.video_id for c in index.batches[-1]] == ["v1"] and writer.calls[-1][0] == "Protein Intake"

    # the agent fixes v2
    agent_claims_path(paths, "v2").write_text(json.dumps([{"claim": "Sleep 8h.", "quote": LINE_B, "t": "00:05",
                                                         "topic": "Sleep"}]), encoding="utf-8")
    stats = th.run()
    assert stats["processed"] == 1 and stats["pending_agent_batches"] == 0
    assert {r.state for r in store.videos()} == {"done"}
    assert len(claims_lines(paths)) == 2


# ---- estimate --------------------------------------------------------------------------------------------------

def test_estimate(env):
    paths, store = env
    add_video(paths, store, "v1", LINE_A)
    add_video(paths, store, "v2", LINE_B, state=JobState.FETCHED)
    store.add_videos("jeremy", [VideoMeta(id="v3", title="q", published="2025-01-01")], {"v3": 1.0})
    th, _, _ = make(env)
    est = th.estimate()
    assert est["pending_videos"] == 1 and est["chars"] > 50 and est["not_yet_written"] == 2
    assert est["llm_available"] is True and est["needs_confirm"] is False
    assert th.estimate("nobody")["pending_videos"] == 0
