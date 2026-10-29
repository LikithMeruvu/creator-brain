"""MCP server (stdio). One tool per thing an agent needs; every tool first runs the non-blocking daily check."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:  # mcp 2.x renamed it
    from mcp.server.mcpserver import MCPServer as _Server  # type: ignore[no-redef]

INSTRUCTIONS = (
    "Creator Brain is a local library of YouTube creators' transcripts (an Obsidian vault). "
    "Always search_brain BEFORE answering a question about what a creator says; never answer from memory. "
    "Cite the video title and the timestamp URL of every point you use, and quote creators only with words from the results. "
    "Call my_profile first to personalise advice (goal, level, injuries, equipment, days per week) and ask the user for any "
    "missing field. Tell the user how complete the brain is (brain_completeness / percent_built) when it is below 100%. "
    "Save finished plans and answers with save_output so they land in the vault linked to their sources."
)

Getter = Callable[[], Any]


def create_server(get_brain: Getter) -> Any:
    """Build the server. `get_brain` is called lazily on each tool call so importing never touches disk."""
    mcp = _Server("creator-brain", instructions=INSTRUCTIONS)

    def call(method: str, *args: Any, **kwargs: Any) -> dict:
        brain = get_brain()
        note = None
        try:
            note = brain.maybe_daily_check()
        except Exception:  # noqa: BLE001 - the daily check must never break a tool
            pass
        result = getattr(brain, method)(*args, **kwargs)
        if note and isinstance(result, dict) and "error" not in result:
            result["daily_check"] = note.get("message", "started")
        return result

    @mcp.tool()
    def add_creator(handle: str, full: bool = False) -> dict[str, Any]:
        """Add a YouTube creator (handle like @JeremyEthier, or channel URL) to the brain and start building it.
        Returns quickly: the video list is queued and transcripts download in the background, so you can search
        what is ready right away. full=True fetches every video instead of the newest 30% plus the top winners.
        Tell the user the returned brain label and that more material keeps arriving."""
        return call("add_creator", handle, full=full)

    @mcp.tool()
    def add_videos(creator: str, match: str | None = None, video_ids: list[str] | None = None,
                   top: int | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Add more of a creator's videos to their brain. By default a brain holds the newest 30% plus top winners;
        every other upload is listed in the creator's '<Name> - All Videos' note. Pick by title words (match=
        'protein diet'), explicit video_ids, or top=N most-viewed. Use dry_run=True first to show the user the
        matches, then call again to add them. They download and get analysed in the background."""
        return call("add_videos", creator, match=match, video_ids=video_ids, top=top, dry_run=dry_run)

    @mcp.tool()
    def update_creators(creator: str | None = None) -> dict[str, Any]:
        """Check one creator (or all of them) for new videos and queue anything new. Cheap and quick; it also
        runs automatically about once a day. New material appears in search as it finishes processing."""
        return call("update", creator)

    @mcp.tool()
    def brain_status(creator: str | None = None) -> dict[str, Any]:
        """How complete is the brain? Returns the brain label, percent built (searchable) and analysed,
        whether the background worker is running, failures with reasons and a rough ETA. Use it to tell the
        user how much of a creator is ready before relying on search results."""
        return call("status", creator)

    @mcp.tool()
    def search_brain(query: str, creators: list[str] | None = None, limit: int = 8) -> dict[str, Any]:
        """Search creators' transcripts and verified claims (call this BEFORE answering from memory).
        Returns short snippets, each with a timestamp URL: cite the video title and that URL for every point.
        Includes brain_completeness per creator: if it is below 100%, tell the user results may be missing videos.
        Use plain words, e.g. 'protein per day for muscle gain'; narrow with creators=['jeremy-ethier']."""
        return call("search", query, creators=creators, limit=limit)

    @mcp.tool()
    def get_topic(name: str) -> dict[str, Any]:
        """Everything the brain has verified about one topic (e.g. 'Protein Intake') across all creators:
        claims with exact quotes and timestamp URLs, newest first. Names and synonyms are matched loosely.
        Cite the timestamp URLs when you use a claim."""
        return call("topic", name)

    @mcp.tool()
    def compare_creators(topic: str, creators: list[str]) -> dict[str, Any]:
        """Compare what several creators say about a topic: per creator, their claims (with quotes and timestamp
        URLs) plus matching transcript passages. Use it to show where creators agree or disagree, and cite both sides."""
        return call("compare", topic, creators)

    @mcp.tool()
    def my_profile(updates: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read the user's profile (Me.md): goal, level, age, weight, injuries, equipment, days_per_week, diet, notes.
        Read it before giving advice so the answer is personalised, and ask the user for fields that are empty.
        Pass updates={'goal': 'build muscle', 'days_per_week': 4} to save new facts the user tells you."""
        return call("profile", updates)

    @mcp.tool()
    def save_output(title: str, markdown: str, sources: list[str] | None = None) -> dict[str, Any]:
        """Save a finished plan, summary or answer into the vault (outputs/) so the user can open it in Obsidian.
        Always save plans with this tool. Put the timestamp URLs / video titles you relied on in sources."""
        return call("save_output", title, markdown, sources or [])

    @mcp.tool()
    def suggest_creators(topic: str) -> dict[str, Any]:
        """Find YouTube channels worth adding for a topic (ranked by views in search results, excluding creators
        already in the brain). Show the list to the user and call add_creator only for the ones they choose."""
        return call("suggest_creators", topic)

    @mcp.tool()
    def brain_doctor(fix: bool = False) -> dict[str, Any]:
        """Check the vault for problems (broken links, missing notes, failed videos, stale creators) and,
        with fix=True, repair what is safe. Run it if search looks wrong or status shows failures."""
        return call("doctor", fix=fix)

    return mcp


_BRAIN: Any = None
_VAULT: Path | None = None


def get_brain() -> Any:
    global _BRAIN
    if _BRAIN is None:
        from .service import Brain
        _BRAIN = Brain(_VAULT)
    return _BRAIN


def main(argv: list[str] | None = None) -> int:
    global _VAULT
    ap = argparse.ArgumentParser(prog="brain-mcp", description="Creator Brain MCP server (stdio)")
    ap.add_argument("--vault", default=None)
    args = ap.parse_args(argv)
    _VAULT = Path(args.vault).expanduser() if args.vault else None
    create_server(get_brain).run()   # stdio
    return 0


if __name__ == "__main__":
    sys.exit(main())
