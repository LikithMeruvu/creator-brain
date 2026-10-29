"""Vault health check (and safe repairs). Reads notes in a streaming fashion so big vaults stay fast."""

from __future__ import annotations

import importlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import worker
from .jobs import JobStore, WriterLock, now_iso
from .paths import VaultPaths

SAMPLE = 8
_LINK_RE = re.compile(r"\[\[([^\]\n]+?)\]\]")
_SKIP_DIRS = {".brain", ".obsidian", ".git", ".trash"}
_ROOT_NOTES = {"home", "me", "agents"}


def _mod(name: str):
    return importlib.import_module(name)


def _issue(check: str, severity: str, count: int, detail: list, hint: str, fixed: int = 0) -> dict:
    return {"check": check, "severity": severity, "count": count, "detail": detail[:SAMPLE], "fixed": fixed, "hint": hint}


def _notes(paths: VaultPaths):
    for p in paths.root.rglob("*.md"):
        if not any(part in _SKIP_DIRS for part in p.relative_to(paths.root).parts):
            yield p


def _aliases(text: str) -> list[str]:
    m = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        return []
    out: list[str] = []
    block = m.group(1)
    inline = re.search(r"^aliases:\s*\[(.*?)\]\s*$", block, re.M)
    if inline:
        out += [a.strip().strip("\"'") for a in inline.group(1).split(",") if a.strip()]
    multi = re.search(r"^aliases:\s*\r?\n((?:[ \t]*-.*\r?\n?)+)", block, re.M)
    if multi:
        out += [l.strip()[1:].strip().strip("\"'") for l in multi.group(1).splitlines() if l.strip().startswith("-")]
    return out


def _check_links(paths: VaultPaths) -> tuple[dict, dict]:
    names: set[str] = set()
    inbound: set[str] = set()
    links: list[tuple[str, str]] = []
    all_notes = list(_notes(paths))
    for p in all_notes:
        names.add(p.stem.lower())
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(a.lower() for a in _aliases(text))
        targets = {re.split(r"[|#^]", m.group(1))[0].strip().rsplit("/", 1)[-1] for m in _LINK_RE.finditer(text)}
        for t in targets:
            if t:
                inbound.add(t.lower())
                links.append((p.stem, t))
    broken = [(s, t) for s, t in links
              if t.lower() not in names and not t.lower().endswith((".png", ".jpg", ".jpeg", ".base", ".pdf"))]
    orphans = [p.stem for p in all_notes
               if p.stem.lower() not in inbound and p.stem.lower() not in _ROOT_NOTES
               and p.parent != paths.outputs and p.parent.parent != paths.creators]
    return ({"count": len(broken), "sample": [f"{s} -> [[{t}]]" for s, t in broken[:SAMPLE]]},
            {"count": len(orphans), "sample": orphans[:SAMPLE]})


def _iter_claims_stats(paths: VaultPaths) -> tuple[int, int]:
    total = bad = 0
    for d in worker.iter_claim_dicts(paths):
        total += 1
        if not d.get("verified"):
            bad += 1
    return total, bad


def _rows(store: JobStore, where: str, args: tuple = ()):
    cur = store.db.execute(f"SELECT * FROM videos WHERE {where}", args)
    while True:
        batch = cur.fetchmany(500)
        if not batch:
            return
        for r in batch:
            yield store._row(r)


