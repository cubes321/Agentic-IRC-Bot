# Agentic IRC Bot

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)
![Status: pre-1.0](https://img.shields.io/badge/status-pre--1.0-orange.svg)

A from-scratch agentic IRC bot in Python. The LLM is the orchestrator: it
decides which tools to call, when to call them, and how to combine results
into a reply. The bot has its own initiative (chimes in unprompted in
"chatty" channels), long-term semantic memory (extracted every 20 messages,
recalled on demand or implicitly each reply), and a graceful operator
shutdown flow that says a personality-matched goodbye in each active channel
before disconnecting.

Designed for use with **LM Studio** (or any OpenAI-compatible endpoint),
with first-class support for **Quakenet** authentication.

## What it does

**Engagement**
- Connects to any IRC server (TLS optional), joins multiple channels.
- Quakenet Q AUTH on connect; optional `+x` user mode to mask the hostname.
- Replies when mentioned in a channel, when DMed, or when addressed via a
  `/me` action (e.g. `/me pokes BotNick`).
- All public-channel messages — including `/me` actions — persisted to
  SQLite for the recent buffer, memory extractor, and `log_search`.

**Tool-using agent loop** (`bot/agent.py`)
- Hard step cap (default 6 per reply turn) and configurable wall-clock budget.
- Hallucinated-tool-name recovery, bad-JSON-args recovery, same-call-twice
  loop detector, polite "out of budget" summary if the cap is hit.
- Per-call HTTP timeout decoupled from the per-turn wall budget.
- Concurrency cap on outbound chat-completion calls, shared by reply turns,
  extractor, ticks, and goodbyes.
- `<think>...</think>` thinking-mode blocks stripped from all LLM output
  before posting (for Qwen 3-style reasoning models).

**Per-channel policy** (`bot/policy.py`, `[channels."#name"]` in config)
- Mode: `chatty | quiet | worker | locked`.
- Persona, allowed IRC-native actions (`me`, `topic`, `msg`), tick interval,
  auto-summarise links toggle, auto-welcome toggle.
- Auto-recall: opt-in implicit memory recall on every reply turn (prepends
  top-K matching memories to the system context, no LLM tool call needed).
- Operator account list (`[operator].accounts`) grants admin everywhere.

**Long-term memory** (`bot/memory.py`)
- Embeddings via a configured embed model (e.g. `text-embedding-nomic-embed-text-v1.5`).
- Auto-extractor runs every N public messages per channel, pulls memorable
  facts from the chat, embeds and dedups them.
- `recall(query, k)` and `remember(content, kind)` tools for the LLM.
- `forget(memory_id)` tool with safety: refuses cross-channel deletes,
  echoes the deleted content back so users can spot mistakes.
- Cosine top-K with similarity floor; channel-scoped (per-channel privacy).
- `!memory_stats` chat command shows per-channel counts + breakdown.

**Initiative ticks** (`bot/scheduler.py`)
- One coroutine per chatty channel; tick interval configurable per channel.
- Multi-layer necropost defense: SQL freshness window, minimum recent-message
  count, max age of newest message, `<silent>` opt-out.
- Robust silence detection (case-insensitive, HTML-escape tolerant,
  bare-word "silent").
- Token usage attributed by purpose (reply / tick / extractor / goodbye)
  for visibility into where the bot's spend goes.

**Reminders** (`bot/scheduler.py` + `set_reminder` tool)
- LLM-callable: `set_reminder(when, target_nick, message)`.
- `when` accepts ISO-8601 or relative (`5m`, `2h`, `1d`, `1h30m`).
- Reminder loop polls the table every 10s (configurable), posts due
  reminders to the channel.

**Background tasks** (`bot/tasks.py`)
- `!task <goal>` schedules a multi-step background task. 30-step /
  30-minute budgets; uses the full tool catalog; cancellable via
  `!cancel <id>`; listable via `!tasks`.
- Auth gated per-channel by `task_issuers` (`all | ops | operator`).
- Persisted to the `tasks` table (status lifecycle: pending → running →
  done / failed / cancelled). Restart-safe: any task left in `running`
  from a previous bot lifetime gets marked `cancelled` with
  `[interrupted by restart]` on next boot.
- Shutdown cancels any running tasks with a brief channel notice before
  the bot disconnects.

**Direct-message gate** (`bot/ircclient.py`, `[dm]` config)
- DMs are private and invisible to channel ops, so an open DM channel is
  a foot-gun: random users can DM the bot and trigger LLM/tool calls
  with no oversight. The `[dm].mode` policy gates engagement at the
  source account level:
  - `ignore`: silently drop every DM
  - `operators` (default): only `[operator].accounts` may DM
  - `allowlist`: operators + explicit `[dm].allowed_accounts`
  - `all`: anyone (legacy / public-help-bot use case)
- Identity is the sender's services account, never their nick. Users
  without an account can't pass any mode except `all`.
- Dropped DMs are logged at INFO so the operator can see who's trying;
  the sender gets no confirmation, so spammers get nothing to game.

**Graceful shutdown** (`bot/main.py`, `bot/ircclient.py`)
- Operator-only `!quit [parting message]` IRC command.
- SIGINT/SIGTERM (Unix) and KeyboardInterrupt (Windows) route through the
  same shutdown flow.
- Grace period for in-flight reply turns to finish posting.
- Per-channel in-character goodbye via LLM call (concurrent, capped wall
  budget) before the IRC QUIT.
- Token usage summary printed on exit.

## Tools

| Tool             | Tier | Notes                                                   |
|------------------|:----:|---------------------------------------------------------|
| `web_search`     | 1    | Brave / DuckDuckGo / Tavily with configurable fallback  |
| `fetch_url`      | 1    | readability-lxml extraction, first 4k chars             |
| `wikipedia`      | 1    | REST API; search fallback on 404; disambiguation aware  |
| `youtube_info`   | 1    | yt-dlp metadata                                         |
| `log_search`     | 1    | LIKE over message_log, channel-scoped                   |
| `calc`           | 2    | Safe arithmetic via AST walk (no `eval`)                |
| `unit_convert`   | 2    | pint; handles offset units (°C/°F) correctly            |
| `look_at_image`  | 2    | Vision call; degrades gracefully if model not loaded    |
| `recall`         | 4    | Cosine top-K over channel memories                      |
| `remember`       | 4    | Explicit fact storage (extractor also runs every 20 msg)|
| `forget`         | 4    | Delete by id; refuses cross-channel deletes             |
| `set_reminder`   | 4    | Persists to DB; reminder loop fires                     |
| `me_action`      | 5    | `/me` action; gated by `allow_actions = ["me", ...]`    |
| `set_topic`      | 5    | TOPIC command; needs bot ops or `-t` channel mode       |
| `private_msg`    | 5    | DM to a user; rate-limited 3/min/target                 |

Tools are gated per channel:
- Capability flags (`vision`, `memory`, `brave_key`, `tavily_key`) filter
  out tools whose dependencies aren't configured.
- `action:*` requirements check the channel's `allow_actions` list.

## Setup

Requires Python 3.11+.

```bash
# 1. Create a virtual environment (recommended; isolates the bot's deps
#    from your global Python install).
python -m venv .venv

# 2. Activate it.
#    Windows PowerShell:  .\.venv\Scripts\Activate.ps1
#    Windows cmd:         .\.venv\Scripts\activate.bat
#    Unix / macOS:        source .venv/bin/activate

# 3. Install dependencies into the venv.
pip install -r requirements.txt

# 4. Create your local config.
cp sample-config.toml config.toml
# edit config.toml — at minimum: [server].nick, channels, [ai].chat_model,
# and (if using Quakenet) [server.quakenet].q_account + q_password_file
```

Once the venv exists, `start.bat` (Windows) invokes the venv's Python
directly, so you don't need to activate it every time you start the bot.

**Windows path note:** in TOML basic strings, backslashes are escape
characters. For a Windows-style path like `E:\…\qpass.txt`, either:
- Use single quotes (TOML literal string): `q_password_file = 'E:\path\qpass.txt'`
- Or use forward slashes in double quotes: `q_password_file = "E:/path/qpass.txt"`

In LM Studio:
1. Load a chat model that supports tool calling. Verified to work well:
   `qwen3-class`, `qwen2.5-7b-instruct`, `qwen2.5-14b-instruct`,
   `llama-3.3-70b-instruct`, `hermes-3-llama-3.1`. Smaller / older models
   often emit malformed tool calls — the agent recovers, but quality drops.
2. (Recommended) Load an embed model such as
   `text-embedding-nomic-embed-text-v1.5` to enable long-term memory.
3. (Optional) Load a vision model such as `qwen2.5-vl-7b-instruct` to
   enable `look_at_image`. The tool gracefully reports `vision_disabled`
   if the vision model isn't loaded at call time.
4. Start the local server (default `http://localhost:1234/v1`).

## Run

If the venv is activated:

```bash
python -m bot.main config.toml          # normal
python -m bot.main -v config.toml       # DEBUG to console; file always gets DEBUG
```

If you'd rather not activate, invoke the venv's Python directly:

```bash
# Windows
.\.venv\Scripts\python.exe -m bot.main config.toml
# or just:
start.bat

# Unix / macOS
./.venv/bin/python -m bot.main config.toml
```

`start.bat` is the recommended launcher on Windows — it uses the venv's
Python without changing the current shell's state, so the bot runs in
isolation even if you forgot to `Activate.ps1`.

The bot logs connection progress, channel joins, every engagement decision,
every tool call, every initiative tick decision (speak / silent / skipped),
and the token-usage summary on exit.

## Operating

- **Mention or DM** the bot to engage normally. DM access is gated by
  the `[dm]` config policy — by default, only accounts listed in
  `[operator].accounts` can DM the bot. See the "Direct messages" section
  below.
- **`!memory_stats`** in any channel: prints per-channel memory totals,
  kinds breakdown, top users.
- **`!task <goal>`**: schedule a multi-step background task. The bot will
  work on it using its full tool catalog and a 30-step / 30-minute budget,
  then post the result back to the channel with a `[task #N]` prefix.
  Authorised per `[channels."#name"].task_issuers` (`all | ops | operator`).
  Example: `!task search for recent reviews of the Framework Laptop 13 and summarise the consensus`.
- **`!cancel <id>`**: cancel a running task. Allowed for the task's
  original issuer (by account) or any operator. The agent finishes its
  current step before the cancellation lands, so allow up to ~one LLM
  call duration for the `[task #N] cancelled` line to appear.
- **`!tasks`**: list recent tasks in the current channel (any status,
  most recent first) with id, state, age, owner, and goal preview.
- **`!quit [parting message]`** (operator only): graceful shutdown.
  Operator status is bot-wide (account in `[operator].accounts`); channel
  ops are NOT sufficient. Sends an in-character goodbye in each active
  channel before disconnecting. Any running tasks get cancelled with a
  brief interruption notice posted to their channels.
- **Ctrl+C / SIGINT / SIGTERM**: same graceful path, no parting message.

## Quick smoke test

In any channel the bot joins:

```
<MyBot> what's 17 * 23?                         → calc
<MyBot> what's 80F in celsius?                  → unit_convert
<MyBot>: search the web for "rust async 2025"   → web_search
<MyBot> summarise https://example.com/article   → fetch_url
<MyBot> who is Ada Lovelace?                    → wikipedia
<MyBot> remind me in 5m to feed the cat         → set_reminder
<MyBot> what did Alice say about pizza?         → log_search (channel-scoped)
```

Each one exercises a different tool path. Watch the bot's console — it
logs every tool call and result.

## Configuration cheatsheet

The full schema is in [`sample-config.toml`](sample-config.toml). Highlights:

- `[ai]`: `chat_model`, `embed_model` (enables memory), `vision_model`
  (enables `look_at_image`), `vision_base_url` (optional override).
- `[tools]`: `search_provider`, `search_fallback`, `brave_api_key`,
  `tavily_api_key`.
- `[operator].accounts`: Q-account names with admin override everywhere
  (issuing tasks, shutting down).
- `[budgets]`: `reply_step_cap`, `reply_wall_sec`, `llm_call_timeout_sec`,
  `max_concurrent_chat_calls`. Lower these aggressively if your local
  model is slow.
- `[scheduler]`: `reminder_poll_sec`, `tick_jitter_pct`,
  `min_tick_interval_sec`, `tick_skip_if_idle_sec`,
  `tick_skip_if_last_message_older_than_sec` (necropost defense).
- `[shutdown]`: `quit_message`, `grace_sec`, `goodbye_recent_sec`,
  `goodbye_timeout_sec`.
- `[dm]`: direct-message policy. `mode` (`ignore` | `operators` (default) |
  `allowlist` | `all`) and `allowed_accounts` (for `allowlist` mode). See
  "Direct messages" below.
- `[memory].extractor_batch_size`: how often to fire the memory extractor
  (every N public messages per channel).
- `[channels."#name"]`: per-channel `mode`, `persona`, `task_issuers`,
  `allow_actions`, `tick_interval_sec`, `auto_summarise_links`,
  `auto_welcome`, `auto_recall`, `auto_recall_k`.
- `[ignore].nicks`: nicks to hard-ignore (default: `Q`, `ChanServ`,
  `NickServ`).

## Architecture quick map

```
bot/main.py           # entry point: builds deps, wires shutdown event
bot/ircclient.py      # pydle subclass; routes events, on_ctcp_action for /me
bot/agent.py          # LLM tool-call loop with all defensive features
bot/scheduler.py      # initiative ticks, reminder firing, shutdown goodbyes
bot/tasks.py          # TaskRunner: !task / !cancel / !tasks lifecycle
bot/memory.py         # MemoryStore (embed + recall) + MemoryWriter (extractor)
bot/auth.py           # Quakenet account-based authorisation + op tracking
bot/policy.py         # per-channel ChannelPolicy wrapper
bot/db.py             # aiosqlite wrapper + schema
bot/config.py         # pydantic models + TOML loader
bot/prompts.py        # every system prompt, in one place for easy tuning
bot/tools/__init__.py # self-registering tool registry + capability filter
bot/tools/web.py      # web_search with Brave/DDG/Tavily backends
bot/tools/url.py      # fetch_url with readability extraction
bot/tools/wiki.py     # wikipedia
bot/tools/youtube.py  # youtube_info (yt-dlp)
bot/tools/log_search.py
bot/tools/calc_tool.py# safe arithmetic via AST walk (no eval-of-strings)
bot/tools/unit_convert.py
bot/tools/vision.py   # look_at_image (gated by vision capability)
bot/tools/memory_tools.py  # recall / remember / forget / set_reminder
bot/tools/irc_native.py    # me_action / set_topic / private_msg (Tier-5)
```

## Notes for local models

The agent loop is defensive on purpose — local models drop tool calls,
hallucinate tool names, and occasionally loop. The default budgets (6 steps
per reply, 90s wall) are calibrated for that. If your model is solid,
raise `budgets.reply_step_cap` and let the agent take more turns; if it's
shaky, drop it and watch the recovery messages do their job.

If a model emits no tool calls at all when asked for facts, that usually
means tool calling isn't supported by the runtime or the model wasn't
fine-tuned for it. Try a different model from the verified list above.

**Known limitation: `me_action` spontaneous use.** Small local models have a
deep IRC training prior that says actions are wrapped in `*asterisks*` in
chat text, and they default to that pattern instead of calling the
`me_action` tool — even with explicit anti-pattern guidance in the prompt.
The tool works correctly when explicitly invoked; the issue is purely
behavioural. See the V2 deferred list in the design doc.

**Thinking-mode models (Qwen 3, etc).** If you're using a model with a
`<think>...</think>` reasoning block, the bot's `_strip_thinking` handles
removal correctly — but every tick / reply / extractor / goodbye call pays
the thinking tokens. Watch the on-exit usage summary's by-purpose
breakdown; if `tick` or `extractor` rows are unexpectedly large, disable
thinking mode server-side for cheaper batch operations.
