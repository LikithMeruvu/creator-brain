import json
import re
from pathlib import Path

import pytest

from cbrain.models import Chapter, Claim, Creator, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths
from cbrain.vault.writer import VaultWriter, brain_label, fmt_ts, parse_frontmatter

CREATOR = Creator(slug="jeremy-ethier", name="Jeremy Ethier", handle="@JeremyEthier", channel_id="UC123",
                  scope="recent", last_checked_at="2026-10-29T10:00:00")


def make_transcript(vid="abc123", n=30, step=10.0, chapters=None, removed=None):
    segs = [Segment(start=i * step, end=(i + 1) * step, text=f"segment number {i} about protein.") for i in range(n)]
    return Transcript(video_id=vid, segments=segs, source="auto_caption", chapters=chapters or [],
                      removed_spans=removed or [])


@pytest.fixture
def writer(tmp_path):
    w = VaultWriter(VaultPaths(tmp_path / "vault"))
    w.ensure_vault()
    return w


def all_notes(root: Path):
    return [p for p in root.rglob("*.md") if ".brain" not in p.parts and ".obsidian" not in p.parts]


def check_links(root: Path):
    """Every [[wikilink]] in every note must point at an existing note (or .base) stem."""
    stems = {p.stem for p in all_notes(root)} | {p.name for p in root.glob("*.base")}
    bad = []
    for note in all_notes(root):
        text = note.read_text(encoding="utf-8")
        body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        for target in re.findall(r"\[\[([^\]]+)\]\]", body):
            name = target.split("|")[0]
            if name not in stems:
                bad.append((note.name, name))
    return bad


def test_brain_label():
    counts = {"done": 90, "written": 3, "extracting": 1, "fetched": 2, "queued": 40, "parked": 5, "total": 141}
    assert brain_label(CREATOR, counts, "2026-10-29") == "jeremy-ethier \u00b7 recent \u00b7 updated 2026-10-29 \u00b7 96 videos"


def test_ensure_vault_creates_everything_and_keeps_me(tmp_path):
    paths = VaultPaths(tmp_path / "v")
    w = VaultWriter(paths)
    w.ensure_vault()
    for p in (paths.home, paths.me, paths.agents_md, paths.root / "Creators.base", paths.obsidian / "graph.json"):
        assert p.exists(), p
    assert "search_brain" in paths.agents_md.read_text(encoding="utf-8")
    paths.me.write_text("---\ngoal: \"my own goal\"\n---\nhello", encoding="utf-8")
    w.ensure_vault()
    assert "my own goal" in paths.me.read_text(encoding="utf-8")


def test_agents_md_user_edit_preserved_when_marker_removed(tmp_path):
    paths = VaultPaths(tmp_path / "v")
    w = VaultWriter(paths)
    w.ensure_vault()
    paths.agents_md.write_text("my rules", encoding="utf-8")
    w.ensure_vault()
    assert paths.agents_md.read_text(encoding="utf-8") == "my rules"


def test_creators_base_is_valid_yaml_shape(writer):
    text = (writer.paths.root / "Creators.base").read_text(encoding="utf-8")
    assert "type == \"creator\"" in text and "views:" in text and "- type: table" in text
    assert "\t" not in text


def test_video_note_frontmatter_and_body(writer):
    meta = VideoMeta(id="abc123", title="Best Protein: 5 Myths? #1", published="2026-10-01", duration=300, views=12345)
    tr = make_transcript(removed=[(10.0, 40.0)])
    path = writer.write_video_note(CREATOR, meta, tr, topics=["Protein Intake"], on_screen=[(30.0, "A bar chart")])
    assert path.parent == writer.paths.videos_dir("Jeremy Ethier")
    assert path.stem.startswith("2026-10-01 Best Protein")
    for ch in '<>:"/\\|?*#':
        assert ch not in path.stem
    text = path.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    assert fm["type"] == "video" and fm["id"] == "abc123" and fm["creator"] == "[[Jeremy Ethier]]"
    assert fm["title"] == "Best Protein: 5 Myths? #1" and fm["published"] == "2026-10-01"
    assert fm["views"] == 12345 and fm["duration"] == 300 and fm["removed_sponsor_seconds"] == 30
    assert fm["transcript_source"] == "auto_caption" and fm["tags"] == ["video", "creator/jeremy-ethier"]
    assert 'url: "https://www.youtube.com/watch?v=abc123"' in text
    assert "Topics: [[Protein Intake]]" in text
    assert "## On screen" in text and "A bar chart" in text
    # no chapters -> sections every ~120 s: 300 s transcript -> 3 sections
    headings = re.findall(r"^## \[(\d+:\d\d)\] ", text, re.M)
    assert headings == ["00:00", "02:00", "04:00"]
    # every paragraph ends with a timestamp link carrying &t=
    paragraphs = [p for p in text.split("\n\n") if p.startswith("segment number")]
    assert paragraphs and all(re.search(r"\[\u25b6 \d\d:\d\d\]\(https://www\.youtube\.com/watch\?v=abc123&t=\d+s\)$", p.strip())
                              for p in paragraphs)
    seg_counts = [p.count("segment number") for p in paragraphs]
    assert all(3 <= c <= 7 for c in seg_counts), seg_counts


