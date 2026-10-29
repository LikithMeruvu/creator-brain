import threading

import pytest

from cbrain.models import Chapter, Claim, Creator, Segment, Transcript, VideoMeta
from cbrain.vault.index import SearchIndex, build_chunks

JEREMY = Creator(slug="jeremy-ethier", name="Jeremy Ethier", handle="@J", channel_id="UC1")
NIPPARD = Creator(slug="jeff-nippard", name="Jeff Nippard", handle="@N", channel_id="UC2")


def tr(vid, lines, step=10.0, chapters=None):
    segs = [Segment(i * step, (i + 1) * step, t) for i, t in enumerate(lines)]
    return Transcript(video_id=vid, segments=segs, source="manual_caption", chapters=chapters or [])


@pytest.fixture
def idx(tmp_path):
    i = SearchIndex(tmp_path / "x" / "search.sqlite")
    yield i
    i.close()


def test_chunk_sizes_and_chapter_boundaries():
    segs = [Segment(i * 5.0, (i + 1) * 5.0, f"word{i}") for i in range(100)]          # 500 s, 5 s segments
    chunks = build_chunks(segs, [Chapter(0, "A"), Chapter(100, "B"), Chapter(235, "C")])
    for c in chunks[:-1]:
        dur = c["t_end"] - c["t_start"]
        assert dur <= 90 + 1e-6
    # a chunk never crosses a chapter start
    for c in chunks:
        for ch in (100, 235):
            assert not (c["t_start"] < ch < c["t_end"] - 1e-6) or c["t_start"] >= ch
    starts = {c["t_start"] for c in chunks}
    assert 100 in starts and 235 in starts          # first segment at/after each chapter starts a chunk
    assert {c["chapter"] for c in chunks} == {"A", "B", "C"}
    # without chapter pressure chunks reach at least 45 s
    plain = build_chunks(segs)
    assert all(45 <= c["t_end"] - c["t_start"] <= 90 for c in plain[:-1])
    assert sum(c["text"].count("word") for c in plain) == 100       # nothing lost or duplicated


def test_chunk_sentence_end_closes_early():
    lines = ["a b c"] * 12
    lines[5] = "this one ends the sentence."      # segment 5 ends at 60 s
    chunks = build_chunks([Segment(i * 10.0, (i + 1) * 10.0, t) for i, t in enumerate(lines)])
    assert chunks[0]["t_end"] == 60.0


def test_index_and_search_basic(idx):
    meta = VideoMeta(id="v1", title="Protein Guide", published="2026-01-01")
    lines = ["welcome", "today we talk about progressive overload for muscle growth", "squats and bench"] + ["filler words"] * 20
    assert idx.index_transcript(JEREMY, meta, tr("v1", lines)) >= 1
    hits = idx.search("progressive overload")
    assert hits and hits[0]["video_id"] == "v1" and hits[0]["creator"] == "jeremy-ethier"
    h = hits[0]
    assert h["kind"] == "chunk" and h["url"] == "https://www.youtube.com/watch?v=v1&t=0s" and h["t"] == 0
    assert "**" in h["snippet"] and h["score"] > 0 and h["title"] == "Protein Guide"
    assert idx.search("overloading")                      # porter stemming
    assert idx.stats()["videos"] == 1


def test_reindex_replaces_and_remove(idx):
    meta = VideoMeta(id="v1", title="T", published="2026-01-01")
    idx.index_transcript(JEREMY, meta, tr("v1", ["alpha beta"] * 8))
    idx.index_transcript(JEREMY, meta, tr("v1", ["gamma delta"] * 8))
    assert idx.search("alpha") == []
    assert idx.search("gamma")
    n = idx.stats()["chunks"]
    idx.index_transcript(JEREMY, meta, tr("v1", ["gamma delta"] * 8))
    assert idx.stats()["chunks"] == n
    idx.remove_video("v1")
    assert idx.search("gamma") == [] and idx.stats()["chunks"] == 0


