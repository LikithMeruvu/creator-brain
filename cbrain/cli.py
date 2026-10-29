"""`brain` command line. Short plain-English output; add --json where a script might want the data."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from .paths import default_vault


# ---------------------------------------------------------------- prompts (monkeypatched in tests)

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    ans = input(f"{prompt}{suffix}: ").strip()
    return ans or default


def _ask_secret(prompt: str) -> str:
    return getpass.getpass(f"{prompt} (hidden, Enter to skip): ").strip()


def _ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    ans = input(f"{prompt} [{hint}]: ").strip().lower()
    return default if not ans else ans.startswith("y")


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brain", description="Creator Brain: turn YouTube creators into a searchable Obsidian vault.")
    p.add_argument("--vault", help="vault folder (default: $CBRAIN_VAULT or ~/CreatorBrain)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the vault folders and notes")
    sub.add_parser("setup", help="guided setup: Groq key, proxies, local whisper")

    a = sub.add_parser("add", help="add a creator and start building their brain")
    a.add_argument("handle", help="@handle or channel URL")
    a.add_argument("--full", action="store_true", help="every video, not just the newest 30%% + top winners")
    a.add_argument("--shorts", action="store_true", help="include Shorts")
    a.add_argument("--json", action="store_true")

    u = sub.add_parser("update", help="check for new videos")
    u.add_argument("creator", nargs="?")
    u.add_argument("--json", action="store_true")

    st = sub.add_parser("status", help="how complete is the brain?")
    st.add_argument("creator", nargs="?")
    st.add_argument("--json", action="store_true")

    s = sub.add_parser("search", help="search transcripts and claims")
    s.add_argument("query")
    s.add_argument("--creator", action="append", default=None, help="limit to a creator (repeatable)")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--json", action="store_true")

    t = sub.add_parser("topic", help="show everything the brain knows about a topic")
    t.add_argument("name")
    t.add_argument("--json", action="store_true")

    c = sub.add_parser("compare", help="compare creators on a topic")
    c.add_argument("topic")
    c.add_argument("--creator", action="append", default=None, help="creator (repeat for each)")
    c.add_argument("--json", action="store_true")

    pr = sub.add_parser("profile", help="show or edit your profile (Me.md)")
    pr.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", dest="sets")
    pr.add_argument("--json", action="store_true")

    d = sub.add_parser("doctor", help="check the vault and repair what is safe")
    d.add_argument("--fix", action="store_true")
    d.add_argument("--json", action="store_true")

    sub.add_parser("pause", help="pause downloading")
    sub.add_parser("resume", help="resume downloading")
    m = sub.add_parser("more", help="add more of a creator's videos to the brain (from the All Videos list)")
    m.add_argument("creator")
    m.add_argument("--match", help="words that must all appear in the title")
    m.add_argument("--ids", help="comma-separated video ids")
    m.add_argument("--top", type=int, help="add the N most-viewed videos not yet in the brain")
    m.add_argument("--dry-run", action="store_true", help="only show what would be added")
    m.add_argument("--json", action="store_true")
    rb = sub.add_parser("rebuild", help="re-render names, key points and links for the whole vault (pause first)")
    rb.add_argument("--no-merge", action="store_true", help="skip folding narrow topics together")
    rb.add_argument("--json", action="store_true")
    gr = sub.add_parser("graph", help="open the live, animated brain graph in your browser")
    gr.add_argument("--port", type=int, default=8787)
    gr.add_argument("--no-open", action="store_true")
    tm = sub.add_parser("merge-topics", help="fold narrow topics into broader ones (pause first)")
    tm.add_argument("--dry-run", action="store_true", help="only show the proposed groups")
    tm.add_argument("--json", action="store_true")
    dt = sub.add_parser("dates", help="look up missing publish dates and rename those notes (pause first)")
    dt.add_argument("creator", nargs="?")
    r = sub.add_parser("retry", help="try failed videos again")
    r.add_argument("creator", nargs="?")
    sub.add_parser("worker", help="run the background worker in this window")
    sub.add_parser("mcp", help="run the MCP server (stdio) for AI agents")
    return p


# ---------------------------------------------------------------- formatting

def _bar(pct: int, width: int = 20) -> str:
    filled = round(width * pct / 100)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct}%"


def _fmt_status(d: dict) -> str:
    lines = [d["message"], ""]
    for c in d.get("creators", []):
        lines.append(f"{c['name']:<28} {_bar(c['percent_built'])}  {c['videos_searchable']}/{c['videos_total']} searchable"
                     + (f", {c['failed']} failed" if c["failed"] else "")
                     + (f", {c['still_processing']} in progress" if c["still_processing"] else ""))
    if d.get("parked_reasons"):
        lines += ["", "Why some videos failed:"] + [f"  {r['count']} x {r['reason']}" for r in d["parked_reasons"]]
    px = d.get("proxy")
    if px:
        lines += ["", "Proxies: " + ", ".join(f"{k}={v}" for k, v in px.items() if isinstance(v, int | float | str))]
    return "\n".join(lines).strip()


def _fmt_search(d: dict) -> str:
    lines = [d["message"], ""]
    for i, r in enumerate(d.get("results", []), 1):
        lines += [f"{i}. {r['title']}  ({r['creator']}, {r.get('published') or 'n/a'}, at {r['timestamp']})",
                  f"   {r['snippet']}", f"   {r['url']}", ""]
    return "\n".join(lines).rstrip()


def _fmt_topic(d: dict) -> str:
    lines = [d["message"], ""]
    for c in d.get("claims", []):
        lines += [f"- [{c.get('creator')}] {c.get('claim')}", f"    \"{c.get('quote')}\"  {c.get('url')}"]
    for r in d.get("transcript_matches", []):
        lines += [f"- {r['title']} ({r['creator']}, {r['timestamp']}): {r['snippet']}", f"    {r['url']}"]
    return "\n".join(lines).rstrip()


def _fmt_compare(d: dict) -> str:
    lines = [d["message"], ""]
    for v in d.get("by_creator", {}).values():
        lines.append(f"== {v['name']} ({v['claim_count']} claims)")
        for c in v["claims"]:
            lines += [f"- {c['claim']}", f"    \"{c['quote']}\"  {c['url']}"]
        for r in v["transcript_matches"]:
            lines.append(f"- (transcript) {r['title']} at {r['timestamp']}: {r['snippet']}  {r['url']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _fmt_profile(d: dict) -> str:
    lines = [d["message"], ""] + [f"{k:<14} {v if v not in ('', [], None) else '-'}" for k, v in d["profile"].items()]
    return "\n".join(lines + ["", f"File: {d['path']}"])


def _fmt_doctor(d: dict) -> str:
    lines = [d["message"], ""]
    for i in d["issues"]:
        mark = {"warn": "!", "info": "-"}.get(i["severity"], "!")
        lines.append(f"{mark} {i['check']}: {i['count']}" + (f" (fixed {i['fixed']})" if i["fixed"] else "") + f"  {i['hint']}")
        lines += [f"     {x}" for x in i["detail"][:4]]
    return "\n".join(lines).rstrip()


def _emit(result: dict, as_json: bool, fmt=None) -> int:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 1 if "error" in result else 0
    if "error" in result:
        print(f"Problem: {result['error']}", file=sys.stderr)
        if result.get("hint"):
            print(f"What to do: {result['hint']}", file=sys.stderr)
        return 1
    print(fmt(result) if fmt else result.get("message", ""))
    return 0


# ---------------------------------------------------------------- setup wizard

def run_setup(vault: Path) -> int:
    from .config import Settings, save_groq_key
    from .paths import VaultPaths
    paths = VaultPaths(vault)
    paths.ensure_dirs()
    s = Settings.load(paths)
    print("Creator Brain setup. Press Enter to keep the value in [brackets].\n")
    print("1) Groq key (free at console.groq.com) gives fast audio transcription and claim extraction.")
    key = _ask_secret("Groq API key")
    if key:
        save_groq_key(key)
        print("   Saved outside the vault (~/.cbrain/secrets.json), so sharing the vault never leaks it.")
    print("\n2) Your own proxies (optional): a text file with one proxy URL per line.")
    pf = _ask("Proxy file path", s.proxy_file)
    if pf and not Path(pf).expanduser().exists():
        print(f"   Note: {pf} does not exist yet. Saved anyway.")
    s.proxy_file = pf
    s.use_free_proxies = _ask_yes_no("3) Also use free public proxies (checked and rotated automatically)?", s.use_free_proxies)
    s.local_whisper = _ask_yes_no("4) Use local Whisper when Groq is unavailable (needs `pip install faster-whisper`)?", s.local_whisper)
    s.save(paths)
    print(f"\nSettings saved in {paths.config}.\nNext: brain add @SomeCreator")
    return 0


# ---------------------------------------------------------------- dispatch

def _parse_sets(pairs: list[str]) -> dict[str, Any]:
    from .service import PROFILE_ALIASES, parse_scalar
    out: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise ValueError(f"'{p}' is not KEY=VALUE")
        k, v = p.split("=", 1)
        k = k.strip().lower().replace(" ", "_")
        k = PROFILE_ALIASES.get(k, k)
        if k in ("injuries", "equipment") and "," in v:
            out[k] = [x.strip() for x in v.split(",") if x.strip()]
        elif k in ("age", "weight", "days_per_week"):
            out[k] = parse_scalar(v)
        else:
            out[k] = v.strip()
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):      # transcripts contain emoji; never crash a legacy Windows console
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    args = build_parser().parse_args(argv)
    vault = Path(args.vault).expanduser() if args.vault else default_vault()

    if args.cmd == "setup":
        return run_setup(vault)
    if args.cmd == "worker":
        from . import worker
        return worker.main(["--vault", str(vault)])
    if args.cmd == "mcp":
        from . import mcp_server
        return mcp_server.main(["--vault", str(vault)])

    from .service import Brain
    brain = Brain(vault)
    j = getattr(args, "json", False)

    if args.cmd == "init":
        print(f"Vault ready at {brain.paths.root}\nOpen that folder in Obsidian, then run: brain add @SomeCreator"
              + (f"\n(note: {brain.init_warning})" if brain.init_warning else ""))
        return 0
    if args.cmd == "add":
        return _emit(brain.add_creator(args.handle, full=args.full, shorts=args.shorts), j)
    if args.cmd == "update":
        return _emit(brain.update(args.creator), j)
    if args.cmd == "status":
        return _emit(brain.status(args.creator), j, _fmt_status)
    if args.cmd == "search":
        return _emit(brain.search(args.query, args.creator, args.limit), j, _fmt_search)
    if args.cmd == "topic":
        return _emit(brain.topic(args.name), j, _fmt_topic)
    if args.cmd == "compare":
        return _emit(brain.compare(args.topic, args.creator or []), j, _fmt_compare)
    if args.cmd == "profile":
        try:
            updates = _parse_sets(args.sets) if args.sets else None
        except ValueError as e:
            print(f"Problem: {e}\nWhat to do: use --set goal=\"build muscle\"", file=sys.stderr)
            return 2
        return _emit(brain.profile(updates), j, _fmt_profile)
    if args.cmd == "doctor":
        return _emit(brain.doctor(fix=args.fix), j, _fmt_doctor)
    if args.cmd == "pause":
        return _emit(brain.pause(), False)
    if args.cmd == "resume":
        return _emit(brain.resume(), False)
    if args.cmd == "retry":
        return _emit(brain.retry_parked(args.creator), False)
    if args.cmd == "rebuild":
        return _emit(brain.rebuild(merge=not args.no_merge), j)
    if args.cmd == "graph":
        from .livegraph import serve
        serve(brain.paths.root, port=args.port, open_browser=not args.no_open)
        return 0
    if args.cmd == "merge-topics":
        res = brain.merge_topics(dry_run=args.dry_run)
        if not j and isinstance(res, dict) and res.get("groups"):
            for g, ms in list(res["groups"].items())[:40]:
                print(f"  {g}  <-  {', '.join(m for m in ms if m != g)}")
        return _emit(res, j)
    if args.cmd == "dates":
        return _emit(brain.backfill_dates(args.creator), False)
    if args.cmd == "more":
        ids = [i for i in (args.ids or "").split(",") if i.strip()] or None
        res = brain.add_videos(args.creator, match=args.match, video_ids=ids, top=args.top, dry_run=args.dry_run)
        if not j and isinstance(res, dict):
            for v in res.get("videos", [])[:20]:
                print(f"  - {v['title']}  {v['url']}")
        return _emit(res, j)
    return 2


if __name__ == "__main__":
    sys.exit(main())
