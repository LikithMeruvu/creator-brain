"""SponsorBlock: cut sponsor / self-promo / interaction spans out of a transcript."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace

from ..models import Transcript

API = "https://sponsor.ajay.app/api/skipSegments"
CATEGORIES = ["sponsor", "selfpromo", "interaction"]


def sponsor_spans(video_id: str, timeout: float = 8) -> list[tuple[float, float]]:
    """Merged (start, end) spans in seconds; [] on 404 or any error. Direct connection, no credentials."""
    query = urllib.parse.urlencode({"videoID": video_id, "categories": json.dumps(CATEGORIES)})
    req = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": "creator-brain/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    spans = []
    for item in data if isinstance(data, list) else []:
        try:
            if item.get("actionType", "skip") not in ("skip", "mute"):
                continue
            a, b = item["segment"]
            if float(b) > float(a) >= 0:
                spans.append((float(a), float(b)))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return merge(spans)


def merge(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def cut(transcript: Transcript, spans: list[tuple[float, float]], threshold: float = 0.5) -> Transcript:
    """Drop segments with more than `threshold` of their length inside a span; record the spans."""
    spans = merge([(float(a), float(b)) for a, b in spans])
    if not spans:
        return transcript

    def inside(seg) -> float:
        length = max(seg.end - seg.start, 0.001)
        covered = sum(max(0.0, min(seg.end, b) - max(seg.start, a)) for a, b in spans)
        return covered / length

    kept = [s for s in transcript.segments if inside(s) <= threshold]
    removed = [s for s in transcript.segments if inside(s) > threshold]
    if not removed:
        return transcript
    return replace(transcript, segments=kept,
                   removed_spans=merge([*transcript.removed_spans, *spans]))