def test_video_note_chapters(writer):
    meta = VideoMeta(id="vid1", title="Chapters", published="2026-01-01", duration=100)
    tr = make_transcript("vid1", n=12, chapters=[Chapter(0, "Intro"), Chapter(60, "Main Part")])
    text = writer.write_video_note(CREATOR, meta, tr).read_text(encoding="utf-8")
    assert "## [00:00] Intro" in text and "## [01:00] Main Part" in text
    assert text.index("## [01:00] Main Part") < text.index("segment number 6")


def test_video_note_idempotent_and_keeps_topics(writer):
    meta = VideoMeta(id="abc123", title="Same", published="2026-10-01")
    tr = make_transcript()
    p1 = writer.write_video_note(CREATOR, meta, tr, topics=["Creatine"])
    t1 = p1.read_bytes()
    mtime = p1.stat().st_mtime_ns
    p2 = writer.write_video_note(CREATOR, meta, tr)   # topics=None keeps the existing Topics line
    assert p1 == p2 and p2.read_bytes() == t1 and p2.stat().st_mtime_ns == mtime
    assert not list(p1.parent.glob("*.tmp"))
    # a fresh writer instance (new process) resolves the same path
    w2 = VaultWriter(writer.paths)
    assert w2.video_note_path(CREATOR, meta) == p1


def test_video_note_collision_and_unknown_date(writer):
    a = VideoMeta(id="aaa", title="Same Title", published="2026-10-01")
    b = VideoMeta(id="bbb", title="Same Title", published="2026-10-01")
    pa = writer.write_video_note(CREATOR, a, make_transcript("aaa"))
    pb = writer.write_video_note(CREATOR, b, make_transcript("bbb"))
    assert pa != pb and pb.stem == "2026-10-01 Same Title (bbb)"
    assert writer.write_video_note(CREATOR, b, make_transcript("bbb")) == pb
    assert writer.video_note_path(CREATOR, a) == pa
    # unknown date -> 0000-00-00, and renamed once the date is known (old file removed)
    c = VideoMeta(id="ccc", title="No Date")
    pc = writer.write_video_note(CREATOR, c, make_transcript("ccc"))
    assert pc.stem == "0000-00-00 No Date"
    c.published = "2026-02-02"
    pc2 = writer.write_video_note(CREATOR, c, make_transcript("ccc"))
    assert pc2.stem == "2026-02-02 No Date" and not pc.exists()


def test_topic_note_claims_conflicts_and_links(writer):
    m1 = VideoMeta(id="v1", title="Old Video", published="2025-01-01")
    m2 = VideoMeta(id="v2", title="New Video", published="2026-06-01")
    writer.write_video_note(CREATOR, m1, make_transcript("v1"), topics=["Protein Intake"])
    writer.write_video_note(CREATOR, m2, make_transcript("v2"), topics=["Protein Intake"])
    writer.write_creator_note(CREATOR, {"done": 2, "total": 2}, ["Protein Intake"], [])
    old = Claim("jeremy-ethier", "v1", 65, "Eat 1.6 g/kg protein", "eat one point six grams per kilo", "Protein Intake", date="2025-01-01")
    new = Claim("jeremy-ethier", "v2", 125, "Eat 2.2 g/kg protein", "two point two grams per kilo is best", "Protein Intake", date="2026-06-01")
    conflicts = [{"creator": "jeremy-ethier", "topic": "Protein Intake", "old": old.to_dict(), "new": new.to_dict(),
                  "summary": "Raised target from 1.6 to 2.2"}]
    path = writer.write_topic_note("Protein Intake", [old, new], ["protein", "Protein Target"], conflicts)
    text = path.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    assert fm["type"] == "topic" and fm["aliases"] == ["protein", "Protein Target"]
    assert fm["creators"] == ["[[Jeremy Ethier]]"]
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert "Eat 2.2" in lines[0] and "[[2026-06-01 New Video|2026-06-01]]" in lines[0]   # newest first
    assert lines[1].startswith("- ~~Eat 1.6 g/kg protein") and "(changed 2026-06-01)" in lines[1]
    assert "[\u25b6 01:05](https://www.youtube.com/watch?v=v1&t=65s)" in lines[1]
    assert "## Where creators agree / disagree" in text and "Raised target from 1.6 to 2.2" in text
    assert check_links(writer.paths.root) == []
    t1 = path.read_bytes()
    assert writer.write_topic_note("Protein Intake", [old, new], ["protein", "Protein Target"], conflicts).read_bytes() == t1


