# Creator Brain — build spec (contract for all modules)

Goal: `brain add @JeremyEthier` turns a YouTube creator into an **Obsidian vault** of linked notes that any AI
agent (Claude Code, Codex, Cursor, Gemini CLI…) can build, search and keep updated through one MCP server, a CLI,
and a plain `AGENTS.md`. Fast (parallel lanes), never stuck (fallback ladder, durable queue), accurate (every claim
is verified against the exact transcript words).

**Shared code you must use, not change:** `cbrain/models.py`, `cbrain/paths.py`, `cbrain/config.py`, `cbrain/jobs.py`.
If you need a change there, describe it in your final report instead of editing.

## Global rules for every module
- Python 3.11, stdlib + `yt-dlp`, `aiohttp`, `aiohttp-socks`, `mcp`. Optional: `faster-whisper` (import lazily).
- Groq is called over plain HTTPS (OpenAI-compatible `https://api.groq.com/openai/v1/...`) with aiohttp/urllib — no SDK.
- **Never** send cookies, logins or API keys through a proxy. Proxies are only for public YouTube caption/audio/page requests. TLS verification stays on.
- Every file write that Obsidian may see is **atomic**: write `name.tmp` then `os.replace`.
- Tests: `pytest`, **no real network** (mock yt-dlp, aiohttp, Groq). Run with `.venv/Scripts/python -m pytest tests/<your files> -q`.
- Windows-first (paths with spaces, `\r\n` tolerant), but must also run on macOS/Linux.
- Keep it lightweight: no new heavy dependencies. Clear names, short functions, docstrings only where non-obvious.

## Pipeline
```
discover (yt-dlp flat list) → select (recent 30% + top 10 winners; --full = all; skip Shorts/lives)
  → JobStore queue (priority: winners first, then newest)
  → fetch lanes in parallel (lane 0 = own IP paced; lanes 1..N = proxies)   [state queued→fetching→fetched]
       ladder per video: captions(own IP or proxy) → Groq Whisper audio → local Whisper → park
       + SponsorBlock cut + chapters
  → vault writer: video note + search index                                     [fetched→written]
  → thinker: extract claims (Groq) → verify quotes (code) → topic notes, change alerts [written→extracting→done]
  → update note per run, creator note + Home dashboard refreshed
```

## Module ownership and interfaces

### A. Fetch — `cbrain/discovery.py`, `cbrain/fetch/{__init__,errors,captions,asr,sponsorblock,runner}.py`, `cbrain/legacy_import.py`
```python
# discovery.py
def list_channel(handle_or_url: str, limit: int | None = None) -> tuple[Creator, list[VideoMeta]]
    # yt-dlp Python API, extract_flat on https://www.youtube.com/@handle/videos (newest first).
    # Creator.slug = slugify(channel name); fills channel_id, name, handle. VideoMeta.published may be None
    # in flat mode -> fill later from full info at fetch time.
def latest(handle_or_url: str, n: int = 15) -> list[VideoMeta]          # cheap daily check (RSS is dead: 404)
def select(videos: list[VideoMeta], scope: Scope, recent_fraction=0.30, top_winners=10,
           include_shorts=False, include_lives=False) -> dict[str, float]   # id -> priority (winners highest)

# fetch/errors.py
class FetchError(Exception): ...
class Blocked(FetchError): ...        # IP/proxy rate-limited or bot-checked: try another lane, do not count attempt
class NoCaptions(FetchError): ...     # go down the ladder to audio
class Retryable(FetchError): ...      # network hiccup: count attempt, retry later
class Permanent(FetchError): ...      # private/removed/members-only: park immediately

# fetch/captions.py
def fetch_captions(video_id: str, proxy: str | None = None, timeout: int = 15) -> tuple[Transcript, dict]
    # yt-dlp extract_info(download=False, proxy=proxy or "", no cookies, no config files), choose best json3 track
    # (manual en > auto en/en-orig > other), parse json3 -> Segments. Returns (transcript, info) where info has
    # title, upload_date->published "YYYY-MM-DD", duration, view_count, chapters, description.
    # Reuse logic ideas from the old project: C:/Last D Backup/creator DB/creator_brain/subtitles.py (read-only).
# fetch/asr.py
def transcribe(video_id: str, settings: Settings, proxy: str | None = None) -> Transcript
    # download smallest audio (e.g. format 139 / worstaudio) via yt-dlp to a temp dir, then Groq
    # /audio/transcriptions (verbose_json, segment timestamps) with settings.asr_model; split if >24 MB
    # (ffmpeg only if present). Fallback to faster-whisper if settings.local_whisper. Honour Groq 429 retry-after.
# fetch/sponsorblock.py
def sponsor_spans(video_id: str) -> list[tuple[float, float]]   # sponsor.ajay.app API; [] on any error/404
def cut(transcript: Transcript, spans) -> Transcript             # drop segments mostly inside spans, record removed_spans
# fetch/runner.py
class FetchRunner:
    def __init__(self, store: JobStore, paths: VaultPaths, settings: Settings, pool: "ProxyPool | None",
                 on_fetched: Callable[[VideoRow, Transcript, dict], None]): ...
    def run(self, stop: threading.Event | None = None, max_videos: int | None = None) -> dict  # stats
    # Lanes as threads: lane 0 own IP with settings.own_ip_interval_seconds pacing; lanes 1..lanes-1 each
    # acquire a proxy from pool (skip if pool None/empty). Each lane loops store.claim(); ladder per video;
    # Blocked -> release proxy as blocked, store.defer(video, 30) and lane takes a new proxy (own-IP lane
    # backs off 15 min); success -> save gz JSON to paths.transcript(id), store.mark(FETCHED, source),
    # call on_fetched. Respect paths.pause_flag (stop claiming). Stop when queue has no due work.
    # Whisper upgrade: for top `settings.whisper_upgrade_top` videos by views, if source is auto_caption and a
    # Groq key exists, also run asr and keep the better (SOURCE_RANK).
# legacy_import.py
def import_old_creator_brain(old_db: Path, store: JobStore, paths: VaultPaths, creator: Creator) -> int
    # Read-only import of transcripts already fetched by the old project
    # (C:/Last D Backup/claude code/creators/knowledge.db + archive). Inspect its schema read-only.
    # Saved as Transcript(source="legacy_import" or original source), state FETCHED. Returns count.
```

