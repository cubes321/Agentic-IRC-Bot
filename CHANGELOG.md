# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres loosely to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(loose because there's no published API surface yet — `[Unreleased]` will accumulate
breaking changes freely until a `1.0.0` release).

## [Unreleased]

### Fixed
- Command dispatch now strips a leading bot-nick mention before matching.
  Previously `!task X` worked but `CubesBot: !task X` (or any other
  nick-prefixed form, which is the natural IRC habit) fell through to
  the engagement path and got treated as a regular question instead of
  a command. Affects all five commands (`!task`, `!cancel`, `!tasks`,
  `!memory_stats`, `!quit`). Bare forms (`!task` alone) now also
  dispatch and trigger a usage message rather than falling through.

### Added (Slice 2c — Tasks)
- **Multi-step background tasks.** Users can now issue long-running goals
  to the bot via three chat commands:
  - `!task <goal>` — schedule a background task. Auth gated by the
    channel's `task_issuers` policy (`all | ops | operator`). Returns
    immediately with `[task #N] starting: <goal>`; the bot works on it
    in the background.
  - `!cancel <id>` — cancel a running task. Authorised for the task's
    issuer (by account) or any operator. Task posts a cancellation line
    when the agent loop reaches the next step boundary.
  - `!tasks` — list recent tasks for the channel (any status) with id,
    state, age, owner, goal preview.
- `TaskRunner` (new `bot/tasks.py`): owns the task lifecycle. Persists
  every task to the `tasks` table immediately on issuance, transitions
  pending → running → done/failed/cancelled, and posts results back to
  the channel with `[task #N] ...` prefix.
- `AgentCore.run_task_turn()`: new entry point for task-mode agent calls.
  Uses `TASK_SYSTEM` prompt, 30-step / 30-minute budgets (per
  `[budgets].task_step_cap` and `task_wall_sec`), and supports
  cancellation via an `asyncio.Event` checked at every step boundary.
- `_run_loop` cancel-event support: the shared agent loop now honours a
  cancel_event for any caller that wants cooperative interruption.
  Reply turns still don't pass one (no use case); tasks always do.
- Restart safety: on startup, `TaskRunner.startup_cleanup()` marks any
  tasks left in `running` state from a previous bot lifetime as
  `cancelled` with result `"[interrupted by restart]"`. Resuming a
  partial agent loop is unsafe with local models (no determinism, tools
  may have side effects), so the policy is "fail safe, let user re-issue."
- Shutdown safety: `TaskRunner.shutdown_all()` cancels every running task
  during graceful shutdown, with a 10-second grace period for agent
  loops to wind down cleanly before hard-cancelling. Each task posts a
  brief interruption line to its channel before the bot disconnects.

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
