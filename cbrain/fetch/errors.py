"""Fetch exceptions plus one classifier that turns yt-dlp / network failures into them."""

from __future__ import annotations


class FetchError(Exception):
    """Base class for everything a fetch lane can raise."""


class Blocked(FetchError):
    """IP/proxy rate-limited or bot-checked: try another lane, do not count an attempt."""


class NoCaptions(FetchError):
    """No usable caption track: go down the ladder to audio. `info` keeps the video metadata we did get."""

    def __init__(self, *args, info: dict | None = None):
        super().__init__(*args)
        self.info = info or {}


class Retryable(FetchError):
    """Network hiccup: count an attempt, retry later."""


class Permanent(FetchError):
    """Private / removed / members-only: park immediately."""


_BLOCKED = (
    "sign in to confirm", "confirm you're not a bot", "confirm you\u2019re not a bot", "http error 429",
    "too many requests", "rate-limit", "rate limit", "this content isn't available, try again later",
    "this content isn\u2019t available, try again later", "ip address is blocked", "ipblocked",
)
_PERMANENT = (
    "private video", "video unavailable", "this video is unavailable", "has been removed", "removed by the uploader",
    "members-only", "members only", "join this channel", "age-restricted", "confirm your age",
    "sign in to confirm your age", "account associated with this video has been terminated",
    "this video is not available", "video has been removed", "copyright", "no longer available",
    "this live event", "premieres in", "is a live event", "live stream recording is not available",
    "inappropriate", "does not exist",
)
_PROXY = (
    "proxyerror", "proxy error", "unable to connect to proxy", "tunnel connection failed", "socks",
    "proxy authentication", "http error 407", "cannot connect to proxy",
)
_NETWORK = (
    "timed out", "timeout", "connection reset", "connection aborted", "connection refused", "remote end closed",
    "temporary failure", "name or service not known", "getaddrinfo failed", "network is unreachable",
    "incomplete read", "ssl", "eof occurred", "http error 5", "urlopen error", "connection error",
    "unable to download", "remotedisconnected", "read error",
)


def classify(exc: BaseException | str, proxied: bool = False) -> FetchError:
    """Map an arbitrary exception/message to the matching FetchError subclass.

    Order matters: proxy failures first (they blame the proxy, not the video), then the
    explicit YouTube blocks, then permanent video states, then generic network trouble.
    """
    if isinstance(exc, FetchError):
        return exc
    msg = str(exc)
    low = msg.lower()
    short = " ".join(msg.split())[:300]
    if proxied and any(t in low for t in _PROXY):
        return Blocked(f"proxy failure: {short}")
    if "confirm your age" in low or "age-restricted" in low:
        return Permanent(short)
    if any(t in low for t in _BLOCKED):
        return Blocked(short)
    if any(t in low for t in _PERMANENT):
        return Permanent(short)
    if proxied and any(t in low for t in _NETWORK):
        # a dead/slow proxy looks like a plain network error: blame the proxy, keep the attempt
        return Blocked(f"proxy network failure: {short}")
    if any(t in low for t in _NETWORK) or isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return Retryable(short)
    return Retryable(short)
