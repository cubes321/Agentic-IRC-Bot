# Design: `!tell` — leave a message for an absent user

**Date:** 2026-08-27
**Status:** approved, not yet implemented
**Slice:** post-v1 small addition

## Problem

Someone wants to say something to a user who is not around. Today the only
options are to wait, to `/msg` them and hope their client has offline
storage (it doesn't), or to say it into the channel and hope they scroll
back. The bot is already sitting in the channel watching every join and
every line; it is the obvious place to hold the message and hand it over
when the person turns up.

## Summary

Add a channel-scoped, presence-triggered message relay:

- **Leave one** with `!tell <nick> <message>`, or by asking the bot in
  conversation (a `leave_message` LLM tool).
- **Delivered** the first time that person is seen active in the same
  channel — either they JOIN, or they say something (including a `/me`).
- **Delivered publicly**, in-channel, attributed and time-stamped:
  `dave: [from alice, 3h ago] check the logs before you push`
- **Manageable**: `!tells` lists what you have pending, `!untell <id>`
  cancels one.

Identity matching is hybrid — nick *or* Quakenet account — so `dave`
returning as `dave_` still gets his message.

## Decisions and rationale

### Both a command and a tool

`!tell` is dispatched early in `on_message`, alongside `!task` / `!tasks` /
`!cancel`. It never touches the LLM: it is instant, costs zero tokens, and
works in `quiet` and `worker` channels where the bot does not engage. This
is the path people will actually use.

The `leave_message` tool exists separately so that "tell dave I said hi
when he's back" works conversationally, in keeping with the bot's agentic
design. Both write through the same `TellStore.create()`, so they cannot
drift apart in behaviour or in enforcement.

### Delivery on first activity, not on join alone

Join-only delivery misses the common case where the recipient was already
sitting in the channel, idle, when the tell was left — for them the trigger
would never fire. Speech-only delivery misses lurkers who rejoin and read
without talking. Firing on whichever comes first covers both.

Three hooks are needed, not two:

| Hook | Site | Case covered |
|---|---|---|
| `on_join` | `ircclient.py:164` (currently an empty stub) | they reconnect |
| `on_message` | after the `db.log_message` block, `ircclient.py:263` | they were idle and spoke |
| `on_ctcp_action` | `ircclient.py:366` | they `/me`'d |

The third is easy to overlook. Pydle routes IRC actions to
`on_ctcp_action`, never through `on_message` — the file's own comment
already notes that without that handler actions are invisible to the bot.
A `/me waves` is unambiguously "back", so it must count.

### Delivered in-channel, publicly

Matches IRC norms and the bot's channel-scoped privacy model — memories,
`message_log` and policy are all per-channel already. It also keeps the
bot from becoming an unlogged whisper relay, which is the shape this
feature would take if it delivered by DM: a mild but real harassment
vector, and one where nobody else in the channel can see what the bot is
being used for.

Consequence, stated plainly so it is not a surprise: **a tell is not
private.** The whole channel sees it. That is intended.

### Hybrid nick + account matching

The bot resolves nick to Quakenet account via WHOIS and `account-notify`
(`bot/auth.py`) and already handles `rename()` on nick change. Nick-only
matching, the classic IRC memo behaviour, silently fails for `dave_`,
`dave|afk`, or a renamed dave — the message sits in the table until it
expires and nobody learns why.

Account-only matching is stronger but unusable at creation time: the bot
cannot WHOIS an offline nick, so it would have to refuse `!tell dave ...`
whenever dave is not already cached. That is a bad first-use experience.

So: store the nick as typed, and *opportunistically* store the account too
if that nick happens to be in the auth cache when the tell is left. Match
on either at delivery time.

### Own module and own table

`bot/tells.py` owns the `tells` table and nothing else. This follows the
one-file-per-feature shape of `memory.py` and `tasks.py`, and keeps
`ircclient.py` (already 833 lines) from growing more than two thin hooks.

Rejected: extending the `reminders` table with a `trigger_kind` column. A
tell is presence-triggered, a reminder is clock-triggered; they share no
firing logic, only a superficial resemblance. Bolting a second, unrelated
firing path onto a table whose poll loop is clock-driven would leave
`fire_at` meaning "fire time" on some rows and "expiry" on others.

Rejected: reusing the generic `open_threads` table. Zero schema change,
but all the querying — match by nick OR account, count pending per
sender-to-recipient pair — would become JSON-poking in SQL instead of
indexed columns.

## Data model

Added to `SCHEMA` in `bot/db.py`:

```sql
CREATE TABLE IF NOT EXISTS tells (
  id            INTEGER PRIMARY KEY,
  channel       TEXT NOT NULL,
  from_nick     TEXT NOT NULL,
  from_account  TEXT,
  from_key      TEXT NOT NULL,        -- account, else 'nick:<lower>' — see below
  to_nick       TEXT NOT NULL,        -- as typed, for display
  to_nick_lower TEXT NOT NULL,        -- matching key
  to_account    TEXT,                 -- resolved at creation if known
  message       TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  delivered_at  TEXT                  -- NULL = pending
);
CREATE INDEX IF NOT EXISTS idx_tells_pending ON tells(channel, to_nick_lower, delivered_at);
CREATE INDEX IF NOT EXISTS idx_tells_account ON tells(channel, to_account, delivered_at);
CREATE INDEX IF NOT EXISTS idx_tells_from ON tells(channel, from_key, delivered_at);
```

**`from_key` — one column that makes three queries exact.** The sender's
identity is "account if they have one, else nick", the same fallback
`set_reminder` uses for its rate-limit key. Storing that resolved key
once, at creation, turns the per-pair cap, `!tells`, and the `!untell`
ownership check into plain indexed equality tests. Without it each of the
three needs a `(from_account = ? AND from_account IS NOT NULL) OR
(from_account IS NULL AND lower(from_nick) = ?)` clause — three chances
to get the same tricky predicate subtly different. The `nick:` prefix
keeps the namespaces from colliding: account `alice` and nick `alice`
must not be treated as the same person.

`CREATE TABLE IF NOT EXISTS` means no migration entry is required; the
existing `bot.sqlite` picks the table up on next open, same as every
other table in `SCHEMA`.

**Soft delete.** Delivery sets `delivered_at` rather than deleting the
row, mirroring the `memories.deleted_at` decision from security review
M3. It gives a record of what the bot relayed to whom, and lets `!tells`
report "delivered 20m ago" instead of the entry silently vanishing.
Delivered and expired rows are pruned by a retention sweep.

**Matching query:**

```sql
SELECT * FROM tells
WHERE channel = ? AND delivered_at IS NULL AND expires_at > ?
  AND (to_nick_lower = ? OR (to_account IS NOT NULL AND to_account = ?))
ORDER BY created_at ASC
```

## Module interface — `bot/tells.py`

```python
class TellStore:
    def __init__(self, db: Database, cfg: Config) -> None: ...

    async def create(self, *, channel, from_nick, from_account,
                     to_nick, to_account, message) -> TellResult
    async def pending_for(self, channel, nick, account) -> list[Row]
    async def list_from(self, channel, actor_nick, actor_account) -> list[Row]
    async def cancel(self, tell_id, actor_nick, actor_account,
                     is_operator) -> TellResult
    async def deliver(self, bot, channel, nick, account) -> int
    async def prune(self) -> int
```

`deliver()` takes the bot as an argument rather than holding a reference,
matching how `ToolContext` passes `bot` for the IRC-native tools.
`TellStore` therefore depends only on `db` and `cfg`, and can be tested
against a temporary SQLite file with no IRC machinery at all.

Wired up in `main.py` next to the existing stores, and attached to the
bot the same way `task_runner` and `memory_writer` already are.

## Validation and limits

All enforced inside `TellStore.create()`, never at the dispatch sites, so
the `!tell` path and the `leave_message` tool path cannot diverge.

| Rule | Value | Enforcement |
|---|---|---|
| Channel has tells enabled | `allow_tell` | `ChannelCfg` lookup |
| Tells per actor per hour | 5 | in-memory, `set_reminder`'s pattern + periodic sweep |
| Pending per sender-recipient pair | 5 | **DB `COUNT`** |
| Pending per recipient, all senders | 15 | DB `COUNT` |
| Message length | 400 chars | truncate-and-tell, refuse if empty |
| Expiry | 30 days | `expires_at` at creation |
| Delivered per arrival | 3 | `deliver()` |

**Why the per-pair cap is durable and the hourly one is not.**
`set_reminder`'s rate limiter is in-memory, and its comment openly accepts
that a determined attacker can spam through a restart — acceptable for a
reminder a user sets for themselves. It is not acceptable for the one
limit standing between a user and a queue of unwanted messages from
someone else. The per-pair and per-recipient caps are a `COUNT` on an
indexed column, which is cheap enough to do on every create.

**Actor key** for the hourly limit: the same `from_key` described above,
produced by a single module-level `actor_key(nick, account)` helper that
the store, the commands and the tool all share.

**Rate-limit state lives on the `TellStore` instance**, not in module
globals as `set_reminder`'s does. The app builds exactly one store, so
behaviour is identical — but a test can construct a fresh store per case
instead of reaching into another module's globals to reset them. Same
reasoning for the delivery lock.

**Target nick validation:**

1. Strip leading `#`, `&`, `+`, `!` and whitespace — the fix
   `set_reminder` needed in security review L5, where the model would
   occasionally pass a channel name as `target_nick`.
2. Require an RFC 2812-shaped nick: a letter or one of the special
   characters `[ ] \ ` _ ^ { | }` first, then up to 31 more of those
   plus digits and `-`.
3. Refuse the bot's own nick.
4. Allow self-tells. Harmless, and occasionally a useful note-to-self.

Without rule 2, `!tell hi there everyone` files a tell for a user called
"hi" and nobody notices.

**Overflow on delivery.** More than three pending produces the first
three plus `(+N more, next time you're around)`; the remainder are
delivered on their next activity. `irc_send` already takes a `max_lines`
cap and returns a wire-line count, which is exactly the excess-flood
guard needed here — the same mechanism that fixed the task-result flood
in the most recent commit.

**Wire safety needs no new code.** `irc_send` splits on newlines and
filters to `c.isprintable() or c == " "`, so relayed user text cannot
carry a newline or control character onto the wire. Relayed content goes
out through `irc_send` like everything else and inherits that.

**Audit.** Both creation and delivery write to the existing `audit`
table. A tell is the bot posting a message on someone else's behalf,
which is the same category as `private_msg` — already audited.

## Delivery concurrency

A JOIN and a first message can arrive milliseconds apart, and both hooks
fire independently. A single module-level `asyncio.Lock` wraps the whole
*query pending, post, mark delivered* sequence, so the second event finds
nothing pending.

`delivered_at` is set **after** a successful `irc_send`, not before.
A send failure therefore leaves the row pending and it retries on the
recipient's next activity — the same "leave the row, retry later" choice
`_fire_reminder` makes at `scheduler.py:553`. A duplicated tell is
annoying; a silently lost one defeats the entire feature.

## Commands

```
!tell <nick> <message>   leave a message
!tells                   list the tells you have pending in this channel
!untell <id>             cancel one you left
```

All channel-only, consistent with `!task` / `!tasks` / `!cancel`. All
accept the bot-nick prefix form (`CubesBot: !tell dave hi`) via the
existing `cmd_text` normalisation. `!untell` is ownership-checked: you
may cancel your own tells; operators may cancel any.

Bare `!tell` and bare `!untell` print a usage hint rather than falling
through to engagement — the pattern the existing task commands use.

## LLM tool

`bot/tools/tell.py`, Tier-4, registered alongside the memory tools:

```
leave_message(target_nick: str, message: str)
```

No `action:` capability tag — the per-channel gate is `allow_tell`,
checked inside `TellStore.create()`, so the tool returns a clear refusal
in a channel with tells disabled rather than being absent from the
catalog. The tool description warns against passing a channel name as
`target_nick`, the same defensive wording `set_reminder` carries.

Rate limits are attributed to the *actor* who triggered the reply turn,
not to the bot, so a prompt-injected instruction to leave tells is capped
by the injecting user's own hourly budget.

## Configuration

```toml
[channels."#geeks"]
allow_tell = true    # default; set false to disable !tell in this channel
```

`ChannelCfg` is a Pydantic model with defaulted fields, so a new
defaulted boolean is backward-compatible — existing `config.toml` and
`config-england.toml` keep working with no edit. Documented in
`sample-config.toml`.

`allow_tell` gates **creation only**. Turning it off stops new tells but
still delivers ones already pending, so switching the feature off cannot
strand messages people have already left. `!tells` and `!untell` keep
working in a disabled channel for the same reason.

## Files touched

| File | Change |
|---|---|
| `bot/tells.py` | new — `TellStore`, the table's only owner |
| `bot/tools/tell.py` | new — `leave_message` tool, registered in `tools/__init__.py` |
| `tests/test_tells.py` | new — first test file in the repo |
| `bot/db.py` | `tells` table + indexes appended to `SCHEMA` |
| `bot/config.py` | `allow_tell: bool = True` on `ChannelCfg` |
| `bot/ircclient.py` | three delivery hooks; three command handlers + dispatch |
| `bot/scheduler.py` | call `TellStore.prune()` from the existing retention cycle |
| `bot/main.py` | construct `TellStore`, attach to bot |
| `bot/tools/__init__.py` | import the new tool module |
| `requirements.txt` | `pytest`, `pytest-asyncio` |
| `sample-config.toml`, `README.md`, `CHANGELOG.md` | docs |

## Failure handling

- Every IRC hook wraps its `TellStore` call so a DB error cannot break
  `on_join` / `on_message` / `on_ctcp_action`. This feature must never
  be able to take the event loop down.
- Send failure at delivery: row stays pending, logged, retried on next
  activity.
- Validation and rate-limit refusals return a specific reason. No silent
  drops — a user who cannot leave a tell is told why.
- Malformed command: usage hint.
- Retention sweep prunes delivered rows older than 30 days and expired
  undelivered rows, run from the existing scheduler prune cycle that
  already handles reminder retention.

## Testing

`pytest` and `pytest-asyncio` added to `requirements.txt`, and a `tests/`
directory created. This is the repo's first test suite; `TellStore` is
the right place to start one because it is pure logic over SQLite with
no IRC dependency — testable against a temp DB file without mocking
pydle.

`tests/test_tells.py` covers:

- hybrid matching: nick exact, nick case-insensitive, account match with
  a changed nick, no match
- channel scoping: a tell left in `#a` never delivers in `#b`
- each limit at its boundary: 5th create allowed and 6th refused,
  per-pair cap, per-recipient cap, message length, nick validation table
- expiry: an expired tell is never delivered
- `!untell` ownership: owner yes, stranger no, operator yes
- delivery: marks `delivered_at`, is not re-delivered, overflow past 3
- the double-delivery race: two concurrent `deliver()` calls yield one
  delivery

The three IRC hooks, the LLM tool, and `allow_tell = false` are verified
manually against a live server, since they need real pydle events.

## Explicitly out of scope

- `!seen <nick>` — a natural companion, but a distinct feature with its
  own privacy questions (it turns the bot into a record of who is active
  when). Its own slice, if wanted.
- Cross-channel tells. Channel-scoped, deliberately.
- DM delivery. See the rationale above.
- Editing a pending tell. Cancel and re-send.

## Documentation

- `README.md`: a "Tell relay" section alongside Reminders.
- `CHANGELOG.md`: slice entry.
- `sample-config.toml`: `allow_tell` documented.
