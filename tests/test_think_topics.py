import json

from cbrain.models import Claim
from cbrain.paths import VaultPaths
from cbrain.think.topics import TopicRegistry, clean_topic, title_case, topic_key


def reg(tmp_path):
    return TopicRegistry(VaultPaths(tmp_path))


def mk(topic, text="x", vid="v", t=1.0):
    return Claim(creator="c", video_id=vid, t=t, claim=text, quote="q q q q q q", topic=topic)


def test_title_case_and_cleanup():
    assert title_case("protein intake") == "Protein Intake"
    assert title_case("HIIT for fat loss") == "HIIT for Fat Loss"
    assert title_case("full-body workouts") == "Full-Body Workouts"
    assert clean_topic("  [[protein]] intake: ") == "Protein Intake"
    assert clean_topic("") == "General" and clean_topic(None) == "General"


def test_keys_ignore_case_articles_plurals_order():
    assert topic_key("The Squats") == topic_key("squat")
    assert topic_key("Intake of Protein") == topic_key("Protein Intake")
    assert topic_key("Calories") == topic_key("Calorie")
    assert topic_key("Carbs") != topic_key("Protein")


def test_plural_and_case_merge(tmp_path):
    r = reg(tmp_path)
    assert r.canonical("protein intake") == "Protein Intake"
    assert r.canonical("Protein Intakes") == "Protein Intake"
    assert r.canonical("the PROTEIN intake") == "Protein Intake"
    assert r.all_topics() == ["Protein Intake"]


def test_builtin_synonyms(tmp_path):
    r = reg(tmp_path)
    assert r.canonical("Daily Protein") == "Protein Intake"
    assert r.canonical("Protein Requirements") == "Protein Intake"
    assert r.canonical("hypertrophy") == "Muscle Growth"
    assert r.canonical("Warm-Up") == r.canonical("warmup") == "Warm Up"
    assert r.canonical("Training to Failure") == r.canonical("Proximity to Failure")
    assert sorted(r.aliases("Protein Intake")) == ["Daily Protein", "Protein Requirements"]


def test_new_topic_registered_and_synonym_reuses_existing_canonical(tmp_path):
    r = reg(tmp_path)
    assert r.canonical("Knee Pain") == "Knee Pain"
    assert r.canonical("knee pains") == "Knee Pain"
    assert "Knee Pain" in r.all_topics()
    assert r.aliases("Knee Pain") == ["Knee Pains"]


def test_deterministic_across_instances(tmp_path):
    names = ["Daily Protein", "protein intake", "Carbs", "Carbohydrate Intake", "Sleep Quality", "Hypertrophy"]
    a = TopicRegistry(VaultPaths(tmp_path / "a"))
    b = TopicRegistry(VaultPaths(tmp_path / "b"))
    assert [a.canonical(n) for n in names] == [b.canonical(n) for n in names]


def test_add_claims_relabels_persists_and_reloads(tmp_path):
    paths = VaultPaths(tmp_path)
    r = TopicRegistry(paths)
    c1, c2, c3 = mk("Daily Protein", "a"), mk("Protein Intake", "b"), mk("Knee Pain", "c")
    grouped = r.add_claims([c1, c2, c3])
    assert set(grouped) == {"Protein Intake", "Knee Pain"}
    assert all(c.topic == "Protein Intake" for c in grouped["Protein Intake"]) and c1.topic == "Daily Protein"
    data = json.loads(paths.topics_json.read_text(encoding="utf-8"))
    assert data["Protein Intake"]["aliases"] == ["Daily Protein"]
    assert sorted(data["Protein Intake"]["claims"]) == sorted([c1.id, c2.id])
    assert not paths.topics_json.with_name("topics.json.tmp").exists()

    r.add_claims([c1])  # idempotent
    assert len(json.loads(paths.topics_json.read_text(encoding="utf-8"))["Protein Intake"]["claims"]) == 2

    r2 = TopicRegistry(paths)  # survives reload, aliases still resolve
    assert r2.canonical("Daily Protein") == "Protein Intake"
    assert r2.all_topics() == ["Knee Pain", "Protein Intake"]
    assert r2.aliases("daily protein") == ["Daily Protein"]


def test_corrupt_topics_file_is_ignored(tmp_path):
    paths = VaultPaths(tmp_path)
    paths.brain.mkdir(parents=True)
    paths.topics_json.write_text("{broken", encoding="utf-8")
    assert TopicRegistry(paths).all_topics() == []
