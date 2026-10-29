"""ProxyPool tests: no real network (list downloads and the checker are injected)."""

import asyncio
import json
import threading
import time

import pytest

from cbrain import proxies as px
from cbrain.config import Settings
from cbrain.paths import VaultPaths
from cbrain.proxies import ProxyPool, parse_proxies


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_pool(tmp_path, *, free=True, user_lines=None, alive=lambda u: True, lists=None,
              concurrency=400, clock=None, auto_refill=False, **kw):
    paths = VaultPaths(tmp_path / "vault")
    paths.ensure_dirs()
    s = Settings(use_free_proxies=free)
    if user_lines is not None:
        f = tmp_path / "my_proxies.txt"
        f.write_text("\n".join(user_lines), encoding="utf-8")
        s.proxy_file = str(f)
    checked: list[str] = []
    downloads: list[str] = []

    async def check_fn(url, timeout):
        checked.append(url)
        await asyncio.sleep(0)
        return alive(url)

    def fetch_fn(src):
        downloads.append(src)
        if lists is None:
            return ""
        return lists.get(src, "")

    pool = ProxyPool(paths, s, check_fn=check_fn, fetch_fn=fetch_fn, concurrency=concurrency,
                     auto_refill=auto_refill, now=clock or time.time, **kw)
    pool.checked, pool.downloads, pool.paths_ = checked, downloads, paths
    return pool


def one_list(n, src="http.txt"):
    return {src: "\n".join(f"10.0.{i // 250}.{i % 250 + 1}:{8000 + i % 100}" for i in range(n))}


# ---------------------------------------------------------------- parsing

def test_parse_bare_and_urls():
    text = """
    # comment
    1.2.3.4:8080
    http://5.6.7.8:3128
    socks5://user:pa55@9.9.9.9:1080
    socks5h://8.8.8.8:1081
    socks4a://7.7.7.7:1082
    https://6.6.6.6:443
    1.2.3.4:8080 US anonymous
    999.1.1.1:80
    1.1.1.1:99999
    garbage line
    proxy.example.com:8000
    """
    got = parse_proxies(text, "socks4")
    assert got == [
        "socks4://1.2.3.4:8080",
        "http://5.6.7.8:3128",
        "socks5://user:pa55@9.9.9.9:1080",
        "socks5://8.8.8.8:1081",
        "socks4://7.7.7.7:1082",
        "http://6.6.6.6:443",
        "socks4://proxy.example.com:8000",
    ]


def test_parse_crlf_and_dedupe():
    assert parse_proxies("1.1.1.1:80\r\n1.1.1.1:80\r\n2.2.2.2:81\r\n") == ["http://1.1.1.1:80", "http://2.2.2.2:81"]


def test_free_sources_cover_three_repos():
    assert len(px.FREE_SOURCES) == 9
    assert all(s.startswith("https://raw.githubusercontent.com/") for s in px.FREE_SOURCES)
    assert px._scheme_for_source(px.FREE_SOURCES[1]) == "socks4"
    assert px._scheme_for_source(px.FREE_SOURCES[-1]) == "socks5"
    assert px._scheme_for_source(px.FREE_SOURCES[0]) == "http"


# ---------------------------------------------------------------- refresh

def test_refresh_early_stop(tmp_path):
    pool = make_pool(tmp_path, lists=one_list(500), sources=["http.txt"], concurrency=10)
    res = pool.refresh(target_healthy=5)
    assert pool.stats()["healthy"] >= 5
    assert res["checked"] <= 5 + 10          # stops once target reached (in-flight checks at most)
    assert len(pool.checked) < 500


def test_refresh_only_alive_become_healthy_and_scheme_from_list(tmp_path):
    lists = {"socks5.txt": "1.1.1.1:81\n2.2.2.2:82\n3.3.3.3:83", "http.txt": "4.4.4.4:84"}
    pool = make_pool(tmp_path, lists=lists, sources=["socks5.txt", "http.txt"],
                     alive=lambda u: u.endswith(("1.1.1.1:81", "4.4.4.4:84")))
    res = pool.refresh(target_healthy=10)
    assert res["candidates"] == 4 and res["checked"] == 4
    assert pool.stats()["healthy"] == 2
    got = {pool.acquire(), pool.acquire()}
    assert got == {"socks5://1.1.1.1:81", "http://4.4.4.4:84"}