def check(paths: VaultPaths, store: JobStore, index, fix: bool = False) -> dict:
    issues: list[dict] = []
    fixed_total = 0
    now = time.time()
    creators = {c.slug: c for c in store.list_creators()}
    try:
        writer = _mod("cbrain.vault.writer").VaultWriter(paths)
    except Exception:  # noqa: BLE001
        writer = None

    # 1. broken links + orphans
    try:
        broken, orphans = _check_links(paths)
        if broken["count"]:
            issues.append(_issue("broken_links", "warn", broken["count"], broken["sample"],
                                 "Links to notes that do not exist. Usually fixed by re-running `brain doctor --fix` after a worker run."))
        if orphans["count"]:
            issues.append(_issue("orphan_notes", "info", orphans["count"], orphans["sample"],
                                 "Notes nothing links to. Harmless; they still appear in search."))
    except Exception as e:  # noqa: BLE001
        issues.append(_issue("link_scan", "warn", 1, [f"{type(e).__name__}: {e}"], "Could not scan the notes."))

    # 2. video notes missing / transcripts missing
    missing_notes: list[str] = []
    missing_tr: list[str] = []
    n_notes = n_tr = fixed_notes = fixed_tr = 0
    for row in _rows(store, "selected=1 AND state IN ('fetched','written','extracting','done')"):
        has_tr = paths.transcript(row.id).exists()
        if not has_tr:
            n_tr += 1
            if len(missing_tr) < SAMPLE:
                missing_tr.append(f"{row.id} {row.title[:50]}")
            if fix:
                store.mark(row.id, "queued")
                fixed_tr += 1
            continue
        creator = creators.get(row.creator_slug)
        if row.state == "fetched" or creator is None or writer is None:
            continue
        meta = row.meta()
        meta.channel_id = creator.channel_id
        try:
            note = writer.video_note_path(creator, meta)
        except Exception:  # noqa: BLE001
            continue
        if not Path(note).exists():
            n_notes += 1
            if len(missing_notes) < SAMPLE:
                missing_notes.append(f"{row.id} {row.title[:50]}")
            if fix:
                try:
                    tr = worker.load_transcript(paths, row.id)
                    writer.write_video_note(creator, meta, tr)
                    if index is not None:
                        index.index_transcript(creator, meta, tr)
                    fixed_notes += 1
                except Exception:  # noqa: BLE001
                    pass
    if n_notes:
        issues.append(_issue("video_notes_missing", "warn", n_notes, missing_notes,
                             "Transcript saved but the note is gone. `--fix` re-renders it.", fixed_notes))
    if n_tr:
        issues.append(_issue("transcripts_missing", "warn", n_tr, missing_tr,
                             "The job says it has a transcript but the file is missing. `--fix` queues it for re-download.", fixed_tr))
    fixed_total += fixed_notes + fixed_tr

    # 3. claims
    total_claims, unverified = _iter_claims_stats(paths)
    if unverified:
        issues.append(_issue("unverified_claims", "warn", unverified, [f"{unverified} of {total_claims} claims"],
                             "Claims whose quote was not confirmed in the transcript. Treat them as unreliable; they are not auto-deleted."))

    # 4. parked videos
    parked = store.counts().get("parked", 0)
    if parked:
        reasons = [f"{r['n']} x {r['r']}" for r in store.db.execute(
            "SELECT substr(COALESCE(last_error,'unknown'),1,80) r, COUNT(*) n FROM videos "
            "WHERE selected=1 AND state='parked' GROUP BY r ORDER BY n DESC LIMIT 8")]
        n_fixed = store.unpark() if fix else 0
        issues.append(_issue("parked_videos", "warn", parked, reasons,
                             "Gave up after repeated failures. `--fix` (or `brain retry`) tries them again.", n_fixed))
        fixed_total += n_fixed

    # 5. stale creators
    hours = _daily_hours(paths)
    stale = []
    for c in creators.values():
        last = _parse(c.last_checked_at)
        if last is None or (datetime.now(timezone.utc) - last).total_seconds() > hours * 3600:
            stale.append(c.slug)
    if stale:
        issues.append(_issue("stale_creators", "info", len(stale), stale,
                             "Not checked for new videos recently. Run `brain update` (the MCP server also does this daily)."))

    # 6. duplicate topics
    dupes = _duplicate_topics(paths)
    if dupes:
        issues.append(_issue("duplicate_topics", "info", len(dupes), [" / ".join(d) for d in dupes],
                             "Topics that look like the same thing. Add one as an alias of the other in topics.json."))

    # 7. stale leases
    stale_leases = store.db.execute(
        "SELECT COUNT(*) FROM videos WHERE state IN ('fetching','extracting') AND lease_until<?", (now,)).fetchone()[0]
    if stale_leases:
        released = 0
        if fix and not WriterLock(paths.writer_lock).is_locked():
            released = store.release_stale_leases()
        issues.append(_issue("stale_leases", "warn", stale_leases, [], "A worker crashed mid-video. `--fix` hands the jobs back.", released))
        fixed_total += released

    # 8. manifest / schema
    man = worker.read_manifest(paths)
    if man.get("schema_version") != worker.MANIFEST_VERSION:
        done = 0
        if fix:
            worker.update_manifest(paths)
            done = 1
        issues.append(_issue("manifest", "info", 1, [f"schema_version={man.get('schema_version')!r}"],
                             "manifest.json missing or from another version. `--fix` rewrites it.", done))
        fixed_total += done

    # 9. pause flag
    if paths.pause_flag.exists():
        issues.append(_issue("paused", "info", 1, [], "Downloads are paused. Run `brain resume`."))

    remaining = [i for i in issues if i["count"] - i["fixed"] > 0 and i["severity"] != "info"]
    ok = not remaining
    errors_n = sum(i["count"] - i["fixed"] for i in remaining)
    if ok and not issues:
        msg = "All good. Nothing to fix."
    elif ok:
        msg = "No problems. A few notes for your information (see issues)."
    else:
        msg = f"Found {len(remaining)} thing(s) to look at" + (f"; repaired {fixed_total}." if fix else ". Run with fix to repair what is safe.")
    return {"ok": ok, "issues": issues, "fixed": fixed_total, "checked_at": now_iso(),
            "worker_running": WriterLock(paths.writer_lock).is_locked(), "message": msg, "open_problem_count": errors_n}


def _parse(s: str | None):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _daily_hours(paths: VaultPaths) -> float:
    try:
        from .config import Settings
        return Settings.load(paths).daily_check_hours
    except Exception:  # noqa: BLE001
        return 24.0


def _duplicate_topics(paths: VaultPaths) -> list[list[str]]:
    try:
        data = json.loads(paths.topics_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    groups: dict[str, list[str]] = {}
    for t in data:
        words = re.sub(r"[^a-z0-9]+", " ", t.lower()).split()
        key = " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words)
        groups.setdefault(key, []).append(t)
    return [g for g in groups.values() if len(g) > 1]
