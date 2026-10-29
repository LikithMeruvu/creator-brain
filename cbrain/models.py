"""Shared data models. Every module imports these; change them only in this file."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Literal

TranscriptSource = Literal["manual_caption", "auto_caption", "groq_whisper", "local_whisper", "legacy_import"]
Scope = Literal["recent", "full"]
ClaimKind = Literal["advice", "fact", "protocol", "warning", "opinion"]

# Higher is better when choosing between two transcripts of the same video.
SOURCE_RANK: dict[str, int] = {
    "manual_caption": 5,
    "groq_whisper": 4,
    "local_whisper": 4,
    "legacy_import": 2,
    "auto_caption": 1,
}


class JobState(str, Enum):
    QUEUED = "queued"        # waiting for a transcript
    FETCHING = "fetching"    # a lane holds a lease on it
    FETCHED = "fetched"      # transcript saved in .brain/transcripts
    WRITTEN = "written"      # video note + search index written
    EXTRACTING = "extracting"
    DONE = "done"            # claims extracted, verified, topics updated
    PARKED = "parked"        # gave up after max attempts; see last_error


def slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lstrip("@")).strip("-").lower()
    return text or "creator"


@dataclass
class Creator:
    slug: str                 # "jeremy-ethier" — the brain name
    name: str                 # "Jeremy Ethier"
    handle: str               # "@JeremyEthier"
    channel_id: str           # "UC..." — stable even if the channel is renamed
    scope: Scope = "recent"
    added_at: str = ""        # ISO timestamp
    last_checked_at: str = "" # ISO timestamp of last new-video check


@dataclass
class VideoMeta:
    id: str
    title: str
    published: str | None = None   # "YYYY-MM-DD"
    duration: int | None = None    # seconds
    views: int | None = None
    channel_id: str = ""
    is_short: bool = False
    is_live: bool = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Chapter:
    start: float
    title: str


@dataclass
class Transcript:
    video_id: str
    segments: list[Segment]
    source: TranscriptSource
    language: str | None = None
    chapters: list[Chapter] = field(default_factory=list)
    removed_spans: list[tuple[float, float]] = field(default_factory=list)  # sponsor/self-promo cut out

    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        return cls(
            video_id=d["video_id"],
            segments=[Segment(**s) for s in d["segments"]],
            source=d["source"],
            language=d.get("language"),
            chapters=[Chapter(**c) for c in d.get("chapters", [])],
            removed_spans=[tuple(x) for x in d.get("removed_spans", [])],
        )


@dataclass
class Claim:
    creator: str          # creator slug
    video_id: str
    t: float              # seconds where the quote starts
    claim: str            # one-sentence advice in plain words
    quote: str            # exact words from the transcript
    topic: str            # topic title, e.g. "Protein Intake"
    kind: ClaimKind = "advice"
    date: str | None = None  # video publish date
    verified: bool = False

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.video_id}|{round(self.t)}|{self.claim}".encode()).hexdigest()[:16]

    @property
    def url(self) -> str:
        return f"https://youtu.be/{self.video_id}?t={int(self.t)}"

    def to_dict(self) -> dict:
        return {**asdict(self), "id": self.id, "url": self.url}

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        keys = {"creator", "video_id", "t", "claim", "quote", "topic", "kind", "date", "verified"}
        return cls(**{k: v for k, v in d.items() if k in keys})
