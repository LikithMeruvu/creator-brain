import asyncio
import json

import pytest

from cbrain import mcp_server
from cbrain.service import Brain
from tests.rcpt_fakes import install, seed_videos

TOOLS = {"add_creator", "update_creators", "brain_status", "search_brain", "get_topic", "compare_creators",
         "my_profile", "save_output", "suggest_creators", "brain_doctor", "add_videos"}


class FakeBrain:
    def __init__(self, daily=None):
        self.log = []
        self.daily = daily

    def maybe_daily_check(self):
        self.log.append("daily")
        return self.daily

    def __getattr__(self, name):
        def method(*a, **kw):
            self.log.append((name, a, kw))
            return {"ok": name}
        return method


def schema(tool):
    return getattr(tool, "inputSchema", None) or tool.input_schema


def run(coro):
    return asyncio.run(coro)


def result_dict(res) -> dict:
    """mcp 1.x returns (content, structured); 2.x returns CallToolResult."""
    if isinstance(res, tuple):
        res = res[1] if len(res) > 1 and isinstance(res[1], dict) else res[0]
    if isinstance(res, dict):
        return res
    sc = getattr(res, "structured_content", None)
    if sc:
        return sc
    content = getattr(res, "content", res)
    return json.loads(content[0].text)


def test_all_tools_registered_with_agent_guidance():
    server = mcp_server.create_server(lambda: FakeBrain())
    tools = {t.name: t for t in run(server.list_tools())}
    assert set(tools) == TOOLS
    text = lambda n: tools[n].description.lower()
    assert "before answering" in text("search_brain") and "timestamp url" in text("search_brain")
    assert "completeness" in text("search_brain")
    assert "personalis" in text("my_profile") and "save_output" in server.instructions
    assert "plan" in text("save_output") and "cite" in server.instructions.lower()
    assert schema(tools["search_brain"])["required"] == ["query"]
    assert "creators" in schema(tools["compare_creators"])["required"]


def test_every_tool_runs_daily_check_first_and_passes_args():
    fb = FakeBrain()
    server = mcp_server.create_server(lambda: fb)
    calls = {
        "add_creator": {"handle": "@x", "full": True},
        "update_creators": {},
        "brain_status": {"creator": "x"},
        "search_brain": {"query": "q", "creators": ["a"], "limit": 3},
        "get_topic": {"name": "t"},
        "compare_creators": {"topic": "t", "creators": ["a", "b"]},
        "my_profile": {"updates": {"goal": "g"}},
        "save_output": {"title": "T", "markdown": "m", "sources": ["s"]},
        "suggest_creators": {"topic": "t"},
        "brain_doctor": {"fix": True},
    }
    for name, args in calls.items():
        fb.log.clear()
        res = result_dict(run(server.call_tool(name, args)))
        assert fb.log[0] == "daily", name
        assert len(fb.log) == 2 and res, name
    fb.log.clear()
    run(server.call_tool("search_brain", {"query": "q", "creators": ["a"], "limit": 3}))
    assert fb.log[1] == ("search", ("q",), {"creators": ["a"], "limit": 3})


def test_daily_check_note_is_attached_and_failure_is_ignored():
    server = mcp_server.create_server(lambda: FakeBrain(daily={"started": True, "message": "Checking now."}))
    assert result_dict(run(server.call_tool("brain_status", {})))["daily_check"] == "Checking now."

    class Boom(FakeBrain):
        def maybe_daily_check(self):
            raise RuntimeError("x")
    server = mcp_server.create_server(lambda: Boom())
    assert result_dict(run(server.call_tool("brain_status", {}))) == {"ok": "status"}


def test_real_brain_through_mcp_in_process(monkeypatch, tmp_path):
    rec = install(monkeypatch, tmp_path)
    brain = Brain(tmp_path / "vault")
    monkeypatch.setattr("cbrain.worker.start_background", lambda p: True)
    server = mcp_server.create_server(lambda: brain)

    added = result_dict(run(server.call_tool("add_creator", {"handle": "@JeremyEthier"})))
    assert added["queued"] == 3 and added["brain"].startswith("jeremy-ethier")

    rec.hits = [{"creator": "jeremy-ethier", "video_id": "abc", "title": "Protein", "published": "2026-01-01",
                 "t": 61, "text": "eat protein", "score": 1.0, "kind": "chunks"}]
    found = result_dict(run(server.call_tool("search_brain", {"query": "protein"})))
    assert found["results"][0]["url"] == "https://youtu.be/abc?t=61"
    assert found["brain_completeness"]["jeremy-ethier"]["videos_total"] == 3

    prof = result_dict(run(server.call_tool("my_profile", {"updates": {"goal": "build muscle"}})))
    assert prof["profile"]["goal"] == "build muscle"
    assert "body text" in brain.paths.me.read_text(encoding="utf-8")

    err = result_dict(run(server.call_tool("brain_status", {"creator": "ghost"})))
    assert "error" in err and "hint" in err                          # errors are data, not exceptions


def test_get_brain_is_lazy_and_main_builds_server(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_BRAIN", None)
    made = {}
    monkeypatch.setattr(mcp_server, "create_server", lambda g: made.setdefault("srv", type("S", (), {"run": lambda self: made.setdefault("ran", True)})()))
    assert mcp_server.main(["--vault", str(tmp_path / "v")]) == 0
    assert made["ran"] and mcp_server._BRAIN is None                 # nothing touched disk until a tool is called
    assert not (tmp_path / "v").exists()
