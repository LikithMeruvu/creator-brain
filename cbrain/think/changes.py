"""Change detection: did a creator later contradict what they said earlier on the same topic?"""

from __future__ import annotations

import logging
import re

from ..models import Claim
from .llm import LLMError, NoLLM

log = logging.getLogger("cbrain.think")

MAX_PAIRS_PER_TOPIC = 20
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are", "it", "that", "this", "you",
         "your", "be", "with", "as", "at", "by", "not", "can", "should", "do", "have", "has", "was", "were", "they",
         "i", "we", "if", "so", "but", "from", "will", "just", "more", "than", "about"}

SYSTEM_PROMPT = (
    "You compare what ONE creator said at two different dates about the same topic. You output JSON only.\n"
    "For each numbered pair decide whether the NEWER statement contradicts, reverses or materially changes the "
    "OLDER one (for example a different number, a recommendation that flipped, advice that was withdrawn).\n"
    "Extra detail, a different angle, or a statement about something else is NOT a contradiction. "
    "Use only the text given; if unsure, answer false.\n"
    'Output: {"results": [{"i": 0, "contradicts": true, "summary": "one short plain sentence naming what changed"}]} '
    "with one entry per pair."
)


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 2}


def candidate_pairs(claims: list[Claim], focus_ids: set[str] | None = None,
                    cap: int = MAX_PAIRS_PER_TOPIC) -> list[tuple[Claim, Claim]]:
    """(older, newer) pairs per creator: different dates, different videos, some shared wording. Best overlap first."""
    by_creator: dict[str, list[Claim]] = {}
    for c in claims:
        if c.date:
            by_creator.setdefault(c.creator, []).append(c)
    scored: list[tuple[float, str, str, Claim, Claim]] = []
    for group in by_creator.values():
        group.sort(key=lambda c: (c.date or "", c.video_id, c.t))
        words = {c.id: _content_words(c.claim + " " + c.quote) for c in group}
        for i, old in enumerate(group):
            for new in group[i + 1:]:
                if new.date == old.date or new.video_id == old.video_id:
                    continue
                if focus_ids is not None and old.id not in focus_ids and new.id not in focus_ids:
                    continue
                a, b = words[old.id], words[new.id]
                shared = len(a & b)
                if not shared:
                    continue
                scored.append((shared / len(a | b), new.date or "", old.id + new.id, old, new))
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))
    return [(o, n) for _, _, _, o, n in scored[:cap]]


def _brief(c: Claim) -> dict:
    return {"date": c.date, "claim": c.claim, "quote": c.quote}


def detect_changes(topic: str, claims: list[Claim], llm, focus_ids: set[str] | None = None) -> list[dict]:
    """One batched LLM call for the topic. Returns [{"creator","topic","old","new","summary"}]. No LLM -> []."""
    if llm is None or not llm.available():
        return []
    pairs = candidate_pairs(claims, focus_ids)
    if not pairs:
        return []
    import json
    user = (f"Topic: {topic}\n\nPairs:\n" + json.dumps(
        [{"i": i, "older": _brief(o), "newer": _brief(n)} for i, (o, n) in enumerate(pairs)], ensure_ascii=False)
        + '\n\nReturn {"results": [...]} now.')
    try:
        payload = llm.complete_json(SYSTEM_PROMPT, user, max_tokens=3000)
    except (NoLLM, LLMError) as e:
        log.warning("change detection skipped for %r: %s", topic, e)
        return []
    results = payload.get("results") if isinstance(payload, dict) else payload
    out: list[dict] = []
    seen: set[int] = set()
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict) or r.get("contradicts") is not True:
            continue
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(pairs) or i in seen:
            continue
        seen.add(i)
        old, new = pairs[i]
        summary = " ".join(str(r.get("summary") or "").split()) or "The newer video disagrees with the older one."
        out.append({"creator": new.creator, "topic": topic, "old": old.to_dict(), "new": new.to_dict(),
                    "summary": summary})
    return out
