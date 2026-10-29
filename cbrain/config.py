"""Settings: .brain/config.json, overridden by CBRAIN_* / GROQ_API_KEY environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

from pathlib import Path

from .paths import VaultPaths


def secrets_path() -> Path:
    """API keys live here, outside the vault, so a shared vault never leaks them."""
    return Path(os.environ.get("CBRAIN_SECRETS", Path.home() / ".cbrain" / "secrets.json"))


def save_groq_key(key: str) -> None:
    p = secrets_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"groq_api_key": key}), encoding="utf-8")


@dataclass
class Settings:
    groq_api_key: str = ""              # optional: audio fallback + claim extraction
    proxy_file: str = ""                # optional: user's own proxy list (one URL per line)
    use_free_proxies: bool = True       # collect + verify public proxies
    local_whisper: bool = False         # use faster-whisper when Groq is missing/limited
    lanes: int = 12                     # parallel fetch lanes (lane 0 = own IP)
    own_ip_interval_seconds: float = 15 # pacing for the own-IP lane (~240 videos/hour)
    proxy_lane_interval_seconds: float = 3
    max_attempts: int = 3               # per video, then PARKED
    recent_fraction: float = 0.30       # default scope: newest 30%...
    top_winners: int = 10               # ...plus the all-time top N by views
    include_shorts: bool = False
    include_lives: bool = False
    sponsorblock: bool = True
    whisper_upgrade_top: int = 10       # re-transcribe this many top winners with Whisper for accuracy
    extract_model: str = "openai/gpt-oss-120b"
    llm_tpm: int = 8000                 # tokens/minute budget until Groq's real limit is learned from headers
    asr_model: str = "whisper-large-v3-turbo"
    vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    visual: bool = False
    cost_confirm_usd: float = 1.0       # ask before an extraction run estimated above this
    daily_check_hours: float = 24

    @classmethod
    def load(cls, paths: VaultPaths) -> "Settings":
        data: dict = {}
        if paths.config.exists():
            data = json.loads(paths.config.read_text(encoding="utf-8"))
        s = cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})
        secrets = secrets_path()
        if secrets.exists():
            s.groq_api_key = json.loads(secrets.read_text(encoding="utf-8")).get("groq_api_key", "")
        if os.environ.get("GROQ_API_KEY"):
            s.groq_api_key = os.environ["GROQ_API_KEY"]
        for f in fields(cls):
            env = os.environ.get(f"CBRAIN_{f.name.upper()}")
            if env is None:
                continue
            if f.type in ("bool", bool):
                setattr(s, f.name, env.lower() in ("1", "true", "yes"))
            elif f.type in ("int", int):
                setattr(s, f.name, int(env))
            elif f.type in ("float", float):
                setattr(s, f.name, float(env))
            else:
                setattr(s, f.name, env)
        return s

    def save(self, paths: VaultPaths) -> None:
        paths.brain.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data.pop("groq_api_key")  # keys live in the environment, never in the vault
        paths.config.write_text(json.dumps(data, indent=2), encoding="utf-8")
