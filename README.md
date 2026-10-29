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

See [SPEC.md](SPEC.md) for the architecture.
