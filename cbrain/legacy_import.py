"""Read-only import of transcripts fetched by the old creator-brain project (knowledge.db + markdown archive).

Old schema (see knowledge.db): creators(id, channel_id, handle, name), videos(video_id, creator_id, title,
duration_seconds, language, status, transcript_source, markdown_path), caption_imports(video_id, payload JSON with
ms-precision segments), video_chunks_fts (overlapping chunks, last resort), and per-video markdown files whose body
is `## MM:SS` headings each followed by one caption line.
The source database and archive are never written to.
"""

from __future__ import annotations

import gzip
import json
import re
import sqlite3
from pathlib import Path

from .fetch.runner import save_transcript
from .jobs import JobStore
from .models import SOURCE_RANK, Creator, JobState, Segment, Transcript, VideoMeta
from .paths import VaultPaths

OLD_SOURCE_MAP = {
    "browser_caption": "legacy_import",
    "public_caption": "legacy_import",
    "faster_whisper_small": "local_whisper",
    "faster_whisper": "local_whisper",
    "groq_whisper": "groq_whisper",
}
HEADING_RE = re.compile(r"^##\s+(?:(\d+):)?(\d{1,3}):(\d{2})\s*$")
FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
UNDONE_STATES = (JobState.QUEUED.value, JobState.PARKED.value)


def _open_ro(db: Path) -> sqlite3.Connection:
    uri = "file:" + db.resolve().as_posix().replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _find_creator_row(conn: sqlite3.Connection, creator: Creator):
    handle = creator.handle.lstrip("@").lower()
    return conn.execute(
        "SELECT * FROM creators WHERE channel_id=? OR lower(ltrim(handle,'@'))=? OR lower(name)=?",
        (creator.channel_id, handle, creator.name.lower())).fetchone()


def parse_markdown(text: str, duration: int | None = None) -> list[Segment]:
    """`## MM:SS` heading + body lines -> Segments (end = next heading's start; last uses `duration`)."""
    marks: list[tuple[float, str]] = []
    cur_t: float | None = None
    body: list[str] = []
    text = FRONTMATTER_RE.sub("", text, count=1)
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if cur_t is not None and " ".join(body).strip():
                marks.append((cur_t, " ".join(body).strip()))
            h, mi, s = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
            cur_t, body = float(h * 3600 + mi * 60 + s), []
        elif cur_t is not None and line.strip() and not line.startswith("#"):
            body.append(line.strip())
    if cur_t is not None and " ".join(body).strip():
        marks.append((cur_t, " ".join(body).strip()))
    segs = []
    for i, (t, txt) in enumerate(marks):
        if i + 1 < len(marks):
            end = marks[i + 1][0]
        else:
            end = float(duration) if duration and duration > t else t + 3.0
        segs.append(Segment(start=t, end=max(t, end), text=re.sub(r"\s+", " ", txt)))
    return segs


def _segments_from_payload(payload: str) -> list[Segment]:
    try:
        data = json.loads(payload)
    except ValueError:
        return []
    segs = []
    for s in data.get("segments") or []:
        text = " ".join(str(s.get("text", "")).split())
        if text:
            segs.append(Segment(start=float(s["startMs"]) / 1000, end=float(s["endMs"]) / 1000, text=text))
    return segs


def _markdown_segments(row: sqlite3.Row, archive_root: Path) -> list[Segment]:
    candidates = []
    if row["markdown_path"]:
        candidates.append(Path(row["markdown_path"]))
    candidates += list(archive_root.glob(f"**/videos/{row['video_id']}.md")) if archive_root.exists() else []
    for p in candidates:
        try:
            if p.is_file():
                return parse_markdown(p.read_text(encoding="utf-8-sig"), row["duration_seconds"])
        except OSError:
            continue
    return []


def read_old_transcripts(old_db: Path, creator: Creator):
    """Yield (VideoMeta, Transcript) for every indexed old video of this creator. Read-only."""
    conn = _open_ro(old_db)
    try:
        crow = _find_creator_row(conn, creator)
        if crow is None:
            return
        archive_root = old_db.parent / "creators" / crow["id"]
        imports = {r["video_id"]: r["payload"] for r in conn.execute(
            "SELECT video_id, payload FROM caption_imports ORDER BY rowid")}
        rows = conn.execute("SELECT * FROM videos WHERE creator_id=? AND status='indexed' ORDER BY video_id",
                            (crow["id"],)).fetchall()
        for row in rows:
            segs = _segments_from_payload(imports[row["video_id"]]) if row["video_id"] in imports else []
            if not segs:
                segs = _markdown_segments(row, archive_root)
            if not segs:
                continue
            source = OLD_SOURCE_MAP.get(row["transcript_source"] or "", "legacy_import")
            meta = VideoMeta(id=row["video_id"], title=row["title"], published=(row["published_at"] or None) and row["published_at"][:10],
                             duration=row["duration_seconds"], channel_id=creator.channel_id)
            yield meta, Transcript(video_id=row["video_id"], segments=segs, source=source, language=row["language"])
    finally:
        conn.close()


def import_old_creator_brain(old_db: Path, store: JobStore, paths: VaultPaths, creator: Creator) -> int:
    """Import old transcripts into the vault as FETCHED jobs. Returns the number imported.

    Videos already past the queue (fetched/written/done) are left alone; a queued/parked video is only
    replaced when the old transcript is at least as good as nothing (i.e. always, since it has none yet).
    """
    old_db = Path(old_db)
    if not old_db.exists():
        return 0
    if store.get_creator(creator.slug) is None:
        store.upsert_creator(creator)
    count = 0
    for meta, transcript in read_old_transcripts(old_db, creator):
        row = store.get(meta.id)
        if row is not None and row.state not in UNDONE_STATES:
            continue
        if row is None:
            store.add_videos(creator.slug, [meta], {meta.id: 0.0})
        target = paths.transcript(meta.id)
        if target.exists():  # keep a better transcript already on disk
            try:
                with gzip.open(target, "rt", encoding="utf-8") as f:
                    existing = Transcript.from_dict(json.load(f))
                if SOURCE_RANK.get(existing.source, 0) >= SOURCE_RANK.get(transcript.source, 0):
                    transcript = existing
            except (OSError, ValueError, KeyError):
                pass
        save_transcript(target, transcript)
        store.mark(meta.id, JobState.FETCHED, transcript.source)
        count += 1
    return count
