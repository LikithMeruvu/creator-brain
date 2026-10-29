import json

from cbrain.paths import VaultPaths
from cbrain.vault.graph import COLOR_GROUPS, write_obsidian_config


def test_creates_config_with_defaults(tmp_path):
    paths = VaultPaths(tmp_path / "v")
    write_obsidian_config(paths)
    cfg = json.loads((paths.obsidian / "graph.json").read_text(encoding="utf-8"))
    assert cfg["showTags"] is False
    queries = [g["query"] for g in cfg["colorGroups"]]
    assert queries == [g["query"] for g in COLOR_GROUPS]
    assert queries[0] == "path:creators -path:videos -path:updates"
    assert all(set(g["color"]) == {"a", "rgb"} and isinstance(g["color"]["rgb"], int) for g in cfg["colorGroups"])
    assert not list(paths.obsidian.glob("*.tmp"))


def test_merge_keeps_user_settings_and_groups(tmp_path):
    paths = VaultPaths(tmp_path / "v")
    paths.obsidian.mkdir(parents=True)
    mine = {"query": "tag:#mine", "color": {"a": 1, "rgb": 123}}
    recolored = {"query": "path:topics", "color": {"a": 1, "rgb": 999}}
    (paths.obsidian / "graph.json").write_text(json.dumps({
        "showTags": True, "repelStrength": 3, "colorGroups": [mine, recolored]}), encoding="utf-8")
    write_obsidian_config(paths)
    cfg = json.loads((paths.obsidian / "graph.json").read_text(encoding="utf-8"))
    assert cfg["showTags"] is True and cfg["repelStrength"] == 3          # user settings untouched
    assert cfg["colorGroups"][0] == mine and cfg["colorGroups"][1] == recolored
    queries = [g["query"] for g in cfg["colorGroups"]]
    assert queries.count("path:topics") == 1 and "path:outputs" in queries and "path:videos" in queries
    write_obsidian_config(paths)                                          # idempotent
    assert json.loads((paths.obsidian / "graph.json").read_text(encoding="utf-8")) == cfg


def test_broken_json_is_backed_up(tmp_path):
    paths = VaultPaths(tmp_path / "v")
    paths.obsidian.mkdir(parents=True)
    (paths.obsidian / "graph.json").write_text("{not json", encoding="utf-8")
    write_obsidian_config(paths)
    assert (paths.obsidian / "graph.json.bad").exists()
    assert len(json.loads((paths.obsidian / "graph.json").read_text(encoding="utf-8"))["colorGroups"]) == 5
