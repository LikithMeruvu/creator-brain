"""Groq chat-completions client (OpenAI-compatible) over stdlib urllib. JSON mode, retries, cost estimate."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Callable

from ..config import Settings

log = logging.getLogger("cbrain.think")

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

# ---- cost model: edit these if prices or models change (all USD, conservative on purpose) ----------------------
CHARS_PER_TOKEN = 4                      # rough English average
PRICE_PER_M_INPUT_TOKENS_USD = 0.25      # real price of the default model is lower; we over-estimate
PRICE_PER_M_OUTPUT_TOKENS_USD = 1.00
OUTPUT_TOKENS_PER_INPUT_TOKEN = 0.25     # claims JSON + hidden reasoning tokens of reasoning models
WINDOW_CHARS = 6000                      # transcript window size sent per request (see extract.py)
PROMPT_OVERHEAD_TOKENS_PER_WINDOW = 700  # system prompt + known topics + title

REQUEST_TIMEOUT_SECONDS = 120
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0
RETRY_AFTER_CAP_SECONDS = 120.0
RETRY_STATUSES = {408, 409, 425, 429}    # plus every 5xx

Transport = Callable[[str, dict, bytes, float], "tuple[int, dict, bytes]"]


class NoLLM(Exception):
    """No Groq API key configured."""


class LLMError(Exception):
    """The model call failed for good (never contains the API key)."""


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # TLS verification stays on (default context)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


def parse_json_text(text: str) -> dict | list:
    """Parse model output as JSON, tolerating ``` fences and chatter around the object."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = s.find(open_c), s.rfind(close_c)
        if 0 <= a < b:
            try:
                return json.loads(s[a:b + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("model output is not valid JSON")


class _RateGate:
    """Process-wide tokens-per-minute budget shared by every thread, so parallel windows never storm Groq.
    Learns the real limit from Groq's x-ratelimit-limit-tokens header; a 429 pauses all threads together."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: deque[tuple[float, int]] = deque()
        self.learned_tpm: int | None = None
        self.blocked_until = 0.0

    def wait(self, tokens: int, default_tpm: int, sleep: Callable[[float], None]) -> None:
        while True:
            with self.lock:
                now = time.time()
                while self.events and now - self.events[0][0] > 60:
                    self.events.popleft()
                limit = self.learned_tpm or default_tpm
                used = sum(t for _, t in self.events)
                if now < self.blocked_until:
                    delay = self.blocked_until - now
                elif self.events and used + tokens > limit * 0.9:
                    delay = 60 - (now - self.events[0][0]) + 0.5
                else:
                    self.events.append((now, tokens))
                    return
            sleep(max(0.5, delay))

    def learn(self, headers: dict) -> None:
        try:
            limit = int(headers.get("x-ratelimit-limit-tokens", "") or 0)
        except ValueError:
            return
        if limit > 0:
            self.learned_tpm = limit

    def block(self, seconds: float) -> None:
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.time() + seconds)


_GATE = _RateGate()


class LLMClient:
    def __init__(self, settings: Settings, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = MAX_RETRIES):
        self.settings = settings
        self._transport = transport or _urllib_transport
        self._gate = None if transport else _GATE  # injected test transports skip the shared budget
        self._sleep = sleep
        self.max_retries = max_retries

    def __repr__(self) -> str:  # never show the key
        return f"LLMClient(model={self.settings.extract_model!r}, available={self.available()})"

    def available(self) -> bool:
        return bool((self.settings.groq_api_key or "").strip())

    def complete_json(self, system: str, user: str, max_tokens: int = 4000) -> dict | list:
        """One chat completion in JSON mode. Retries 429/5xx/network/bad-JSON with backoff (honours retry-after)."""
        if not self.available():
            raise NoLLM("no Groq API key configured")
        model = self.settings.extract_model
        payload: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if "gpt-oss" in model:
            payload["reasoning_effort"] = "low"
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.settings.groq_api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "cbrain/0.1 (+creator-brain)",
        }
        last = "unknown error"
        est_tokens = (len(system) + len(user)) // CHARS_PER_TOKEN + min(max_tokens, 1500)
        for attempt in range(self.max_retries + 1):
            retry_after: float | None = None
            if self._gate:
                self._gate.wait(est_tokens, self.settings.llm_tpm, self._sleep)
            try:
                status, resp_headers, raw = self._transport(GROQ_CHAT_URL, headers, body, REQUEST_TIMEOUT_SECONDS)
                if self._gate:
                    self._gate.learn(resp_headers)
            except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as e:
                status, resp_headers, raw = 0, {}, b""
                last = f"network error: {type(e).__name__}"
            else:
                if status == 200:
                    try:
                        return self._parse_response(raw)
                    except ValueError as e:
                        last = str(e)
                else:
                    last = f"HTTP {status}: {_error_text(raw)}"
                    if status not in RETRY_STATUSES and status < 500:
                        raise LLMError(f"Groq request failed ({last})")
                    retry_after = _parse_retry_after(resp_headers.get("retry-after"))
            if attempt >= self.max_retries:
                break
            delay = retry_after if retry_after is not None else min(
                BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** attempt + random.uniform(0, 1))
            if self._gate and status == 429:
                self._gate.block(delay)  # every thread waits, not just this one
                log.info("Groq rate limit reached; all extraction threads wait %.1fs", delay)
            else:
                log.warning("Groq call failed (%s); retry %d/%d in %.1fs", last, attempt + 1, self.max_retries, delay)
            self._sleep(delay)
        raise LLMError(f"Groq request failed after {self.max_retries + 1} attempts ({last})")

    @staticmethod
    def _parse_response(raw: bytes) -> dict | list:
        try:
            data = json.loads(raw.decode("utf-8"))
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ValueError("unexpected response shape") from e
        if not content:
            raise ValueError(f"empty model output (finish_reason={choice.get('finish_reason')})")
        return parse_json_text(content)

    def estimate_cost_usd(self, input_chars: int) -> float:
        """Rough, conservative cost of extracting `input_chars` of transcript text."""
        if input_chars <= 0:
            return 0.0
        windows = -(-input_chars // WINDOW_CHARS)
        in_tokens = input_chars / CHARS_PER_TOKEN + windows * PROMPT_OVERHEAD_TOKENS_PER_WINDOW
        out_tokens = in_tokens * OUTPUT_TOKENS_PER_INPUT_TOKEN
        return (in_tokens * PRICE_PER_M_INPUT_TOKENS_USD + out_tokens * PRICE_PER_M_OUTPUT_TOKENS_USD) / 1_000_000


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), RETRY_AFTER_CAP_SECONDS))
    except ValueError:
        return None


def _error_text(raw: bytes) -> str:
    try:
        err = json.loads(raw.decode("utf-8", "replace")).get("error", {})
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        msg = raw.decode("utf-8", "replace")
    return (msg or "")[:300].replace("\n", " ")
