"""Fill missing publish dates (e.g. transcripts imported from the old project) and re-render affected notes.

Dates come from one lightweight page lookup per video, spread across proxy lanes so the user's own IP is not
spent. Run it while the worker is paused: it rewrites claims.jsonl and topic notes.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .jobs import JobStore
from .models import Claim, Transcript
from .paths import VaultPaths

log = logging.getLogger("cbrain.backfill")
HAS_NOTE = ("fetched", "written", "extracting", "done")


def missing_dates(store: JobStore, creator_slug: str | None = None) -> list[str]:
    q = "SELECT id FROM videos WHERE published IS NULL AND selected=1 AND state IN ('fetched','written','extracting','done')"
    args: tuple = ()
    if creator_slug:
        q, args = q + " AND creator_slug=?", (creator_slug,)
    return [r["id"] for r in store.db.execute(q, args)]


def lookup(video_id: str, proxy: str | None) -> dict | None:
    """Publish date / views / duration from the watch page, without format processing or captions."""
    import yt_dlp
    from .fetch.captions import ydl_opts
    with yt_dlp.YoutubeDL(ydl_opts(proxy, 15, skip_download=True)) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)
    d = (info or {}).get("upload_date")
    if not d or len(d) != 8:
        return None
    return {"published": f"{d[:4]}-{d[4:6]}-{d[6:]}", "views": info.get("view_count"), "duration": info.get("duration")}


def fetch_dates(ids: list[str], pool, workers: int = 16) -> dict[str, dict]:
    """Look up dates through proxies; each id gets up to 4 different proxies."""
    found: dict[str, dict] = {}
    lock = threading.Lock()

    def one(vid: str) -> None:
        for _ in range(4):
            proxy = pool.acquire() if pool else None
            if pool and proxy is None:
                return
            try:
                meta = lookup(vid, proxy)
            except Exception:  # noqa: BLE001 - a bad proxy just means try the next one
                if pool:
                    pool.release(proxy, ok=False)
                continue
            if pool:
                pool.release(proxy, ok=True)
            if meta:
                with lock:
                    found[vid] = meta
            return

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, ids))
    return found


def apply(paths: VaultPaths, store: JobStore, found: dict[str, dict]) -> dict:
    """Save dates, rename/re-render video notes, date the claims and rewrite their topic notes."""
    from .vault.writer import VaultWriter
    writer = VaultWriter(paths)
    for vid, m in found.items():
        store.update_meta(vid, published=m["published"], views=m.get("views"), duration=m.get("duration"))
    renamed = 0
    for vid in found:
        row = store.get(vid)
        creator = store.get_creator(row.creator_slug) if row else None
        tpath = paths.transcript(vid)
        if not row or not creator or not tpath.exists():
            continue
        with gzip.open(tpath, "rt", encoding="utf-8") as f:
            transcript = Transcript.from_dict(json.load(f))
        writer.write_video_note(creator, row.meta(), transcript)  # keeps topics; renames off the no-date name
        renamed += 1

    # claims of these videos get their date, then every topic that mentions them is re-rendered
    claims: list[dict] = []
    if paths.claims.exists():
        with open(paths.claims, encoding="utf-8") as f:
            claims = [json.loads(line) for line in f if line.strip()]
    touched: set[str] = set()
    for c in claims:
        m = found.get(c.get("video_id"))
        if m and not c.get("date"):
            c["date"] = m["published"]
            touched.add(c["topic"])
    if touched:
        tmp = paths.claims.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for c in claims:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        os.replace(tmp, paths.claims)
    topics = _load(paths.topics_json)
    conflicts = _load(paths.conflicts)
    for t in sorted(touched):
        group = [Claim.from_dict(c) for c in claims if c.get("topic") == t]
        group.sort(key=lambda c: (c.date or "", c.creator, c.video_id, c.t), reverse=True)
        aliases = (topics.get(t) or {}).get("aliases", []) if isinstance(topics, dict) else []
        writer.write_topic_note(t, group, aliases, conflicts.get(t, []) if isinstance(conflicts, dict) else [])
    return {"dated": len(found), "notes_renamed": renamed, "topics_rewritten": len(touched)}


def run(paths: VaultPaths, store: JobStore, settings, creator_slug: str | None = None) -> dict:
    from .proxies import ProxyPool
    ids = missing_dates(store, creator_slug)
    if not ids:
        return {"missing": 0, "message": "Every video already has a publish date."}
    pool = ProxyPool(paths, settings)
    if pool.stats().get("healthy", 0) < 40:  # free proxies die fast: a fresh set dates ~95% in one pass
        pool.refresh(target_healthy=80, force=True)
    found = fetch_dates(ids, pool)
    pool.save()
    result = {"missing": len(ids), **apply(paths, store, found)}
    result["message"] = f"Dated {result['dated']} of {len(ids)} videos; {len(ids) - result['dated']} still unknown."
    return result


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