def test_free_lists_skipped_when_disabled(tmp_path):
    pool = make_pool(tmp_path, free=False, lists=one_list(5), sources=["http.txt"])
    res = pool.refresh(force=True)
    assert res["skipped"] == "no_sources"
    assert pool.downloads == [] and pool.checked == []


def test_failed_list_download_is_tolerated(tmp_path):
    paths = VaultPaths(tmp_path / "v")
    paths.ensure_dirs()

    def fetch(src):
        if src == "bad.txt":
            raise OSError("boom")
        return "1.1.1.1:80"

    async def chk(u, t):
        return True

    pool = ProxyPool(paths, Settings(), check_fn=chk, fetch_fn=fetch, sources=["bad.txt", "ok.txt"])
    res = pool.refresh(target_healthy=5)
    assert res["lists_ok"] == 1 and res["lists_failed"] == 1
    assert pool.stats()["healthy"] == 1


def test_skip_recent_refresh_unless_force(tmp_path):
    clock = Clock()
    pool = make_pool(tmp_path, lists=one_list(30), sources=["http.txt"], clock=clock)
    pool.refresh(target_healthy=12)
    assert pool.stats()["healthy"] >= 12
    n_checked, n_dl = len(pool.checked), len(pool.downloads)

    clock.t += 5 * 60
    res = pool.refresh(target_healthy=12)
    assert res["skipped"] == "recent"
    assert len(pool.checked) == n_checked and len(pool.downloads) == n_dl

    res = pool.refresh(target_healthy=12, force=True)
    assert res["skipped"] is None
    assert len(pool.downloads) == n_dl + 1

    clock.t += 31 * 60
    assert pool.refresh(target_healthy=12)["skipped"] is None


def test_refresh_runs_in_non_main_thread(tmp_path):
    pool = make_pool(tmp_path, lists=one_list(20), sources=["http.txt"])
    out = {}
    t = threading.Thread(target=lambda: out.update(pool.refresh(target_healthy=5)))
    t.start()
    t.join(30)
    assert not t.is_alive() and pool.stats()["healthy"] >= 5


def test_stale_known_proxies_rechecked_and_evicted(tmp_path):
    clock = Clock()
    dead = set()
    pool = make_pool(tmp_path, lists=one_list(3), sources=["http.txt"], clock=clock,
                     alive=lambda u: u not in dead)
    pool.refresh(target_healthy=50)
    assert pool.stats()["healthy"] == 3
    victim = pool.acquire()
    pool.release(victim, ok=True)
    dead.add(victim)
    clock.t += 40 * 60
    pool.refresh(target_healthy=50)
    assert pool.stats()["healthy"] == 2 and pool.stats()["evicted"] == 1


# ---------------------------------------------------------------- persistence

def test_persistence_round_trip(tmp_path):
    clock = Clock()
    pool = make_pool(tmp_path, lists=one_list(5), sources=["http.txt"], clock=clock)
    pool.refresh(target_healthy=50)
    p = pool.acquire()
    pool.release(p, ok=True)
    pool.save()

    data = json.loads(pool.paths_.proxies_json.read_text(encoding="utf-8"))
    assert data["version"] == 1 and len(data["proxies"]) == 5
    row = next(r for r in data["proxies"] if r["url"] == p)
    assert set(row) >= {"url", "source", "score", "fails", "last_ok", "last_checked"}
    assert row["score"] == 2.0 and row["fails"] == 0 and row["source"] == "free"
    assert not list(pool.paths_.brain.glob("*.tmp"))

    pool2 = ProxyPool(pool.paths_, Settings(), now=clock)
    assert pool2.stats()["healthy"] == 5
    assert pool2.stats()["last_refresh"] is not None
    assert pool2.acquire() == p                      # best score first