### B. Proxy pool — `cbrain/proxies.py`
```python
FREE_SOURCES: list[str]   # raw GitHub lists: TheSpeedX/PROXY-List (http/socks4/socks5.txt), monosans/proxy-list
                          # (proxies/*.txt), proxifly/free-proxy-list (proxies/protocols/*/data.txt)
class ProxyPool:
    def __init__(self, paths: VaultPaths, settings: Settings): ...
    def refresh(self, target_healthy: int = 60, force: bool = False) -> dict
        # user proxies (settings.proxy_file) always first; free lists only if settings.use_free_proxies.
        # async check (aiohttp + aiohttp_socks, concurrency 400, timeout 10s): GET https://www.youtube.com/
        # through proxy, TLS verified, healthy if 200 and body > 2KB. Stream through shuffled candidates and stop
        # early once target_healthy reached. Persist to paths.proxies_json with score/last_ok/fails.
        # Skip if refreshed < 30 min ago unless force. Real measured success: ~4.5% alive, ~20% of those fetch captions.
    def acquire(self) -> str | None      # best healthy proxy not currently leased (thread-safe), user proxies preferred
    def release(self, proxy: str, ok: bool, blocked: bool = False) -> None
        # ok -> score up; fail -> fails+1; 2 consecutive fails or blocked -> evict. Auto-refresh in background
        # thread when healthy count < 10.
    def stats(self) -> dict
```

