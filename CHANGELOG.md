# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres loosely to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(loose because there's no published API surface yet — `[Unreleased]` will accumulate
breaking changes freely until a `1.0.0` release).

## [Unreleased]

### Security
- **[SEC C1]** SSRF mitigation. `fetch_url`, `look_at_image`, and
  `youtube_info` previously accepted any HTTP(S) URL with no host
  validation and `follow_redirects=True`, letting an LLM-mediated
  request target `127.0.0.1`, RFC 1918 ranges, link-local
  (`169.254.169.254` cloud-metadata), and any other non-globally-routable
  address. Classified as CRITICAL in the 2026-05-22 security review.

  New module `bot/url_safety.py` exposes:
  - `is_safe_url(url)` — verifies scheme is http/https AND every IP the
    host resolves to passes `ipaddress.is_global` (rejects loopback,
    link-local, RFC 1918, RFC 4193, multicast, reserved, unspecified).
    Multi-A-record-safe (rejects if ANY resolved IP is unsafe).
  - `safe_fetch(http, url, ...)` — drop-in replacement for buffered
    `http.request()` with manual redirect following. Re-runs the safety
    check on every hop. Caps redirect chain at 5.
  - `safe_stream(http, url, ...)` — same but for streaming responses
    (used by the vision tool's image cap). Closes and reopens the
    stream on each redirect so the safety check runs in-band.

  Tool changes:
  - `bot/tools/url.py:_fetch_url` — calls `safe_fetch`, catches
    `UnsafeURLError`, returns "refusing to fetch" rather than raising.
  - `bot/tools/vision.py:_fetch_image` — uses `safe_stream`. Same
    catch-and-return shape.
  - `bot/tools/youtube.py:_youtube_info` — pre-validates the URL host
    against an allowlist of YouTube domains (`youtube.com`, `youtu.be`,
    `youtube-nocookie.com` and their subdomains) before handing off to
    yt-dlp's extractor. Rejects boundary-trick subdomains
    (`evil-youtube.com` etc.) via explicit dot-prefix matching.

  Verified with a 23-case smoke test covering loopback IPv4/IPv6,
  hostnames resolving to loopback, all RFC 1918 ranges, AWS metadata
  endpoint, IPv6 link-local, unspecified/multicast, non-HTTP schemes,
  garbage input, and positive cases (real public URLs). YouTube
  allowlist verified against legit YT subdomains and deliberate
  boundary-trick negative cases.

  Residual risk: DNS rebinding (host resolves benignly during
  validation, then re-resolves to internal address during the actual
  connect). Mitigated only by keeping the window small. Documented in
  `bot/url_safety.py` module docstring.

- **[SEC M1]** `private_msg` now requires the actor and target to share
  a channel. Pre-2026-05, once a channel had `allow_actions = ["msg"]`,
  the LLM could DM any nick on the network — the per-target rate limit
  (3/60s) was the only protection, and spraying to many distinct
  targets bypassed it. Combined with prompt injection from fetched
  content (H1), one malicious page could direct the bot to DM-spray
  up to `step_cap` distinct nicks per turn.

  New helper `_shares_channel(bot, actor, target)` walks pydle's
  `bot.channels` and returns True only if both nicks are members of at
  least one channel the bot is in (case-insensitive; defensive against
  pydle data-shape variation). The check fires before the rate-limit
  check (so refusals don't consume rate budget) and before message
  validation (so error returns are fast). The tool description was
  updated so the LLM knows the requirement up front.

  Doesn't fully solve harassment (two users in the same channel can
  still use the bot as an intermediary) but cuts the abuse surface
  from "the entire network" to "channels the actor inhabits" — a
  meaningful collapse. Verified with a 10-case smoke test covering
  matching/non-matching, case-insensitive, multi-channel, plus the
  defensive cases for missing/wrong-shape pydle state.

  Closes review finding M1.

- **[SEC L3]** Redact DM content in INFO-level logs. The `on_message`
  and `on_ctcp_action` engagement paths previously logged the user's
  message text at INFO (e.g. `Engaging in #foo for alice: 'pizza?'`)
  including for DMs — even though DMs are deliberately excluded from
  the SQLite `message_log` table for exactly the same privacy reason.
  Operator sharing a log file for debugging would inadvertently leak
  private user messages. Now: for DMs, the INFO line says
  `<DM redacted>` and the full content is logged at DEBUG only (so
  deliberate `-v` runs can still see it). Channel messages are
  unchanged — those are already public. Closes review finding L3.

- **[SEC H4]** TLS on by default; TLS verification no longer
  force-disabled. The pre-2026-05 config defaults were `port = 6667,
  tls = false` (plaintext IRC), and `bot/main.py` hardcoded
  `tls_verify=False` regardless of config. Result: on Quakenet (and any
  network where Q AUTH is the bot's services credential), the Q
  password was sent in cleartext on every connect, and even a
  hand-rolled TLS connection accepted any presented certificate.
  Classified as HIGH in the 2026-05-22 security review.

  Config changes (`bot/config.py:ServerCfg`):
  - Default `port` is now `6697` (was `6667`).
  - Default `tls` is now `true` (was `false`).
  - New field `tls_verify: bool = True` controls system-trust-store
    cert validation. Replaces the hardcoded `False` previously passed
    to `client.connect(...)`.

  Behaviour at startup (`bot/main.py`):
  - `_warn_insecure_transport(cfg)` runs before any network activity.
    Logs ERROR if `tls=false` AND Q AUTH is configured (cleartext
    services password), WARNING if `tls=false` without Q AUTH (chat
    visible to path), WARNING if `tls=true` but `tls_verify=false`
    (cert-pinning disabled, MITM-trivial). Operators who knowingly run
    plaintext for a local test server see one loud line and proceed.
  - The connect log line now includes `tls_verify=` for visibility.

  **Behaviour change for existing deployments:** if your `config.toml`
  does NOT explicitly specify `[server].port` and `[server].tls`, the
  next start will connect via TLS on 6697 instead of plaintext on 6667.
  Quakenet supports both; the TLS port is recommended and is what other
  modern clients default to. If your IRC server doesn't run TLS, add
  `port = 6667` and `tls = false` to your `[server]` section — the bot
  will warn at startup but still connect.

  Verified manually: a config omitting `port`/`tls`/`tls_verify`
  resolves to `(6697, True, True)`; a config with explicit
  `port = 6667, tls = false` resolves to `(6667, False, True)` and
  the bot logs the appropriate warning. No mid-session reconnect
  behaviour was changed — only the values passed at initial connect.

### Added
- Direct-message policy gate (`[dm]` config section). Channels are public
  but DMs are private and invisible to channel ops — without a gate,
  any IRC user could DM the bot and trigger LLM/tool calls (web_search,
  fetch_url, etc.) with no oversight. Four modes:
  - `ignore`: drop all DMs silently
  - `operators` (default): only `[operator].accounts` can DM
  - `allowlist`: operators + explicit `[dm].allowed_accounts`
  - `all`: pre-2026 behaviour (anyone can DM)

  Identity for the gate is always the sender's services account, never
  their nick (nicks are trivially impersonated). Users without a
  registered account can't pass any mode except `all`. Dropped DMs are
  logged at INFO; the sender gets no response so spammers can't
  fingerprint the bot. Applies to both DM messages and DM `/me` actions.

  **Behaviour change for existing deployments:** the default switches
  from "anyone can DM" to "operators only." If your bot has users who
  legitimately DM it without operator status, set `[dm].mode = "all"`
  or move them to `[dm].mode = "allowlist"` with their accounts in
  `allowed_accounts`.

### Fixed
- Long outbound IRC lines are now word-wrapped instead of hard-truncated
  with an ellipsis. Previously any line over 400 chars (the safe headroom
  cap under IRC's 512-byte wire limit) got cut at the boundary with a
  trailing `…`, discarding real content. Task results frequently contain
  long markdown bullet points (>400 chars per logical line) and the
  truncation lost the substantive end of those bullets. The new behaviour
  uses stdlib `textwrap.wrap()` to split at word boundaries into multiple
  PRIVMSGs, and `irc_send` now accepts a `continuation_prefix` so callers
  (notably the task runner) can keep their visual prefix on every wire
  line of a wrapped block. Task results: continuation prefix is
  `[task #N]   `; reply turns: empty (no prefix shift). Per-call line
  cap: 5 for reply turns (unchanged), 20 for task `_post_result` calls.
- Text-format tool-call leakage: when a local model emits a tool call as
  plain text (Hermes `<tool_call>` XML, Mistral `[TOOL_CALLS]`, Qwen
  flower markers, etc.) instead of via the structured `tool_calls` API,
  the agent loop now detects this, injects a repair message asking the
  model to use the proper mechanism, and continues. The text-format
  garbage is no longer returned to the channel. As a safety net, the
  budget-exhausted summary path also strips any text-tool-call blocks
  before returning (single regex pass; doesn't fire in normal use). Was
  causing tasks to post `<tool_call><function=...>...` as their final
  result on certain Qwen-class models when LM Studio's compat adapter
  failed to convert the model's native format to the OpenAI shape.
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
