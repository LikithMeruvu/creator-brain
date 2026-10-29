"""Claim extraction: windowed transcript -> LLM -> candidate Claims (unverified), plus the no-Groq agent-batch path."""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..models import Claim, Creator, Transcript, VideoMeta
from ..paths import VaultPaths
from .llm import WINDOW_CHARS, LLMClient
from .topics import clean_topic
from .verify import normalize

log = logging.getLogger("cbrain.think")

PARALLEL_WINDOWS = 4
MAX_CLAIMS_PER_WINDOW = 12
CLAIM_KINDS = ("advice", "fact", "protocol", "warning", "opinion")
_LIST_KEYS = ("claims", "items", "cards", "results", "data")
_MARKER = re.compile(r"\[\s*(?:\d+:)?\d{1,3}:\d{2}\s*\]")

RULES = """\
Extract only practical, specific, useful items a viewer could act on or cite: advice, facts, protocols (concrete \
numbers, sets, reps, doses, durations, steps), warnings, and clearly stated opinions of the speaker.

RULES
1. quote: copy 6 to 40 consecutive words EXACTLY as they appear in the transcript. Same words, same order. Do not \
paraphrase, fix grammar, add or drop words, use "..." or "[...]", or include the [mm:ss] markers. The quote may \
continue onto the next line; just leave out the marker.
2. claim: ONE plain sentence saying what the speaker says. Use only what the quote supports. Do not add numbers, \
advice or knowledge that is not in the quote, and do not turn an opinion into a fact.
3. t: the mm:ss of the marker at the start of the line where the quote begins.
4. topic: a short Title Case noun phrase of 1 to 4 words (example: "Protein Intake"). If one of the KNOWN TOPICS \
fits, use it exactly; otherwise invent a new short one.
5. kind: one of advice, fact, protocol, warning, opinion.
6. SKIP: sponsor reads and discount codes, greetings, intros and outros, calls to subscribe/like/comment, channel \
promotion, jokes, filler, vague motivation, and anything you cannot quote exactly.
7. Quality over quantity. Returning an empty list is fine. At most 12 items. Never repeat the same idea."""

SYSTEM_PROMPT = (
    "You turn a video transcript into verified knowledge cards. You output JSON only.\n\n" + RULES + "\n\n"
    'OUTPUT FORMAT (a JSON object): {"claims": [{"claim": "...", "quote": "...", "t": "mm:ss", '
    '"topic": "...", "kind": "advice"}]}'
)


@dataclass
class Window:
    index: int
    start: float   # seconds of the first segment
    text: str      # lines of "[mm:ss] words"


def fmt_ts(seconds: float) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    return f"{m:02d}:{s:02d}"