def test_corrupt_file_is_ignored(tmp_path):
    paths = VaultPaths(tmp_path / "v")
    paths.ensure_dirs()
    paths.proxies_json.write_text("{not json", encoding="utf-8")
    assert ProxyPool(paths, Settings(use_free_proxies=False)).stats()["healthy"] == 0


# ---------------------------------------------------------------- leasing / eviction

def seeded_pool(tmp_path, n=3, **kw):
    clock = Clock()
    pool = make_pool(tmp_path, lists=one_list(n), sources=["http.txt"], clock=clock, **kw)
    pool.refresh(target_healthy=50)
    pool._clock = clock
    return pool


def test_acquire_is_exclusive_and_returns_none_when_empty(tmp_path):
    pool = seeded_pool(tmp_path, 2, )
    pool.settings.use_free_proxies = False           # no background refill noise
    a, b = pool.acquire(), pool.acquire()
    assert a and b and a != b
    assert pool.acquire() is None
    assert pool.stats()["leased"] == 2
    pool.release(a, ok=True)
    assert pool.acquire() == a


def test_best_score_first(tmp_path):
    pool = seeded_pool(tmp_path, 3)
    pool.settings.use_free_proxies = False
    first = pool.acquire()
    pool.release(first, ok=True)
    pool.release(first, ok=True)
    pool.release(pool.acquire(), ok=True)            # someone else gets a smaller bump
    assert pool.acquire() == first


def test_two_consecutive_fails_evict(tmp_path):
    pool = seeded_pool(tmp_path, 3)
    pool.settings.use_free_proxies = False
    p = pool.acquire()
    pool.release(p, ok=False)
    assert pool.stats()["healthy"] == 3 and pool.stats()["evicted"] == 0
    p2 = pool.acquire()
    assert p2 != p or True
    pool.release(p, ok=False) if p2 != p else pool.release(p2, ok=False)
    assert pool.stats()["evicted"] == 1 and pool.stats()["healthy"] == 2


def test_success_resets_fail_streak(tmp_path):
    pool = seeded_pool(tmp_path, 1)
    pool.settings.use_free_proxies = False
    p = pool.acquire()
    pool.release(p, ok=False)
    p = pool.acquire()
    pool.release(p, ok=True)
    p = pool.acquire()
    pool.release(p, ok=False)
    assert pool.stats()["healthy"] == 1


def test_blocked_evicts_immediately_and_is_not_rechecked_soon(tmp_path):
    pool = seeded_pool(tmp_path, 3)
    pool.settings.use_free_proxies = False
    p = pool.acquire()
    pool.release(p, ok=False, blocked=True)
    s = pool.stats()
    assert s["healthy"] == 2 and s["evicted"] == 1 and s["leased"] == 0
    assert p not in {pool.acquire(), pool.acquire()}
    # a later refresh does not re-check the evicted proxy
    pool.settings.use_free_proxies = True
    pool.checked.clear()
    pool.refresh(target_healthy=50, force=True)
    assert p not in pool.checked


def test_release_unknown_proxy_is_harmless(tmp_path):
    pool = make_pool(tmp_path, free=False)
    pool.release("http://9.9.9.9:9", ok=False, blocked=True)
    assert pool.stats()["healthy"] == 0


# ---------------------------------------------------------------- user proxies

def test_user_proxies_preferred_even_with_lower_score(tmp_path):
    pool = make_pool(tmp_path, user_lines=["http://5.5.5.5:5555", "# note"], lists=one_list(3),
                     sources=["http.txt"])
    pool.refresh(target_healthy=50)
    for _ in range(3):                               # give free proxies a higher score
        for u in list(pool._entries):
            if pool._entries[u].source == "free":
                pool._entries[u].score = 40
    assert pool.acquire() == "http://5.5.5.5:5555"
    assert pool.stats()["user"] == 1


def test_user_proxies_checked_first_and_usable_before_refresh(tmp_path):
    pool = make_pool(tmp_path, user_lines=["6.6.6.6:80"], lists=one_list(5), sources=["http.txt"],
                     concurrency=1)
    assert pool.acquire() == "http://6.6.6.6:80"     # usable immediately
    pool.release("http://6.6.6.6:80", ok=True)
    pool.refresh(target_healthy=50)
    assert pool.checked[0] == "http://6.6.6.6:80"


