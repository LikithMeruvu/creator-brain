"""Vault layout. All file locations come from here."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


def default_vault() -> Path:
    return Path(os.environ.get("CBRAIN_VAULT", Path.home() / "CreatorBrain")).expanduser()


def safe_filename(text: str, max_len: int = 110) -> str:
    """Windows/macOS/Linux-safe note name; keeps spaces so Obsidian titles read naturally."""
    text = re.sub(r'[<>:"/\\|?*#^\[\]\x00-\x1f]', "", text).strip().rstrip(".")
    text = re.sub(r"\s+", " ", text)
    return (text[:max_len].rstrip() or "untitled")


@dataclass(frozen=True)
class VaultPaths:
    root: Path

    # hidden engine room (Obsidian ignores dot-folders)
    @property
    def brain(self) -> Path: return self.root / ".brain"
    @property
    def jobs_db(self) -> Path: return self.brain / "jobs.sqlite"
    @property
    def search_db(self) -> Path: return self.brain / "search.sqlite"
    @property
    def claims(self) -> Path: return self.brain / "claims.jsonl"
    @property
    def topics_json(self) -> Path: return self.brain / "topics.json"
    @property
    def conflicts(self) -> Path: return self.brain / "conflicts.json"
    @property
    def transcripts(self) -> Path: return self.brain / "transcripts"
    @property
    def visual(self) -> Path: return self.brain / "visual"
    @property
    def pending_extraction(self) -> Path: return self.brain / "pending_extraction"
    @property
    def proxies_json(self) -> Path: return self.brain / "proxies.json"
    @property
    def config(self) -> Path: return self.brain / "config.json"
    @property
    def manifest(self) -> Path: return self.brain / "manifest.json"
    @property
    def writer_lock(self) -> Path: return self.brain / "writer.lock"
    @property
    def pause_flag(self) -> Path: return self.brain / "PAUSED"
    @property
    def log(self) -> Path: return self.brain / "worker.log"

    # visible notes
    @property
    def home(self) -> Path: return self.root / "Home.md"
    @property
    def me(self) -> Path: return self.root / "Me.md"
    @property
    def agents_md(self) -> Path: return self.root / "AGENTS.md"
    @property
    def creators(self) -> Path: return self.root / "creators"
    @property
    def topics(self) -> Path: return self.root / "topics"
    @property
    def outputs(self) -> Path: return self.root / "outputs"
    @property
    def obsidian(self) -> Path: return self.root / ".obsidian"

    def transcript(self, video_id: str) -> Path:
        return self.transcripts / f"{video_id}.json.gz"

    def creator_dir(self, creator_name: str) -> Path:
        return self.creators / safe_filename(creator_name)

    def creator_note(self, creator_name: str) -> Path:
        return self.creator_dir(creator_name) / f"{safe_filename(creator_name)}.md"

    def videos_dir(self, creator_name: str) -> Path:
        return self.creator_dir(creator_name) / "videos"

    def updates_dir(self, creator_name: str) -> Path:
        return self.creator_dir(creator_name) / "updates"

    def topic_note(self, topic: str) -> Path:
        return self.topics / f"{safe_filename(topic)}.md"

    def ensure_dirs(self) -> None:
        for d in (self.brain, self.transcripts, self.visual, self.pending_extraction,
                  self.creators, self.topics, self.outputs):
            d.mkdir(parents=True, exist_ok=True)