def parse_timestamp(value: object) -> float | None:
    """"mm:ss", "h:mm:ss", "[mm:ss]" or a number of seconds -> seconds; None if unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    parts = value.strip().strip("[]() ").split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if not 1 <= len(nums) <= 3 or any(n < 0 for n in nums):
        return None
    secs = 0.0
    for n in nums:
        secs = secs * 60 + n
    return secs


def make_windows(transcript: Transcript, target_chars: int = WINDOW_CHARS) -> list[Window]:
    windows: list[Window] = []
    lines: list[str] = []
    size, start = 0, 0.0
    for seg in transcript.segments:
        text = " ".join(seg.text.split())
        if not text:
            continue
        line = f"[{fmt_ts(seg.start)}] {text}"
        if lines and size + len(line) > target_chars:
            windows.append(Window(len(windows), start, "\n".join(lines)))
            lines, size = [], 0
        if not lines:
            start = seg.start
        lines.append(line)
        size += len(line) + 1
    if lines:
        windows.append(Window(len(windows), start, "\n".join(lines)))
    return windows


def build_user_prompt(creator: Creator, meta: VideoMeta, window: Window, total: int, known_topics: list[str]) -> str:
    known = "; ".join(known_topics[:150]) if known_topics else "(none yet)"
    return (
        f"Creator: {creator.name or creator.slug}\nVideo title: {meta.title}\n"
        f"Part {window.index + 1} of {total} of the transcript.\n"
        f"KNOWN TOPICS: {known}\n\n"
        "TRANSCRIPT WINDOW (each line starts with its [mm:ss] marker):\n"
        f"{window.text}\n\n"
        'Return the JSON object {"claims": [...]} now.'
    )


def _items(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            if isinstance(payload.get(key), list):
                return payload[key]
        if "claim" in payload and "quote" in payload:
            return [payload]
    return []


def parse_claims_payload(payload: object, creator_slug: str, video_id: str, date: str | None,
                         default_t: float = 0.0) -> list[Claim]:
    """Defensive parse of model/agent output into unverified Claims. Bad items are skipped."""
    out: list[Claim] = []
    for item in _items(payload):
        if not isinstance(item, dict):
            continue
        claim = " ".join(str(item.get("claim") or "").split())
        quote = " ".join(_MARKER.sub(" ", str(item.get("quote") or "")).split()).strip("\"“” ")
        if not claim or not quote:
            continue
        t = parse_timestamp(item.get("t"))
        kind = str(item.get("kind") or "advice").strip().lower()
        out.append(Claim(creator=creator_slug, video_id=video_id, t=default_t if t is None else t, claim=claim,
                         quote=quote, topic=clean_topic(item.get("topic")),
                         kind=kind if kind in CLAIM_KINDS else "advice", date=date, verified=False))
    return out


def dedupe_by_quote(claims: list[Claim]) -> list[Claim]:
    seen: set[str] = set()
    out = []
    for c in claims:
        key = normalize(c.quote)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def extract_claims(creator: Creator, meta: VideoMeta, transcript: Transcript, llm: LLMClient,
                   known_topics: list[str] | None = None) -> list[Claim]:
    """Unverified candidate claims for a whole video. Raises if any window fails (so the job is retried)."""
    windows = make_windows(transcript)
    topics = known_topics or []

    def one(w: Window) -> list[Claim]:
        payload = llm.complete_json(SYSTEM_PROMPT, build_user_prompt(creator, meta, w, len(windows), topics),
                                    max_tokens=6000)
        return parse_claims_payload(payload, creator.slug, meta.id, meta.published, w.start)[:MAX_CLAIMS_PER_WINDOW]

    if not windows:
        return []
    with ThreadPoolExecutor(max_workers=min(PARALLEL_WINDOWS, len(windows))) as pool:
        futures = [pool.submit(one, w) for w in windows]
        results = [f.result() for f in futures]  # raises the first failure, in window order
    return dedupe_by_quote([c for r in results for c in r])


# ---- no-Groq fallback: a harness AI agent does the extraction -------------------------------------------------

def agent_batch_path(paths: VaultPaths, video_id: str) -> Path:
    return paths.pending_extraction / f"{video_id}.md"


def agent_claims_path(paths: VaultPaths, video_id: str) -> Path:
    return paths.pending_extraction / f"{video_id}.claims.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def write_agent_batch(paths: VaultPaths, creator: Creator, meta: VideoMeta, transcript: Transcript,
                      known_topics: list[str] | None = None) -> Path:
    """Write .brain/pending_extraction/<id>.md: instructions + windowed transcript for the harness agent."""
    windows = make_windows(transcript)
    out_name = agent_claims_path(paths, meta.id).name
    front = {"type": "extraction_batch", "video_id": meta.id, "creator": creator.slug,
             "creator_name": creator.name, "title": meta.title, "published": meta.published or "",
             "url": meta.url, "write_claims_to": out_name}
    head = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()) + "\n---\n\n"
    known = "; ".join(known_topics or []) or "(none yet)"
    body = (
        f"# Extraction task: {meta.title}\n\n"
        f"You are the extraction step of Creator Brain. Read the transcript of this video by {creator.name or creator.slug} "
        f"below and write knowledge cards for it.\n\n{RULES}\n\n"
        f"KNOWN TOPICS: {known}\n\n"
        "## What to write\n\n"
        f"Create the file `{out_name}` in the SAME folder as this file, containing ONLY this JSON "
        "(no markdown fences):\n\n"
        "```\n"
        '{"video_id": "' + meta.id + '", "claims": [\n'
        '  {"claim": "one plain sentence", "quote": "6-40 words copied exactly", "t": "mm:ss", '
        '"topic": "Title Case Topic", "kind": "advice"}\n'
        "]}\n"
        "```\n\n"
        "Every quote is checked automatically against the real transcript; any quote that is not an exact copy is "
        "discarded, so copy-paste carefully. Do not edit this file.\n\n"
        f"## Transcript ({len(windows)} parts)\n\n"
    )
    parts = [f"### Part {w.index + 1} of {len(windows)}\n\n{w.text}\n" for w in windows]
    path = agent_batch_path(paths, meta.id)
    _atomic_write(path, head + body + "\n".join(parts))
    return path


def _read_front(md: Path) -> dict:
    out: dict = {}
    if not md.exists():
        return out
    lines = md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, val = line.partition(":")
        val = val.strip()
        try:
            out[key.strip()] = json.loads(val)
        except ValueError:
            out[key.strip()] = val.strip("\"'")
    return out


def ingest_agent_batch(paths: VaultPaths, file: Path) -> list[Claim]:
    """Read the agent's <id>.claims.json (or pass the batch .md) and return UNVERIFIED Claims. Raises ValueError
    on a malformed file."""
    file = Path(file)
    name = file.name
    if name.endswith(".claims.json"):
        vid, claims_file = name[: -len(".claims.json")], file
    else:
        vid = file.stem
        claims_file = file.with_name(f"{vid}.claims.json")
    if not claims_file.exists():
        raise FileNotFoundError(f"agent has not written {claims_file.name} yet")
    try:
        payload = json.loads(claims_file.read_text(encoding="utf-8-sig"))
    except ValueError as e:
        raise ValueError(f"{claims_file.name} is not valid JSON: {e}") from e
    front = _read_front(claims_file.with_name(f"{vid}.md"))
    top = payload if isinstance(payload, dict) else {}
    creator = front.get("creator") or top.get("creator") or ""  # the pipeline overrides it from the job row
    date = front.get("published") or top.get("date") or None
    if not (isinstance(payload, list) or "claim" in top or any(isinstance(top.get(k), list) for k in _LIST_KEYS)):
        raise ValueError(f"{claims_file.name} has no claims list")
    return parse_claims_payload(payload, str(creator), vid, date)


def archive_agent_files(paths: VaultPaths, video_id: str) -> None:
    """Move a finished batch + its answer to pending_extraction/ingested/ so they are not offered again."""
    dest = paths.pending_extraction / "ingested"
    dest.mkdir(parents=True, exist_ok=True)
    for p in (agent_batch_path(paths, video_id), agent_claims_path(paths, video_id)):
        if p.exists():
            os.replace(p, dest / p.name)
