import json

import pytest

from cbrain.models import Creator, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths
from cbrain.think.extract import (agent_claims_path, extract_claims, ingest_agent_batch, make_windows,
                                  parse_claims_payload, parse_timestamp, write_agent_batch)
from cbrain.think.verify import verify_claims

CREATOR = Creator(slug="jeremy-ethier", name="Jeremy Ethier", handle="@J", channel_id="UC1")
META = VideoMeta(id="vid1", title="Protein guide", published="2026-01-05")


def transcript(n=400):
    segs = [Segment(start=i * 5.0, end=i * 5.0 + 5, text=f"this is spoken sentence number {i} about training") for i in range(n)]
    return Transcript(video_id="vid1", segments=segs, source="manual_caption")


class FakeLLM:
    def __init__(self, fn=None):
        self.fn = fn or (lambda s, u: {"claims": []})
        self.prompts = []

    def available(self):
        return True

    def complete_json(self, system, user, max_tokens=4000):
        self.prompts.append((system, user))
        return self.fn(system, user)


def test_windows_cover_everything_with_markers():
    t = transcript()
    ws = make_windows(t, 6000)
    assert len(ws) > 3 and all(len(w.text) <= 6200 for w in ws)
    joined = "\n".join(w.text for w in ws)
    assert joined.count("[") == 400
    assert ws[0].text.startswith("[00:00] this is spoken sentence number 0")
    assert "[01:15]" in joined  # segment 15 starts at 75 s
    assert ws[1].start > 0


def test_parse_timestamp():
    assert parse_timestamp("01:30") == 90 and parse_timestamp("[1:02:03]") == 3723
    assert parse_timestamp(45) == 45.0 and parse_timestamp("abc") is None and parse_timestamp(None) is None


def test_parse_payload_is_defensive():
    payload = {"claims": [
        {"claim": " Eat protein. ", "quote": "[00:10] \"eat more protein every day\"", "t": "00:10",
         "topic": "protein intake", "kind": "Protocol"},
        {"claim": "no quote"}, "junk", {"claim": "x", "quote": "y y y y y y", "t": "??", "kind": "weird"},
    ]}
    cs = parse_claims_payload(payload, "c", "v", "2026-01-01", default_t=99)
    assert len(cs) == 2
    assert cs[0].quote == "eat more protein every day" and cs[0].t == 10 and cs[0].kind == "protocol"
    assert cs[0].topic == "Protein Intake" and cs[0].creator == "c" and cs[0].date == "2026-01-01"
    assert cs[1].t == 99 and cs[1].kind == "advice"
    assert parse_claims_payload([{"claim": "a", "quote": "b"}], "c", "v", None)[0].claim == "a"
    assert parse_claims_payload("garbage", "c", "v", None) == []
    assert parse_claims_payload({"claim": "a", "quote": "b"}, "c", "v", None)[0].quote == "b"


def test_extract_runs_every_window_with_known_topics_and_dedupes():
    t = transcript()

    def fn(system, user):
        return {"claims": [{"claim": "Same idea.", "quote": "this is spoken sentence number 1 about training",
                            "t": "00:00", "topic": "Training", "kind": "fact"}]}

    llm = FakeLLM(fn)
    out = extract_claims(CREATOR, META, t, llm, known_topics=["Protein Intake", "Sleep"])
    assert len(llm.prompts) == len(make_windows(t))
    assert len(out) == 1  # identical quote from several windows collapses
    system, user = llm.prompts[0]
    assert "EXACTLY" in system and "JSON" in system and "6 to 40" in system
    assert "Protein Intake; Sleep" in user and "Protein guide" in user and "[00:00]" in user


def test_extract_propagates_llm_failure():
    def boom(s, u):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        extract_claims(CREATOR, META, transcript(40), FakeLLM(boom))


def test_extract_empty_transcript():
    assert extract_claims(CREATOR, META, Transcript("v", [], "auto_caption"), FakeLLM()) == []


# ---- agent batch round trip ------------------------------------------------------------------------------------

def test_agent_batch_round_trip(tmp_path):
    paths = VaultPaths(tmp_path / "vault")
    t = Transcript("vid1", [Segment(0, 5, "intro hello"), Segment(5, 10, "you should eat more protein every day"),
                            Segment(10, 15, "and sleep well at night too")], "auto_caption")
    md = write_agent_batch(paths, CREATOR, META, t, known_topics=["Sleep"])
    assert md == paths.pending_extraction / "vid1.md"
    text = md.read_text(encoding="utf-8")
    assert "vid1.claims.json" in text and "[00:05] you should eat more protein every day" in text
    assert "EXACTLY" in text and "KNOWN TOPICS: Sleep" in text and not md.with_name("vid1.md.tmp").exists()

    with pytest.raises(FileNotFoundError):
        ingest_agent_batch(paths, md)

    answer = {"video_id": "vid1", "claims": [
        {"claim": "Eat more protein daily.", "quote": "you should eat more protein every day", "t": "00:05",
         "topic": "Protein Intake", "kind": "advice"},
        {"claim": "Invented.", "quote": "creatine cures everything and more", "t": "00:09", "topic": "Creatine"},
    ]}
    agent_claims_path(paths, "vid1").write_text(json.dumps(answer), encoding="utf-8")
    for f in (md, agent_claims_path(paths, "vid1")):  # both entry points work
        claims = ingest_agent_batch(paths, f)
        assert [c.creator for c in claims] == ["jeremy-ethier"] * 2 and claims[0].date == "2026-01-05"
        assert all(not c.verified for c in claims)
    kept, rejected = verify_claims(claims, t)
    assert [c.claim for c in kept] == ["Eat more protein daily."] and kept[0].t == 5.0 and kept[0].verified
    assert [c.claim for c in rejected] == ["Invented."]


def test_agent_ingest_rejects_malformed_file(tmp_path):
    paths = VaultPaths(tmp_path)
    paths.pending_extraction.mkdir(parents=True)
    f = paths.pending_extraction / "x.claims.json"
    f.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        ingest_agent_batch(paths, f)
    f.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(ValueError):
        ingest_agent_batch(paths, f)
    f.write_text(json.dumps({"claims": []}), encoding="utf-8")
    assert ingest_agent_batch(paths, f) == []
