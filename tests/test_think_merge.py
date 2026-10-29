import gzip
import json

from cbrain.jobs import JobStore
from cbrain.models import Claim, Creator, Segment, Transcript, VideoMeta
from cbrain.paths import VaultPaths
from cbrain.think.merge import consolidate
from cbrain.think.topics import TopicRegistry
from cbrain.vault.writer import VaultWriter


class FakeLLM:
    def available(self):
        return True

    def complete_json(self, system, user, max_tokens=4000):
        return {"groups": [{"topic": "Walking", "members": ["Walking Fat Loss", "Walking Habits", "Made Up Name"]},
                           {"topic": "Protein Intake", "members": ["Protein Intake"]}]}


def _setup(tmp_path):
    paths = VaultPaths(tmp_path)
    paths.ensure_dirs()
    store = JobStore(paths.jobs_db)
    cr = Creator("jeremy-ethier", "Jeremy Ethier", "@JeremyEthier", "UC1")
    store.upsert_creator(cr)
    store.add_videos(cr.slug, [VideoMeta("v1", "Walk More", published="2026-01-02")], {"v1": 1.0})
    store.mark("v1", "done")
    t = Transcript("v1", [Segment(0, 5, "walking ten thousand steps a day helps you lose fat steadily")], "auto_caption")
    with gzip.open(paths.transcript("v1"), "wt", encoding="utf-8") as f:
        json.dump(t.to_dict(), f)
    writer = VaultWriter(paths)
    writer.ensure_vault()
    claims = [Claim(cr.slug, "v1", 0, "Walk 10k steps", "walking ten thousand steps a day", "Walking Fat Loss",
                    date="2026-01-02", verified=True),
              Claim(cr.slug, "v1", 0, "Walk daily", "steps a day helps you lose fat", "Walking Habits",
                    date="2026-01-02", verified=True),
              Claim(cr.slug, "v1", 0, "Eat protein", "lose fat steadily", "Protein Intake", date="2026-01-02",
                    verified=True)]
    TopicRegistry(paths).add_claims(claims)
    with open(paths.claims, "w", encoding="utf-8") as f:
        for c in claims:
            f.write(json.dumps(c.to_dict()) + "\n")
    for topic in ("Walking Fat Loss", "Walking Habits", "Protein Intake"):
        writer.write_topic_note(topic, [c for c in claims if c.topic == topic], [], [])
    writer.write_video_note(cr, store.get("v1").meta(), t, topics=["Walking Fat Loss", "Walking Habits", "Protein Intake"])
    return paths, store, writer


def test_consolidate_merges_rewrites_and_keeps_links(tmp_path):
    paths, store, writer = _setup(tmp_path)
    res = consolidate(paths, store, writer, None, FakeLLM())
    assert res["merged_topics"] == 2 and res["topics_now"] == 2
    topics = {json.loads(line)["topic"] for line in paths.claims.read_text(encoding="utf-8").splitlines()}
    assert topics == {"Walking", "Protein Intake"}
    assert paths.topic_note("Walking").exists()
    assert not paths.topic_note("Walking Fat Loss").exists() and not paths.topic_note("Walking Habits").exists()
    note = paths.topic_note("Walking").read_text(encoding="utf-8")
    assert "Walking Fat Loss" in note and "Walking Habits" in note        # old names kept as aliases
    video = next(paths.videos_dir("Jeremy Ethier").glob("*.md")).read_text(encoding="utf-8")
    assert "[[Walking]]" in video and "[[Walking Habits]]" not in video
    reg = TopicRegistry(paths)
    assert reg.canonical("walking habits") == "Walking"                   # future claims land in the merged topic


def test_dry_run_changes_nothing(tmp_path):
    paths, store, writer = _setup(tmp_path)
    before = paths.claims.read_text(encoding="utf-8")
    res = consolidate(paths, store, writer, None, FakeLLM(), dry_run=True)
    assert res["groups"] == {"Walking": ["Walking Fat Loss", "Walking Habits"]}
    assert paths.claims.read_text(encoding="utf-8") == before
