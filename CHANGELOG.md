# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres loosely to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(loose because there's no published API surface yet — `[Unreleased]` will accumulate
breaking changes freely until a `1.0.0` release).

## [Unreleased]

### Added
- Startup health check that probes the configured LM Studio endpoint(s)
  once at boot. On success, logs the count of loaded models plus the
  first few names — instant visibility into whether the configured
  `chat_model` / `embed_model` / `vision_model` are actually present
  under those names. On failure (timeout, connection error, API error),
  logs a clear WARNING naming the likely cause. Non-blocking — bot still
  starts and connects to IRC even if LM Studio is down. Probes vision
  endpoint separately only when it's at a different `base_url` than chat.

### Changed
- `start.bat` now invokes the venv's `python.exe` directly rather than
  whatever `python` is on the system PATH. Bot runs in its own isolated
  dependency set regardless of shell activation state.
- Goodbye broadcast: tightened `GOODBYE_SYSTEM` prompt to default to
  speaking. Silence is now reserved for the rare "no real-user activity
  in the buffer" case rather than the broad "feels weird to interrupt"
  escape that local Qwen 3-class models were taking ~100% of the time.
- Goodbye broadcast: silence outcome bumped from DEBUG to INFO so the
  per-channel result is visible at default log level.

### Docs
- README setup section: explicit venv creation step (Windows + Unix
  invocations). Run section: documents both activated and direct-invocation
  paths.

## [0.1.0] - 2026-05-20

Initial public release. Feature-complete bot lifecycle: connects, lives,
acts, and disconnects politely.

### Added

- **Engagement.** Connect to any IRC server (TLS optional), join multiple
  channels, reply to mentions / DMs / `/me` actions. All public messages
  (including actions) persisted to SQLite.
- **Quakenet support.** Q AUTH on connect, optional `+x` user-mode masking,
  account-based authorisation (operator accounts in config).
- **Tool-using agent loop.** Hard step caps, wall-clock budgets,
  hallucinated-tool recovery, bad-args recovery, loop detector, polite
  out-of-budget summary. Per-call HTTP timeout decoupled from per-turn
  budget. `<think>...</think>` stripping for thinking-mode models.
- **Per-channel policy.** `chatty | quiet | worker | locked` mode, persona,
  allowed IRC-native actions, tick interval, auto-recall opt-in, etc.
- **Long-term memory.** Embedding-based extractor every N messages,
  cosine top-K recall, channel-scoped, dedup on insert. `recall`,
  `remember`, `forget` tools. `!memory_stats` chat command.
- **Initiative ticks.** Per-channel coroutine for unprompted chime-ins
  in `chatty` channels. Multi-layer necropost defense (buffer-window
  filter, min-fresh-count, newest-message-age gate), robust
  silence detection.
- **Reminders.** `set_reminder` tool; reminder loop polls and posts.
- **Graceful shutdown.** Operator `!quit [parting message]` command;
  SIGINT/SIGTERM/KeyboardInterrupt routed through the same path; grace
  period for in-flight reply turns; per-channel in-character goodbyes
  via LLM call before IRC QUIT; on-exit token-usage summary.
- **Tools (15 total).**
  - Tier 1 (info): `web_search` (Brave/DDG/Tavily with fallback),
    `fetch_url`, `wikipedia`, `youtube_info`, `log_search`.
  - Tier 2 (compute/perception): `calc`, `unit_convert`, `look_at_image`.
  - Tier 4 (memory): `recall`, `remember`, `forget`, `set_reminder`.
  - Tier 5 (IRC-native): `me_action`, `set_topic`, `private_msg`
    (rate-limited 3/min/target).
- **Token-usage tracking.** Per-call rows in SQLite with `purpose` and
  `channel` attribution. `!quit` and clean shutdowns print a session +
  lifetime summary broken down by purpose and model.