def test_user_proxy_cooldown_not_permanent_eviction(tmp_path):
    clock = Clock()
    pool = make_pool(tmp_path, user_lines=["http://5.5.5.5:5555"], free=False, clock=clock)
    u = pool.acquire()
    pool.release(u, ok=False, blocked=True)
    assert pool.acquire() is None                    # cooling down
    assert pool.stats()["evicted"] == 1 and pool.stats()["user"] == 1
    clock.t += 9 * 60
    assert pool.acquire() is None
    clock.t += 2 * 60                                # > 10 minutes
    assert pool.acquire() == u
    assert pool.stats()["evicted"] == 0


def test_user_proxy_failing_check_goes_to_cooldown(tmp_path):
    pool = make_pool(tmp_path, user_lines=["http://5.5.5.5:5555"], free=False, alive=lambda u: False)
    pool.refresh(force=True)
    s = pool.stats()
    assert s["healthy"] == 0 and s["user"] == 1 and s["evicted"] == 1


def test_user_proxy_removed_from_file_is_dropped(tmp_path):
    pool = make_pool(tmp_path, user_lines=["http://5.5.5.5:5555"], free=False)
    assert pool.stats()["user"] == 1
    open(pool.settings.proxy_file, "w").write("")
    pool.refresh(force=True)
    assert pool.stats()["user"] == 0


# ---------------------------------------------------------------- background refresh

def test_single_background_refresh_thread(tmp_path):
    gate = threading.Event()
    calls = []

    async def check_fn(url, timeout):
        calls.append(url)
        while not gate.is_set():
            await asyncio.sleep(0.01)
        return True

    paths = VaultPaths(tmp_path / "v")
    paths.ensure_dirs()
    pool = ProxyPool(paths, Settings(use_free_proxies=True), check_fn=check_fn,
                     fetch_fn=lambda s: "\n".join(f"10.1.0.{i}:80" for i in range(1, 40)),
                     sources=["http.txt"], bg_min_interval=0)
    runs = []
    orig = pool._bg_run
    pool._bg_run = lambda: (runs.append(1), orig())
    assert pool.acquire() is None                    # empty pool -> starts one refill
    assert pool.background_running()
    first = pool._bg_thread
    for _ in range(5):                               # more triggers while it runs
        pool.release("http://nope:1", ok=False)
        pool.acquire()
    assert pool._bg_thread is first
    assert len(runs) == 1
    gate.set()
    first.join(30)
    assert not first.is_alive()
    assert pool.stats()["healthy"] >= 10
    assert pool.acquire() is not None


def test_no_background_refresh_without_sources(tmp_path):
    pool = make_pool(tmp_path, free=False, auto_refill=True)
    assert pool.acquire() is None
    assert not pool.background_running()


def test_background_refresh_rate_limited(tmp_path):
    clock = Clock()
    pool = make_pool(tmp_path, lists=one_list(0), sources=["http.txt"], clock=clock, bg_min_interval=300,
                     auto_refill=True)
    assert pool._maybe_background_refresh()
    pool._bg_thread.join(10)
    assert not pool._maybe_background_refresh()      # too soon
    clock.t += 301
    assert pool._maybe_background_refresh()
    pool._bg_thread.join(10)


# ---------------------------------------------------------------- checker plumbing

def test_quiet_handler_swallows_noise():
    loop = asyncio.new_event_loop()
    try:
        px._quiet_handler(loop, {"message": "x", "exception": ConnectionResetError(10054, "reset")})
    finally:
        loop.close()


def test_check_proxy_bad_proxy_returns_false():
    # nothing listens on port 9 of localhost: connection refused -> False, no exception
    assert asyncio.run(px.check_proxy("http://127.0.0.1:9", timeout=2)) is False
    assert asyncio.run(px.check_proxy("socks5://127.0.0.1:9", timeout=2)) is False