def test_title_boost_and_creator_filter(idx):
    idx.index_transcript(JEREMY, VideoMeta(id="a", title="Creatine explained", published="2026-01-01"),
                         tr("a", ["intro words here"] * 9))
    idx.index_transcript(NIPPARD, VideoMeta(id="b", title="Random video", published="2026-01-02"),
                         tr("b", ["we mention creatine once"] + ["other talk"] * 8))
    hits = idx.search("creatine")
    assert [h["video_id"] for h in hits][0] == "a"        # title match ranks above a body mention
    only = idx.search("creatine", creators=["jeff-nippard"])
    assert [h["video_id"] for h in only] == ["b"]
    assert idx.search("creatine", creators=["nobody"]) == []


def test_and_then_or_fallback(idx):
    idx.index_transcript(JEREMY, VideoMeta(id="a", title="x", published="2026-01-01"), tr("a", ["squat form tips"] * 9))
    idx.index_transcript(JEREMY, VideoMeta(id="b", title="y", published="2026-01-02"), tr("b", ["bench press grip"] * 9))
    both = idx.search("squat bench")                      # no chunk has both -> OR fallback finds both videos
    assert {h["video_id"] for h in both} == {"a", "b"}
    one = idx.search("squat form")                        # AND succeeds -> only matching video
    assert {h["video_id"] for h in one} == {"a"}


@pytest.mark.parametrize("q", ['c++', 'AND', 'OR NOT', '"unbalanced', 'foo AND', '(a OR b', 'NEAR(a b)', '*', "it's", '"', '', '   ',
                               'col:val', '-minus', 'a" OR "b'])
def test_hostile_queries_never_crash(idx, q):
    idx.index_transcript(JEREMY, VideoMeta(id="a", title="C++ talk", published="2026-01-01"),
                         tr("a", ["learning c plus plus is fun"] * 9))
    assert isinstance(idx.search(q), list)


def test_special_queries_find_things(idx):
    idx.index_transcript(JEREMY, VideoMeta(id="a", title="C++ talk", published="2026-01-01"),
                         tr("a", ["learning c and the squat and bench press"] * 9))
    assert idx.search("c++")
    assert idx.search('"squat and bench"')
    assert idx.search("squat AND bench")                  # operator word ignored, both terms searched
    assert idx.search("AND") == [] or isinstance(idx.search("AND"), list)


def test_claims_indexed_and_searched(idx):
    meta = VideoMeta(id="v1", title="Guide", published="2026-01-01")
    idx.index_transcript(JEREMY, meta, tr("v1", ["filler"] * 9))
    claims = [Claim("jeremy-ethier", "v1", 42, "Eat 2 g of protein per kg", "two grams of protein per kilogram", "Protein Intake",
                    date="2026-01-01", verified=True)]
    idx.index_claims(claims)
    idx.index_claims(claims)                              # idempotent
    assert idx.stats()["claims"] == 1
    hits = idx.search("protein per kg", kind="claims")
    assert len(hits) == 1 and hits[0]["kind"] == "claim" and hits[0]["title"] == "Guide"
    assert hits[0]["url"].endswith("&t=42s") and hits[0]["topic"] == "Protein Intake" and hits[0]["verified"] is True
    assert idx.search("protein", kind="chunks") == []
    assert idx.search("protein")[0]["kind"] == "claim"
    idx.remove_video("v1")
    assert idx.search("protein", kind="claims") == []


def test_claim_before_transcript_gets_title_later(idx):
    idx.index_claims([Claim("jeremy-ethier", "v9", 5, "Sleep eight hours", "sleep eight hours every night", "Sleep")])
    idx.index_transcript(JEREMY, VideoMeta(id="v9", title="Sleep Video", published="2026-03-03"), tr("v9", ["x"] * 9))
    assert idx.search("sleep", kind="claims")[0]["title"] == "Sleep Video"


def test_concurrent_writer_and_readers(idx):
    errors = []

    def writer():
        try:
            for i in range(20):
                idx.index_transcript(JEREMY, VideoMeta(id=f"w{i}", title="t", published="2026-01-01"), tr(f"w{i}", ["token alpha"] * 9))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def reader():
        try:
            for _ in range(40):
                idx.search("alpha")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and idx.stats()["videos"] == 20