### C. Vault — `cbrain/vault/{__init__,writer,index,graph}.py`
```python
# writer.py
def brain_label(creator: Creator, counts: dict, updated: str) -> str
    # "jeremy-ethier · recent · updated 2026-10-29 · 96 videos"
class VaultWriter:
    def __init__(self, paths: VaultPaths): ...
    def ensure_vault(self) -> None   # folders, Home.md, Me.md template, AGENTS.md (from template), .obsidian graph config
    def write_video_note(self, creator: Creator, meta: VideoMeta, transcript: Transcript,
                         topics: list[str] | None = None, on_screen: list[tuple[float, str]] | None = None) -> Path
        # path: creators/<Name>/videos/<YYYY-MM-DD> <Title>.md ; frontmatter: type: video, id, creator ([[Name]]),
        # title, published, views, duration, url, transcript_source, removed_sponsor_seconds, tags.
        # Body: link to [[Creator]], "Topics:" [[Topic]] links, then transcript grouped under "## [mm:ss] Chapter"
        # headings (chapters, else every ~2 minutes), each paragraph ends with a [▶ mm:ss](url&t=) link.
    def write_creator_note(self, creator: Creator, counts: dict, top_topics: list[str], recent_updates: list[str]) -> Path
        # frontmatter type: creator, slug, brain label, channel_id, scope, last_checked_at; links to top videos/topics/updates
    def write_topic_note(self, topic: str, claims: list[Claim], aliases: list[str], conflicts: list[dict]) -> Path
        # "## <Creator>" sections with "- claim — “quote” [▶ mm:ss](url) ([[video note]])", newest first, outdated
        # ones struck/marked; "## Where creators agree/disagree"; frontmatter type: topic, aliases (Obsidian aliases!)
    def write_update_note(self, creator: Creator, date: str, new_videos: list[VideoMeta],
                          new_claims: list[Claim], changed: list[dict]) -> Path
        # creators/<Name>/updates/<Name> - Update <YYYY-MM-DD>.md (append if same day)
    def write_output(self, title: str, markdown: str, sources: list[str]) -> Path   # outputs/<date> <title>.md
    def write_home(self, creators: list[Creator], counts_by_creator: dict[str, dict], topics: list[str]) -> Path
        # dashboard table: creator | brain label | % built | failed | last update ; plus Creators.base (Obsidian Bases)
    def video_note_path(self, creator: Creator, meta: VideoMeta) -> Path
# index.py
class SearchIndex:   # SQLite FTS5 at paths.search_db, bm25 ranking, porter tokenizer
    def __init__(self, db_path: Path): ...
    def index_transcript(self, creator: Creator, meta: VideoMeta, transcript: Transcript) -> int
        # chunks of 45–90 s at segment boundaries (respect chapters), store creator slug/name, video id, title,
        # published, t_start, t_end, text, url (with &t=). Re-indexing a video replaces its chunks.
    def index_claims(self, claims: list[Claim]) -> None
    def search(self, query: str, creators: list[str] | None = None, limit: int = 8, kind: str = "any") -> list[dict]
        # kind: "chunks" | "claims" | "any"; return dicts {creator, video_id, title, published, t, url, text, score, kind}
        # Escape FTS syntax safely; fall back to OR of terms when the AND query finds nothing.
    def remove_video(self, video_id: str) -> None
    def stats(self) -> dict
# graph.py
def write_obsidian_config(paths: VaultPaths) -> None
    # .obsidian/graph.json colour groups: creators blue (path:creators -path:videos), topics green, videos grey,
    # outputs orange; don't overwrite user edits if file exists (merge colorGroups only)
```

### D. Thinker — `cbrain/think/{__init__,llm,extract,verify,topics,changes,pipeline}.py`
```python
# llm.py
class NoLLM(Exception): ...
class LLMClient:
    def __init__(self, settings: Settings): ...       # raises NoLLM on use if no groq key
    def available(self) -> bool
    def complete_json(self, system: str, user: str, max_tokens: int = 4000) -> dict | list
        # Groq chat completions, response_format json_object, temperature 0.1, retries with backoff on 429/5xx
    def estimate_cost_usd(self, input_chars: int) -> float   # rough, conservative; documented constants
# extract.py
def extract_claims(creator: Creator, meta: VideoMeta, transcript: Transcript, llm: LLMClient) -> list[Claim]
    # split transcript into ~6000-char windows with [mm:ss] markers; prompt asks for practical advice/facts/
    # protocols/warnings with an EXACT quote (6-40 words) copied from the window, its timestamp and a short
    # topic title (Title Case, reuse known topics passed in prompt). Windows run in parallel (ThreadPool 4).
def write_agent_batch(paths, creator, meta, transcript) -> Path
def ingest_agent_batch(paths, file: Path) -> list[Claim]
    # No-Groq fallback: write .brain/pending_extraction/<id>.md with instructions + transcript for the harness
    # agent; the agent writes <id>.claims.json next to it; ingest reads + returns claims (then verified).
# verify.py
def normalize(text: str) -> str      # lowercase, strip punctuation, collapse whitespace, unify quotes/apostrophes
def verify_claims(claims: list[Claim], transcript: Transcript) -> tuple[list[Claim], list[Claim]]
    # kept, rejected. Quote must appear in normalized transcript text (window search across segment joins);
    # set claim.t to the start of the segment where the match begins; claim.verified=True. Quotes < 5 words rejected.
# topics.py
class TopicRegistry:   # persisted in paths.topics_json: {canonical: {"aliases": [...], "claims": [ids]}}
    def __init__(self, paths: VaultPaths): ...
    def canonical(self, topic: str) -> str    # alias/normalized/plural/simple-synonym merge; new topics registered
    def add_claims(self, claims: list[Claim]) -> dict[str, list[Claim]]   # canonical topic -> claims
    def all_topics(self) -> list[str]
    def aliases(self, topic: str) -> list[str]
# changes.py
def detect_changes(topic: str, claims: list[Claim], llm: LLMClient | None) -> list[dict]
    # same creator, same topic, different dates: ask LLM (if available) whether newer contradicts older.
    # returns [{"creator","topic","old": claim_dict, "new": claim_dict, "summary"}]. No LLM -> [].
# pipeline.py
class Thinker:
    def __init__(self, store: JobStore, paths: VaultPaths, settings: Settings, writer: VaultWriter, index: SearchIndex): ...
    def process(self, row: VideoRow) -> list[Claim]   # extract -> verify -> append claims.jsonl -> topics -> topic notes
    def run(self, stop=None, max_videos=None) -> dict # claims WRITTEN jobs -> EXTRACTING -> DONE; parallel 4
    def estimate(self, creator_slug: str | None = None) -> dict   # pending videos, chars, est. USD
```

