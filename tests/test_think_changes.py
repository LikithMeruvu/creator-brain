from cbrain.models import Claim
from cbrain.think.changes import MAX_PAIRS_PER_TOPIC, candidate_pairs, detect_changes


def mk(creator, vid, date, text, t=1.0):
    return Claim(creator=creator, video_id=vid, t=t, claim=text, quote=text + " quote words here", topic="Protein Intake",
                 date=date)


class FakeLLM:
    def __init__(self, results=None, available=True, error=None):
        self.results, self._available, self.error = results, available, error
        self.calls = []

    def available(self):
        return self._available

    def complete_json(self, system, user, max_tokens=4000):
        self.calls.append(user)
        if self.error:
            raise self.error
        return self.results


def test_no_llm_returns_empty():
    cs = [mk("a", "v1", "2024-01-01", "eat protein daily"), mk("a", "v2", "2025-01-01", "eat protein daily more")]
    assert detect_changes("Protein Intake", cs, None) == []
    assert detect_changes("Protein Intake", cs, FakeLLM(available=False)) == []


def test_pairs_same_creator_different_dates_only():
    cs = [
        mk("a", "v1", "2024-01-01", "eat one gram protein per pound"),
        mk("a", "v2", "2025-01-01", "eat point seven grams protein per pound"),
        mk("a", "v3", "2025-01-01", "protein per pound is plenty"),  # same date as v2 -> never paired with v2
        mk("b", "v4", "2025-06-01", "eat one gram protein per pound"),  # other creator
        mk("a", "v5", None, "eat protein"),  # undated
    ]
    pairs = candidate_pairs(cs)
    keys = {(o.video_id, n.video_id) for o, n in pairs}
    assert ("v1", "v2") in keys and ("v1", "v3") in keys and ("v2", "v3") not in keys
    assert all(o.creator == n.creator == "a" and o.date < n.date for o, n in pairs)


def test_one_batched_call_and_conflict_shape():
    old = mk("a", "v1", "2024-01-01", "eat one gram protein per pound")
    new = mk("a", "v2", "2025-01-01", "eat point seven grams protein per pound")
    llm = FakeLLM({"results": [{"i": 0, "contradicts": True, "summary": "Lowered the target."}]})
    out = detect_changes("Protein Intake", [old, new], llm)
    assert len(llm.calls) == 1
    assert out == [{"creator": "a", "topic": "Protein Intake", "old": old.to_dict(), "new": new.to_dict(),
                    "summary": "Lowered the target."}]


def test_non_contradictions_and_garbage_ignored():
    old = mk("a", "v1", "2024-01-01", "eat one gram protein per pound")
    new = mk("a", "v2", "2025-01-01", "eat point seven grams protein per pound")
    for result in ({"results": [{"i": 0, "contradicts": False}]}, {"results": [{"i": 7, "contradicts": True}]},
                   {"results": "nope"}, [], "x", {"results": [None, {"i": "z", "contradicts": True}]}):
        assert detect_changes("T", [old, new], FakeLLM(result)) == []


def test_llm_error_is_swallowed():
    from cbrain.think.llm import LLMError
    old = mk("a", "v1", "2024-01-01", "eat one gram protein per pound")
    new = mk("a", "v2", "2025-01-01", "eat point seven grams protein per pound")
    assert detect_changes("T", [old, new], FakeLLM(error=LLMError("down"))) == []


def test_pairs_capped():
    cs = [mk("a", f"v{i}", f"2024-{i % 12 + 1:02d}-{i // 12 + 1:02d}", f"eat protein amount {i}") for i in range(30)]
    assert len(candidate_pairs(cs)) == MAX_PAIRS_PER_TOPIC


def test_focus_ids_limit_pairs():
    a = mk("a", "v1", "2024-01-01", "eat protein daily")
    b = mk("a", "v2", "2024-06-01", "eat protein daily")
    c = mk("a", "v3", "2025-01-01", "eat protein daily")
    pairs = candidate_pairs([a, b, c], focus_ids={c.id})
    assert {(o.video_id, n.video_id) for o, n in pairs} == {("v1", "v3"), ("v2", "v3")}
