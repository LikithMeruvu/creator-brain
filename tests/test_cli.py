import json
from pathlib import Path

import pytest

from cbrain import cli, worker
from cbrain.config import Settings
from cbrain.paths import VaultPaths
from tests.rcpt_fakes import install


@pytest.fixture
def rec(monkeypatch, tmp_path):
    r = install(monkeypatch, tmp_path)
    monkeypatch.setattr(worker, "start_background", lambda paths: True)
    return r


@pytest.fixture
def run(rec, tmp_path, capsys):
    vault = str(tmp_path / "vault")

    def go(*argv):
        code = cli.main(["--vault", vault, *argv])
        out = capsys.readouterr()
        return code, out.out, out.err
    go.vault = vault
    return go


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("argv,attrs", [
    (["init"], {"cmd": "init"}),
    (["setup"], {"cmd": "setup"}),
    (["add", "@Jeremy", "--full", "--shorts"], {"handle": "@Jeremy", "full": True, "shorts": True}),
    (["add", "@Jeremy"], {"full": False, "shorts": False}),
    (["update"], {"creator": None}),
    (["update", "jeremy"], {"creator": "jeremy"}),
    (["status", "--json"], {"json": True, "creator": None}),
    (["search", "protein", "--creator", "a", "--creator", "b", "--limit", "3"],
     {"query": "protein", "creator": ["a", "b"], "limit": 3}),
    (["topic", "Protein Intake"], {"name": "Protein Intake"}),
    (["compare", "protein", "--creator", "a", "--creator", "b"], {"topic": "protein", "creator": ["a", "b"]}),
    (["profile", "--set", "goal=x", "--set", "age=3"], {"sets": ["goal=x", "age=3"]}),
    (["doctor", "--fix"], {"fix": True}),
    (["pause"], {"cmd": "pause"}), (["resume"], {"cmd": "resume"}),
    (["retry", "c"], {"creator": "c"}), (["worker"], {"cmd": "worker"}), (["mcp"], {"cmd": "mcp"}),
])
def test_parser(argv, attrs):
    ns = cli.build_parser().parse_args(["--vault", "V", *argv])
    assert ns.vault == "V"
    for k, v in attrs.items():
        assert getattr(ns, k) == v


def test_parser_rejects_unknown_command_and_missing_args(capsys):
    for bad in ([], ["frobnicate"], ["add"], ["search"]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(bad)


# ---------------------------------------------------------------- commands

def test_init_creates_vault(run):
    code, out, _ = run("init")
    assert code == 0 and "Vault ready" in out


def test_add_then_status_search_topic_profile(run, rec):
    code, out, _ = run("add", "@JeremyEthier")
    assert code == 0 and "Queued 3 of 10" in out

    code, out, _ = run("status")
    assert code == 0 and "Jeremy Ethier" in out and "0%" in out

    code, out, _ = run("status", "--json")
    data = json.loads(out)
    assert data["totals"]["queued"] == 3 and data["worker_running"] is False

    rec.hits = [{"creator": "jeremy-ethier", "video_id": "abc", "title": "Protein 101", "published": "2026-01-01",
                 "t": 61, "text": "eat more protein", "score": 1, "kind": "chunks"}]
    code, out, _ = run("search", "protein", "--creator", "jeremy-ethier")
    assert code == 0 and "Protein 101" in out and "https://youtu.be/abc?t=61" in out and "eat more protein" in out

    code, out, _ = run("topic", "nothing here")
    assert code == 0 and "No topic note" in out

    code, out, _ = run("profile", "--set", "goal=build muscle", "--set", "age=31", "--set", "injuries=knee, shoulder")
    assert code == 0 and "build muscle" in out
    code, out, _ = run("profile", "--json")
    prof = json.loads(out)["profile"]
    assert prof["age"] == 31 and prof["injuries"] == ["knee", "shoulder"]


def test_profile_rejects_bad_set(run):
    code, _, err = run("profile", "--set", "nonsense")
    assert code == 2 and "KEY=VALUE" in err


def test_errors_are_plain_english_and_nonzero(run, rec):
    rec.list_error = RuntimeError("boom")
    code, out, err = run("add", "@x")
    assert code == 1 and "Problem:" in err and "What to do:" in err and out == ""
    code, out, _ = run("add", "@x", "--json")
    assert code == 1 and "error" in json.loads(out)


def test_pause_resume_retry_doctor(run, rec):
    run("add", "@JeremyEthier")
    assert run("pause")[0] == 0
    assert VaultPaths(Path(run.vault)).pause_flag.exists()
    assert run("resume")[0] == 0 and not VaultPaths(Path(run.vault)).pause_flag.exists()
    assert "No failed" in run("retry")[1]
    code, out, _ = run("doctor", "--json")
    assert code == 0 and "issues" in json.loads(out)
    assert run("doctor")[0] == 0


def test_worker_and_mcp_commands_delegate(run, monkeypatch):
    seen = {}
    monkeypatch.setattr(worker, "main", lambda argv=None: seen.setdefault("worker", argv) and 0)
    from cbrain import mcp_server
    monkeypatch.setattr(mcp_server, "main", lambda argv=None: seen.setdefault("mcp", argv) and 0)
    assert run("worker")[0] == 0 and run("mcp")[0] == 0
    assert seen["worker"] == ["--vault", run.vault] and seen["mcp"] == ["--vault", run.vault]


# ---------------------------------------------------------------- setup wizard

def test_setup_wizard_saves_everything(run, monkeypatch, tmp_path):
    answers = iter([str(tmp_path / "proxies.txt")])
    yesno = iter([False, True])
    monkeypatch.setattr(cli, "_ask_secret", lambda p: "gsk_test_key")
    monkeypatch.setattr(cli, "_ask", lambda p, d="": next(answers))
    monkeypatch.setattr(cli, "_ask_yes_no", lambda p, d: next(yesno))
    code, out, _ = run("setup")
    assert code == 0 and "Settings saved" in out
    paths = VaultPaths(Path(run.vault))
    s = Settings.load(paths)
    assert s.groq_api_key == "gsk_test_key" and s.proxy_file.endswith("proxies.txt")
    assert s.use_free_proxies is False and s.local_whisper is True
    assert "gsk_test_key" not in paths.config.read_text(encoding="utf-8")        # key stays out of the vault
    assert json.loads((tmp_path / "secrets.json").read_text())["groq_api_key"] == "gsk_test_key"


def test_setup_keeps_defaults_on_enter(run, monkeypatch):
    monkeypatch.setattr(cli, "_ask_secret", lambda p: "")
    monkeypatch.setattr(cli, "_ask", lambda p, d="": d)
    monkeypatch.setattr(cli, "_ask_yes_no", lambda p, d: d)
    assert run("setup")[0] == 0
    s = Settings.load(VaultPaths(Path(run.vault)))
    assert s.groq_api_key == "" and s.use_free_proxies is True and s.local_whisper is False


def test_formatters_cope_with_minimal_data():
    assert "ok" in cli._fmt_status({"message": "ok", "creators": [], "parked_reasons": [], "proxy": None})
    assert cli._bar(50).count("#") == 10