def test_update_note_append_and_idempotent(writer):
    m1 = VideoMeta(id="v1", title="First", published="2026-10-28", views=1000)
    m2 = VideoMeta(id="v2", title="Second", published="2026-10-29", views=50)
    for m in (m1, m2):
        writer.write_video_note(CREATOR, m, make_transcript(m.id))
    claims = [Claim("jeremy-ethier", "v1", 10 * i, f"claim number {i}", f"quote number {i} here", "Topic A", date="2026-10-28")
              for i in range(12)]
    ch = [{"creator": "jeremy-ethier", "topic": "Topic A", "summary": "Now says X",
           "old": claims[0].to_dict(), "new": claims[1].to_dict()}]
    p = writer.write_update_note(CREATOR, "2026-10-29", [m1], claims, ch)
    assert p == writer.paths.updates_dir("Jeremy Ethier") / "Jeremy Ethier - Update 2026-10-29.md"
    fm = parse_frontmatter(p.read_text(encoding="utf-8"))
    assert fm["type"] == "update" and fm["new_videos"] == 1 and fm["new_claims"] == 12 and fm["changed_advice"] == 1
    text = p.read_text(encoding="utf-8")
    assert text.count("claim number") == 10 and "Top 10 of 12" in text and "\u26a0\ufe0f" in text
    first = p.read_bytes()
    writer.write_update_note(CREATOR, "2026-10-29", [m1], claims, ch)           # same input: unchanged
    assert p.read_bytes() == first
    writer.write_update_note(CREATOR, "2026-10-29", [m2], [], [])               # same day: appended
    fm = parse_frontmatter(p.read_text(encoding="utf-8"))
    assert fm["new_videos"] == 2 and fm["new_claims"] == 12
    assert "[[2026-10-28 First|First]]" in p.read_text(encoding="utf-8") and "Second" in p.read_text(encoding="utf-8")


def test_creator_note_and_home(writer):
    for i, views in enumerate((10, 5000, 300)):
        m = VideoMeta(id=f"id{i}", title=f"Video {i}", published=f"2026-10-0{i + 1}", views=views)
        writer.write_video_note(CREATOR, m, make_transcript(m.id))
    writer.write_update_note(CREATOR, "2026-10-29", [], [], [])
    counts = {"done": 1, "written": 1, "fetched": 1, "parked": 1, "total": 4}
    p = writer.write_creator_note(CREATOR, counts, ["Protein Intake"], ["2026-10-29"])
    text = p.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    assert fm["type"] == "creator" and fm["slug"] == "jeremy-ethier" and fm["built_percent"] == 50
    assert fm["brain"] == "jeremy-ethier \u00b7 recent \u00b7 updated 2026-10-29 \u00b7 3 videos"
    assert "[[Jeremy Ethier - Update 2026-10-29]]" in text
    order = [text.index(f"Video {i}") for i in (1, 2, 0)]
    assert order == sorted(order)            # by views: 5000, 300, 10
    writer.write_topic_note("Protein Intake", [], [], [])
    home = writer.write_home([CREATOR], {CREATOR.slug: counts}, ["Protein Intake"]).read_text(encoding="utf-8")
    assert "| [[Jeremy Ethier]] |" in home and "50%" in home and "| 1 |" in home and "[[Protein Intake]]" in home
    assert check_links(writer.paths.root) == []


def test_write_output(writer):
    p = writer.write_output("My Plan", "# Plan\n\nDo this.", ["Jeremy Ethier", "https://youtu.be/x?t=3"])
    text = p.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    assert fm["type"] == "output" and fm["sources"] == ["[[Jeremy Ethier]]", "https://youtu.be/x?t=3"]
    assert p.parent == writer.paths.outputs and "## Sources" in text


def test_frontmatter_quotes_hostile_strings(writer):
    meta = VideoMeta(id="q1", title='He said "hi": a\\b - [[x]]', published="2026-01-01")
    p = writer.write_video_note(CREATOR, meta, make_transcript("q1"))
    fm = parse_frontmatter(p.read_text(encoding="utf-8"))
    assert fm["title"] == 'He said "hi": a\\b - [ [x] ]'


def test_fmt_ts():
    assert fmt_ts(0) == "00:00" and fmt_ts(125.9) == "02:05" and fmt_ts(3725) == "1:02:05"
