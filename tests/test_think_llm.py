import json

import pytest

from cbrain.config import Settings
from cbrain.think.llm import GROQ_CHAT_URL, LLMClient, LLMError, NoLLM, parse_json_text


def ok(content):
    body = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode()
    return 200, {}, body


class Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, json.loads(body), timeout))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(transport, key="gsk_secret", **kw):
    sleeps = []
    c = LLMClient(Settings(groq_api_key=key), transport=transport, sleep=sleeps.append, **kw)
    return c, sleeps


def test_no_key_raises_nollm_and_unavailable():
    c, _ = client(Scripted(), key="")
    assert not c.available()
    with pytest.raises(NoLLM):
        c.complete_json("s", "u")


def test_request_shape_and_json_result():
    t = Scripted(ok('{"claims": []}'))
    c, _ = client(t)
    assert c.complete_json("sys", "usr", max_tokens=123) == {"claims": []}
    url, headers, body, timeout = t.calls[0]
    assert url == GROQ_CHAT_URL == "https://api.groq.com/openai/v1/chat/completions"
    assert headers["Authorization"] == "Bearer gsk_secret"
    assert body["temperature"] == 0.1 and body["response_format"] == {"type": "json_object"}
    assert body["model"] == Settings().extract_model and body["max_completion_tokens"] == 123
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert timeout > 0


def test_retry_after_honoured_on_429_then_success():
    t = Scripted((429, {"retry-after": "7"}, b'{"error":{"message":"slow down"}}'), ok("[1]"))
    c, sleeps = client(t)
    assert c.complete_json("s", "u") == [1]
    assert sleeps == [7.0]


def test_5xx_and_network_errors_backoff_then_success():
    t = Scripted((503, {}, b""), OSError("boom"), ok('{"a": 1}'))
    c, sleeps = client(t)
    assert c.complete_json("s", "u") == {"a": 1}
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] - 1


def test_gives_up_after_max_retries_without_leaking_key():
    t = Scripted(*[(500, {}, b"nope")] * 3)
    c, sleeps = client(t, max_retries=2)
    with pytest.raises(LLMError) as e:
        c.complete_json("s", "u")
    assert "gsk_secret" not in str(e.value) and len(t.calls) == 3 and len(sleeps) == 2


def test_4xx_fails_fast():
    t = Scripted((401, {}, b'{"error":{"message":"invalid key"}}'), ok("{}"))
    c, sleeps = client(t)
    with pytest.raises(LLMError, match="401"):
        c.complete_json("s", "u")
    assert len(t.calls) == 1 and not sleeps


def test_bad_json_is_retried():
    t = Scripted(ok("not json at all"), ok('{"fine": true}'))
    c, _ = client(t)
    assert c.complete_json("s", "u") == {"fine": True}


def test_repr_hides_key():
    c, _ = client(Scripted())
    assert "gsk_secret" not in repr(c)


def test_parse_json_text_variants():
    assert parse_json_text('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_text('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        parse_json_text("nothing")


def test_cost_estimate_monotonic_and_small():
    c, _ = client(Scripted(), key="")
    assert c.estimate_cost_usd(0) == 0.0
    a, b = c.estimate_cost_usd(100_000), c.estimate_cost_usd(1_000_000)
    assert 0 < a < b < 1.0  # a 1M-char (~250k token) pile of transcripts costs well under a dollar


def test_rotates_models_and_parks_rate_limited_model(monkeypatch):
    """A 429 on one model rests only that model; the next call moves to the next model's free quota."""
    import cbrain.think.llm as llm_mod
    monkeypatch.setattr(llm_mod, "_GATES", {})
    calls = []

    def transport(url, headers, body, timeout):
        model = json.loads(body)["model"]
        calls.append(model)
        if model == "m1":
            return 429, {"retry-after": "3600"}, b'{"error":{"message":"daily limit"}}'
        return 200, {}, json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()

    s = Settings(groq_api_key="k", extract_model="m1", extract_fallback_models="m2,m3")
    client = LLMClient(s, transport=transport, sleep=lambda _: None)
    client._gated = True
    assert client.complete_json("sys", "user") == {"ok": True}
    assert calls == ["m1", "m2"]
    assert client.complete_json("sys", "user") == {"ok": True}
    assert calls[-1] in ("m2", "m3") and "m1" not in calls[2:]


def test_all_models_exhausted_raises_rate_limited(monkeypatch):
    import cbrain.think.llm as llm_mod
    monkeypatch.setattr(llm_mod, "_GATES", {})
    s = Settings(groq_api_key="k", extract_model="m1", extract_fallback_models="m2")
    client = LLMClient(s, transport=lambda *a: (429, {"retry-after": "7200"}, b"{}"), sleep=lambda _: None)
    client._gated = True
    with pytest.raises(llm_mod.RateLimited) as e:
        client.complete_json("sys", "user")
    assert e.value.retry_in > 300


def test_parse_groq_reset_durations():
    from cbrain.think.llm import _parse_duration
    assert _parse_duration("7.66s") == pytest.approx(7.66)
    assert _parse_duration("1m2.5s") == pytest.approx(62.5)
    assert _parse_duration("120ms") == pytest.approx(0.12)
    assert _parse_duration("2h0m1s") == pytest.approx(7201)
    assert _parse_duration(None) is None


def test_gate_uses_server_remaining_tokens():
    from cbrain.think.llm import _RateGate
    g = _RateGate()
    g.learn({"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "5000",
             "x-ratelimit-reset-tokens": "30s"})
    assert g.take(3000, 8000) == 0
    assert g.take(3000, 8000) > 0          # only 2000 left until reset
