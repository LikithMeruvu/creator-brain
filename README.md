# Creator Brain

Turn any YouTube creator into a **brain**: an Obsidian vault of linked notes that any AI agent
(Claude Code, Codex, Cursor, Gemini CLI…) can build, search and keep up to date.

```bash
uv tool install creator-brain        # (once published) — for now: uv pip install -e .
brain setup                          # optional: Groq key, proxy list, free proxies, local Whisper
brain add @JeremyEthier              # newest 30% + top 10 winners
brain add @JeremyEthier --full       # every video
brain update                         # pull in new videos (also runs automatically once a day)
brain search "how many sets per week"
```

Open the vault folder (`~/CreatorBrain` by default) in Obsidian to watch the brain grow in the graph view.

- **Fast:** many fetch lanes in parallel — your own IP (paced) plus verified proxies.
- **Never stuck:** captions → Groq Whisper → local Whisper; durable SQLite queue resumes after any crash.
- **Accurate:** every piece of advice carries an exact quote that code checks against the transcript.
- **Agent-ready:** MCP server (`brain-mcp`), CLI, and an `AGENTS.md` rulebook inside the vault.

## Connect it to your AI tool (MCP)

The MCP server is `brain-mcp` (stdio). Point your harness at it:

```bash
# Claude Code
claude mcp add creator-brain --scope user -- brain-mcp
# Codex CLI (~/.codex/config.toml)
#   [mcp_servers.creator-brain]
#   command = "brain-mcp"
```

Cursor / Gemini CLI / others: add an MCP server named `creator-brain` with command `brain-mcp`.
Set `CBRAIN_VAULT` if your vault is not `~/CreatorBrain`, and `GROQ_API_KEY` (or run `brain setup`) for
claim extraction and the audio fallback. Without Groq, transcripts and search still work, and your own agent
can extract claims from `.brain/pending_extraction/`.

Tools: `add_creator`, `update_creators`, `brain_status`, `search_brain`, `get_topic`, `compare_creators`,
`my_profile`, `save_output`, `suggest_creators`, `brain_doctor`. A new-video check runs at most once a day,
in the background, on the first tool call.

## How it stays fast and does not break

| Part | How |
|---|---|
| Transcripts | lane 0 = your IP (paced ~240/h), more lanes through verified proxies; captions → Groq Whisper → local Whisper |
| Queue | SQLite job per video; a crash or reboot resumes exactly where it stopped; bad videos park with a reason |
| Extraction | Groq models rotated, each with its own free quota; follows Groq's rate-limit headers; daily caps defer, never fail |
| Accuracy | every claim's quote is matched against the transcript by code (numbers and "not" must match exactly) |
| Many tools at once | one writer lock per vault; any number of readers |

See [SPEC.md](SPEC.md) for the architecture.
