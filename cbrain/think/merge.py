"""Topic consolidation: fold many narrow topics ("Walking Fat Loss", "Walking Habits") into broader ones.

The LLM only proposes groups of existing names; code checks every name and rewrites claims, topic notes,
video-note topic links and the search index. Run while the worker is paused.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
from collections import Counter, defaultdict

from ..models import Claim, Transcript
from ..paths import VaultPaths
from .topics import TopicRegistry, clean_topic

log = logging.getLogger("cbrain.think")

CHUNK = 220  # topic names per request

SYSTEM_PROMPT = "You organise the topic list of a knowledge base built from a YouTube creator's videos. JSON only."
USER_PROMPT = """Group these topic names so a reader finds related advice in one place.

RULES
1. Put names together when they are the same subject, or a narrow sub-case of one broader subject
   (e.g. "Walking Fat Loss", "Walking Habits", "Step Count Goals" -> "Walking").
2. The group topic is a short Title Case noun phrase of 1 to 4 words. Prefer an existing name when it fits.
3. Aim for groups a coach would file notes under: e.g. Protein Intake, Training Volume, Fat Loss, Sleep.
4. Do not force unrelated names together; a group of one is fine.
5. Every input name must appear in exactly one group, spelled exactly as given.

OUTPUT: {{"groups": [{{"topic": "Walking", "members": ["Walking Fat Loss", "Walking Habits"]}}]}}

TOPIC NAMES (with how many claims each has):
{names}"""


def propose_groups(topics: dict[str, int], llm) -> dict[str, list[str]]:
    """{canonical: [member names]} covering every topic; names the model invents or drops stay on their own."""
    names = sorted(topics, key=lambda t: -topics[t])
    groups: dict[str, list[str]] = {}
    for i in range(0, len(names), CHUNK):
        part = names[i:i + CHUNK]
        listing = "\n".join(f"- {n} ({topics[n]})" for n in part)
        payload = llm.complete_json(SYSTEM_PROMPT, USER_PROMPT.format(names=listing), max_tokens=8000)
        _absorb(groups, payload, set(part))
    if len(names) > CHUNK and len(groups) > 1:  # second pass joins groups that landed in different chunks
        counts = {g: sum(topics.get(m, 0) for m in ms) for g, ms in groups.items()}
        merged: dict[str, list[str]] = {}
        payload = llm.complete_json(SYSTEM_PROMPT, USER_PROMPT.format(
            names="\n".join(f"- {g} ({counts[g]})" for g in sorted(counts, key=lambda g: -counts[g]))), max_tokens=8000)
        _absorb(merged, payload, set(groups))
        groups = {top: sorted({m for g in subs for m in groups[g]} | ({top} if top in topics else set()))
                  for top, subs in merged.items()}
    return groups


def _absorb(groups: dict[str, list[str]], payload, allowed: set[str]) -> None:
    seen: set[str] = set()
    if isinstance(payload, list):           # some models answer with the bare list
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("groups") or next((v for v in payload.values() if isinstance(v, list)), [])
    else:
        items = []
    for g in items:
        if not isinstance(g, dict):
            continue
        topic = clean_topic(str(g.get("topic") or ""))
        members = [m for m in (g.get("members") or []) if isinstance(m, str) and m in allowed and m not in seen]
        if not topic or not members:
            continue
        seen.update(members)
        groups.setdefault(topic, []).extend(members)
    for name in allowed - seen:  # anything the model skipped keeps its own topic
        groups.setdefault(name, []).append(name)


def apply_groups(paths: VaultPaths, store, writer, index, groups: dict[str, list[str]]) -> dict:
    registry = TopicRegistry(paths)
    real = {canon: ms for canon, ms in groups.items() if any(m != canon for m in ms)}
    moved = registry.merge(real)
    if not moved:
        return {"merged_topics": 0, "topics_now": len(registry.all_topics())}

    claims = _read_claims(paths)
    changed_videos: set[str] = set()
    for c in claims:
        new = moved.get(c.get("topic"))
        if new:
            c["topic"] = new
            changed_videos.add(c["video_id"])
    _write_claims(paths, claims)

    conflicts = _load(paths.conflicts)
    if isinstance(conflicts, dict) and conflicts:
        merged_conf: dict[str, list] = defaultdict(list)
        for t, items in conflicts.items():
            merged_conf[moved.get(t, t)].extend(items)
        conflicts = dict(merged_conf)
        tmp = paths.conflicts.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(conflicts, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, paths.conflicts)

    by_topic: dict[str, list[Claim]] = defaultdict(list)
    for c in claims:
        by_topic[c["topic"]].append(Claim.from_dict(c))
    for old in moved:
        old_note = paths.topic_note(old)
        if old not in by_topic and old_note.exists():
            old_note.unlink()  # old name lives on as an alias of the new note, so [[old]] links still resolve
    for topic in sorted(set(moved.values())):
        group = sorted(by_topic.get(topic, []), key=lambda c: (c.date or "", c.creator, c.video_id, c.t), reverse=True)
        writer.write_topic_note(topic, group, registry.aliases(topic),
                                conflicts.get(topic, []) if isinstance(conflicts, dict) else [])
    if index is not None:
        index.index_claims([Claim.from_dict(c) for c in claims if c["video_id"] in changed_videos])

    topics_of: dict[str, set[str]] = defaultdict(set)
    for c in claims:
        if c["video_id"] in changed_videos:
            topics_of[c["video_id"]].add(c["topic"])
    relinked = 0
    for vid, ts in topics_of.items():
        row = store.get(vid)
        creator = store.get_creator(row.creator_slug) if row else None
        tpath = paths.transcript(vid)
        if not (row and creator and tpath.exists()):
            continue
        with gzip.open(tpath, "rt", encoding="utf-8") as f:
            transcript = Transcript.from_dict(json.load(f))
        writer.write_video_note(creator, row.meta(), transcript, topics=sorted(ts))
        relinked += 1
    return {"merged_topics": len(moved), "topics_now": len(registry.all_topics()), "videos_relinked": relinked}


def consolidate(paths: VaultPaths, store, writer, index, llm, dry_run: bool = False) -> dict:
    counts = Counter(c.get("topic") for c in _read_claims(paths))
    counts.pop(None, None)
    if len(counts) < 2:
        return {"merged_topics": 0, "topics_now": len(counts), "message": "Nothing to merge."}
    groups = propose_groups(dict(counts), llm)
    preview = {g: ms for g, ms in groups.items() if any(m != g for m in ms)}
    if dry_run:
        return {"topics_before": len(counts), "groups": preview, "topics_after": len(groups)}
    res = apply_groups(paths, store, writer, index, groups)
    res["topics_before"] = len(counts)
    res["message"] = f"Merged {res['merged_topics']} topic names; {res['topics_before']} topics became {res['topics_now']}."
    return res


def _read_claims(paths: VaultPaths) -> list[dict]:
    if not paths.claims.exists():
        return []
    with open(paths.claims, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_claims(paths: VaultPaths, claims: list[dict]) -> None:
    tmp = paths.claims.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for c in claims:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.replace(tmp, paths.claims)


def _load(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