### E. Receptionist — `cbrain/service.py`, `cbrain/worker.py`, `cbrain/cli.py`, `cbrain/mcp_server.py`, `cbrain/doctor.py`, `cbrain/templates/AGENTS.md`
```python
# service.py  — the one façade CLI and MCP both call. All methods return JSON-able dicts and never block on fetching.
class Brain:
    def __init__(self, vault: Path | None = None): ...      # VaultPaths(default_vault()), Settings.load, ensure_vault
    def add_creator(self, handle: str, full: bool = False, shorts: bool = False) -> dict
        # list_channel -> select -> store.add_videos -> legacy import if old DB has this channel -> start worker
        # returns {brain: label, queued, total_on_channel, message}
    def update(self, creator: str | None = None) -> dict    # latest(n=15) per creator (full list if scope=full and gap>15)
    def maybe_daily_check(self) -> dict | None             # call at the start of every MCP tool; non-blocking (thread)
    def status(self, creator: str | None = None) -> dict   # counts, % built, brain label, worker running?, proxy stats
    def search(self, query: str, creators: list[str] | None = None, limit: int = 8) -> dict
        # include "brain_completeness" so agents can say "64% built"
    def topic(self, name: str) -> dict                     # canonical topic note content + claims
    def compare(self, topic: str, creators: list[str]) -> dict
    def profile(self, updates: dict | None = None) -> dict # read/update Me.md frontmatter (goal, level, injuries, equipment, days)
    def save_output(self, title: str, markdown: str, sources: list[str]) -> dict
    def suggest_creators(self, topic: str, n: int = 5) -> dict   # yt-dlp "ytsearch30:<topic>" grouped by channel
    def pause(self) / resume(self) / retry_parked(self, creator=None)
    def doctor(self, fix: bool = False) -> dict
# worker.py
def start_background(paths: VaultPaths) -> bool   # spawn detached `python -m cbrain.worker --vault <root>` if not locked
def main() -> int   # holds WriterLock; release_stale_leases; pool.refresh in thread; FetchRunner and Thinker run
                    # concurrently; on_fetched writes video note + index; writes update notes + creator notes + Home
                    # at end; logs to paths.log; exits when no due work.
# doctor.py
def check(paths, store, index, fix=False) -> dict
    # broken [[links]], orphan notes, video notes missing for FETCHED+ rows, transcripts missing for notes,
    # unverified claims, parked videos (with reasons), stale creators (> daily_check_hours), duplicate topics,
    # stale leases, schema/manifest version. fix=True repairs what it safely can (re-render, reindex, unpark).
# cli.py   `brain init | setup | add <handle> [--full] [--shorts] | update [creator] | status [creator] |
#          search <q> [--creator X] | topic <name> | doctor [--fix] | pause | resume | retry | worker | mcp`
#          `setup` asks: Groq key (saved via config.save_groq_key), proxy file, free proxies y/n, local whisper y/n.
# mcp_server.py  FastMCP("creator-brain") tools: add_creator, update_creators, brain_status, search_brain,
#          get_topic, compare_creators, my_profile, save_output, suggest_creators, brain_doctor. Each tool first
#          calls brain.maybe_daily_check(). Tool descriptions tell the agent to cite timestamp URLs.
```

### F. Eyes (optional) — `cbrain/visual.py`
```python
def thumbnail(video_id: str, paths: VaultPaths) -> Path | None   # i.ytimg.com maxres -> hq fallback, cached
def storyboard_frames(video_id: str, proxy: str | None = None, every_seconds: int = 30) -> list[tuple[float, bytes]]
    # yt-dlp info formats sb0 (320x180 mosaics); download fragments, crop tiles with stdlib-free approach?
    # -> return mosaic images + per-tile timestamps (cropping only if Pillow is installed; else send whole mosaic)
def describe(frames, settings: Settings) -> list[tuple[float, str]]   # Groq vision model; [] if no key
```

## Vault layout (Obsidian)
```
CreatorBrain/
  AGENTS.md  Home.md  Me.md  Creators.base
  creators/<Name>/<Name>.md  creators/<Name>/videos/*.md  creators/<Name>/updates/*.md
  topics/*.md   outputs/*.md
  .obsidian/graph.json
  .brain/ jobs.sqlite search.sqlite claims.jsonl topics.json transcripts/*.json.gz proxies.json config.json
          manifest.json worker.log pending_extraction/ visual/
```
