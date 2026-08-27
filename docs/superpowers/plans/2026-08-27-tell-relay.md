# `!tell` Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user leave a message for someone who is absent, which the bot relays publicly in-channel the first time that person is next seen active.

**Architecture:** A new `bot/tells.py` owns a new `tells` SQLite table and every operation on it (create / list / cancel / deliver / prune). `bot/ircclient.py` gains three thin presence hooks (`on_join`, `on_message`, `on_ctcp_action`) and three command handlers; a small `bot/tools/tell.py` exposes the same create path to the LLM. All validation and rate limiting lives inside `TellStore.create()` so the command path and the tool path cannot diverge.

**Tech Stack:** Python 3.11+ (3.13 in the local venv), `aiosqlite`, `pydantic` v2, `pydle`. New dev dependency: `pytest` + `pytest-asyncio` — this is the repo's first test suite.

**Spec:** [`docs/superpowers/specs/2026-08-27-tell-relay-design.md`](../specs/2026-08-27-tell-relay-design.md)

## Global Constraints

- Python 3.11+ — the repo uses `tomllib` and PEP 604 unions; `bot/main.py` enforces this at runtime.
- `from __future__ import annotations` at the top of every new module, matching every existing module in `bot/`.
- No new runtime dependencies. `pytest` and `pytest-asyncio` are test-only and go in `requirements.txt` under a clearly marked test section.
- Everything user-facing goes out through `IRCBot.irc_send()` — never `self.message()` directly. `irc_send` does the control-character stripping and word-wrapping that keeps relayed user text safe on the wire.
- Never raise out of an IRC event hook. A failure in this feature must not be able to break `on_join` / `on_message` / `on_ctcp_action`.
- All timestamps are UTC ISO-8601 strings via `bot.db.now_utc_iso()`, matching every other table.
- `TellStore` must not import `pydle` or `IRCBot`. It takes the bot as a `deliver()` argument, exactly as `ToolContext` passes `bot` to the IRC-native tools. This is what keeps it testable.
- Exact limit values, copied from the spec: 5 tells per actor per hour; 5 pending per sender→recipient pair; 15 pending per recipient; 400-character message cap; 30-day expiry; 3 delivered per arrival.

---

## File Structure

| File | Responsibility |
|---|---|
| `bot/tells.py` | **new.** `TellStore` + `TellResult` + `actor_key()`. Sole owner of the `tells` table. No IRC imports. |
| `bot/tools/tell.py` | **new.** The `leave_message` LLM tool. Thin — argument shaping only; all rules live in `TellStore`. |
| `tests/test_tells.py` | **new.** Unit tests for `TellStore` against a temp SQLite file. |
| `tests/conftest.py` | **new.** Shared `db` / `cfg` / `store` fixtures. |
| `pytest.ini` | **new.** `asyncio_mode = auto` so async tests need no per-test decorator. |
| `bot/db.py` | `tells` table + 3 indexes appended to `SCHEMA`. |
| `bot/config.py` | `allow_tell: bool = True` on `ChannelCfg`. |
| `bot/ircclient.py` | 3 delivery hooks, 3 command handlers, 3 dispatch lines, 1 constructor field. |
| `bot/scheduler.py` | one `TellStore.prune()` call in the existing hourly housekeeping block. |
| `bot/main.py` | construct `TellStore`, assign to `client.tell_store`. |
| `bot/tools/__init__.py` | one import line. |
| `requirements.txt` | test dependencies. |
| `sample-config.toml`, `README.md`, `CHANGELOG.md` | docs. |

**Task order rationale:** Tasks 1–6 build and fully test `TellStore` with no IRC involvement — every one of them is verifiable by running pytest. Tasks 7–9 wire it into the live bot, which can only be verified by hand. Task 10 documents. If you run out of time, stopping after Task 6 leaves a tested, unreferenced module that breaks nothing.

---

## Task 1: Test harness, schema, and config flag

Nothing in this task is the feature itself. It creates the ground the other nine stand on: a working pytest setup, the table, and the channel toggle.

**Files:**
- Create: `pytest.ini`
- Create: `tests/conftest.py`
- Create: `tests/test_tells.py`
- Modify: `requirements.txt`
- Modify: `bot/db.py` (append to the `SCHEMA` string, before the closing `"""`)
- Modify: `bot/config.py:194-208` (`ChannelCfg`)
- Modify: `sample-config.toml`

**Interfaces:**
- Consumes: nothing.
- Produces: a `tells` table; `ChannelCfg.allow_tell: bool = True`; pytest fixtures `db`, `cfg`, `store` (the `store` fixture is added in Task 2, once `TellStore` exists).

- [ ] **Step 1: Add the test dependencies**

Append to `requirements.txt`:

```
# --- test-only (not needed to run the bot) ---
pytest>=8.0
pytest-asyncio>=0.23
```

- [ ] **Step 2: Install them**

```bash
".venv/Scripts/python.exe" -m pip install "pytest>=8.0" "pytest-asyncio>=0.23"
```

Expected: `Successfully installed pytest-8.x pytest-asyncio-0.x` (or "Requirement already satisfied").

- [ ] **Step 3: Create `pytest.ini`**

`asyncio_mode = auto` means async test functions run without a `@pytest.mark.asyncio` decorator on each one. Without it every async test is silently skipped with a warning — a trap worth avoiding on a brand-new suite.

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

**Always invoke the suite as `python -m pytest`, never bare `pytest`.**
With no `tests/__init__.py`, pytest's default import mode puts `tests/` on
`sys.path` but *not* the repo root, so `import bot.db` fails under bare
`pytest`. `python -m pytest` puts the current directory on `sys.path`
first, which is what makes `bot` importable. Every command in this plan
uses that form deliberately.

- [ ] **Step 4: Write the failing test**

Create `tests/test_tells.py`:

```python
"""Unit tests for bot.tells.TellStore."""

from __future__ import annotations

import pytest

from bot.db import Database


async def test_tells_table_exists(db: Database):
    row = await db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tells'"
    )
    assert row is not None, "tells table was not created by SCHEMA"


async def test_tells_table_columns(db: Database):
    rows = await db.fetchall("PRAGMA table_info(tells)")
    names = {r["name"] for r in rows}
    assert names == {
        "id", "channel", "from_nick", "from_account", "from_key",
        "to_nick", "to_nick_lower", "to_account", "message",
        "created_at", "expires_at", "delivered_at",
    }


def test_allow_tell_defaults_true():
    from bot.config import ChannelCfg
    assert ChannelCfg().allow_tell is True
```

- [ ] **Step 5: Create the fixtures**

Create `tests/conftest.py`:

```python
"""Shared fixtures. Each test gets its own throwaway SQLite file, so tests
never interfere with each other or with the real bot.sqlite."""

from __future__ import annotations

import pytest

from bot.config import AICfg, ChannelCfg, Config, ServerCfg
from bot.db import Database


def make_cfg(**channels: ChannelCfg) -> Config:
    """Minimal valid Config. `server.host`, `server.nick` and `ai.chat_model`
    are the only fields without defaults."""
    return Config(
        server=ServerCfg(host="irc.example.org", nick="TestBot"),
        ai=AICfg(chat_model="test-model"),
        channels=dict(channels),
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def cfg() -> Config:
    return make_cfg(**{"#test": ChannelCfg()})
```

- [ ] **Step 6: Run the tests to verify they fail**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v
```

Expected: `test_tells_table_exists` FAILS on `assert row is not None`, `test_tells_table_columns` FAILS on the set comparison, `test_allow_tell_defaults_true` FAILS with `AttributeError: 'ChannelCfg' object has no attribute 'allow_tell'`.

- [ ] **Step 7: Add the table to `SCHEMA`**

In `bot/db.py`, append inside the `SCHEMA` triple-quoted string, after the `audit` index lines and before the closing `"""`:

```sql
-- Presence-triggered message relay (`!tell`). A row is a message left for
-- someone not currently around, delivered the first time they are seen
-- active in the same channel. Soft-deleted on delivery (delivered_at) for
-- the same reason memories are: it leaves a record of what the bot relayed
-- to whom. `from_key` is the sender's resolved identity — their account if
-- they have one, else 'nick:<lowercased nick>' — which makes the per-pair
-- cap, !tells and the !untell ownership check plain indexed lookups.
CREATE TABLE IF NOT EXISTS tells (
  id            INTEGER PRIMARY KEY,
  channel       TEXT NOT NULL,
  from_nick     TEXT NOT NULL,
  from_account  TEXT,
  from_key      TEXT NOT NULL,
  to_nick       TEXT NOT NULL,
  to_nick_lower TEXT NOT NULL,
  to_account    TEXT,
  message       TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  delivered_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tells_pending ON tells(channel, to_nick_lower, delivered_at);
CREATE INDEX IF NOT EXISTS idx_tells_account ON tells(channel, to_account, delivered_at);
CREATE INDEX IF NOT EXISTS idx_tells_from ON tells(channel, from_key, delivered_at);
```

No `_migrate()` entry is needed. `Database.open()` runs every `CREATE TABLE IF NOT EXISTS` in `SCHEMA` on each start, so the existing `bot.sqlite` picks the table up automatically. The `_migrate()` list is only for `ALTER TABLE` on tables that already exist.

- [ ] **Step 8: Add the config flag**

In `bot/config.py`, inside `class ChannelCfg`, after the `auto_recall_k: int = 3` line:

```python
    # Presence-triggered message relay (`!tell`). Gates CREATION only:
    # setting this false stops new tells but still delivers ones already
    # pending, so turning the feature off can never strand a message
    # somebody already left. !tells and !untell keep working too.
    allow_tell: bool = True
```

- [ ] **Step 9: Document the flag**

In `sample-config.toml`, in the per-channel example block alongside `task_issuers`, add:

```toml
# Presence-triggered message relay. When on, anyone in the channel can
# leave a message for an absent user with `!tell <nick> <message>`; the
# bot relays it publicly the first time that user is next seen active
# here (join, message, or /me). Tells are NOT private — the whole
# channel sees them. Set false to disable leaving new tells in this
# channel; any already pending are still delivered.
allow_tell = true
```

- [ ] **Step 10: Run the tests to verify they pass**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v
```

Expected: 3 passed.

- [ ] **Step 11: Commit**

```bash
git add pytest.ini tests/ requirements.txt bot/db.py bot/config.py sample-config.toml
git commit -m "test: add pytest harness; schema + config flag for !tell relay"
```

---

## Task 2: `TellStore.create()` — validation

The store, its result type, the shared `actor_key()` helper, and everything that can make a `create()` call fail on the *shape* of its arguments. Limits come in Task 3.

**Files:**
- Create: `bot/tells.py`
- Modify: `tests/conftest.py` (add the `store` fixture)
- Modify: `tests/test_tells.py`

**Interfaces:**
- Consumes: the `tells` table and `ChannelCfg.allow_tell` from Task 1.
- Produces:
  - `actor_key(nick: str, account: str | None) -> str`
  - `TellResult` dataclass: `ok: bool`, `message: str`, `tell_id: int | None = None`
  - `TellStore(db: Database, cfg: Config)`
  - `TellStore.create(*, channel: str, from_nick: str, from_account: str | None, to_nick: str, to_account: str | None, message: str, bot_nick: str) -> TellResult`
  - Constants `TELL_MAX_MESSAGE_LEN = 400`, `TELL_EXPIRY_DAYS = 30`, `NICK_RE`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tells.py`:

```python
from bot.tells import TELL_MAX_MESSAGE_LEN, TellStore, actor_key


def test_actor_key_prefers_account():
    assert actor_key("Alice", "alice_acct") == "alice_acct"


def test_actor_key_falls_back_to_lowercased_nick():
    assert actor_key("Alice", None) == "nick:alice"


def test_actor_key_namespaces_cannot_collide():
    # Account "alice" and nick "alice" must be different people.
    assert actor_key("bob", "alice") != actor_key("alice", None)


async def test_create_succeeds(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account=None, message="check the logs",
        bot_nick="TestBot",
    )
    assert r.ok is True
    assert r.tell_id is not None


async def test_create_stores_normalised_fields(store: TellStore):
    await store.create(
        channel="#test", from_nick="Alice", from_account="alice_acct",
        to_nick="DaVe", to_account="dave_acct", message="hi",
        bot_nick="TestBot",
    )
    row = await store.db.fetchone("SELECT * FROM tells")
    assert row["to_nick"] == "DaVe"          # preserved for display
    assert row["to_nick_lower"] == "dave"    # lowercased for matching
    assert row["from_key"] == "alice_acct"
    assert row["delivered_at"] is None


# NB: "#geeks" is deliberately NOT in this list. Channel prefixes are
# stripped BEFORE validation (spec step 1, the L5 fix), so "#geeks"
# becomes the valid nick "geeks" and is accepted — see
# test_create_strips_channel_prefix_then_validates below.
@pytest.mark.parametrize("bad", ["", "   ", "#", "!!!", "9lives", "a" * 40])
async def test_create_rejects_bad_target_nicks(store: TellStore, bad: str):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick=bad, to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is False


@pytest.mark.parametrize("good", ["dave", "Dave_", "d-a-v-e", "[dave]", "da|ve"])
async def test_create_accepts_valid_nicks(store: TellStore, good: str):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick=good, to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is True


async def test_create_strips_channel_prefix_then_validates(store: TellStore):
    # "#dave" strips to "dave", which is a valid nick, so this succeeds
    # with the stripped value. Guards the L5 failure mode where the model
    # passes a channel name as the target.
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="#dave", to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is True
    row = await store.db.fetchone("SELECT to_nick_lower FROM tells")
    assert row["to_nick_lower"] == "dave"


async def test_create_refuses_the_bot_itself(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="TestBot", to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is False
    assert "myself" in r.message.lower()


async def test_create_allows_self_tell(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="alice", to_account=None, message="note to self",
        bot_nick="TestBot",
    )
    assert r.ok is True


async def test_create_rejects_empty_message(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account=None, message="   ", bot_nick="TestBot",
    )
    assert r.ok is False


async def test_create_truncates_long_message(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account=None, message="x" * 900, bot_nick="TestBot",
    )
    assert r.ok is True
    row = await store.db.fetchone("SELECT message FROM tells")
    assert len(row["message"]) == TELL_MAX_MESSAGE_LEN
    assert "truncat" in r.message.lower()


async def test_create_refused_when_allow_tell_false(tmp_path):
    # Built inline rather than via a conftest helper: `tests/` has no
    # __init__.py, so `from tests.conftest import ...` would not resolve.
    from bot.config import AICfg, ChannelCfg, Config, ServerCfg
    from bot.db import Database

    database = Database(tmp_path / "off.sqlite")
    await database.open()
    try:
        off = Config(
            server=ServerCfg(host="irc.example.org", nick="TestBot"),
            ai=AICfg(chat_model="test-model"),
            channels={"#off": ChannelCfg(allow_tell=False)},
        )
        s = TellStore(db=database, cfg=off)
        r = await s.create(
            channel="#off", from_nick="alice", from_account=None,
            to_nick="dave", to_account=None, message="hi", bot_nick="TestBot",
        )
        assert r.ok is False
        assert "disabled" in r.message.lower()
    finally:
        await database.close()
```

- [ ] **Step 2: Add the `store` fixture**

Append to `tests/conftest.py`:

```python
@pytest.fixture
async def store(db, cfg):
    """A TellStore over a fresh temp DB. Rate-limit state is per-instance,
    so every test starts with a clean budget."""
    from bot.tells import TellStore
    return TellStore(db=db, cfg=cfg)
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v
```

Expected: the 3 Task-1 tests still pass; everything new FAILS at import with `ModuleNotFoundError: No module named 'bot.tells'`.

- [ ] **Step 4: Create `bot/tells.py`**

```python
"""TellStore: presence-triggered message relay.

A "tell" is a message left for a user who is not around, delivered the
first time that user is next seen active in the same channel — they JOIN,
they speak, or they /me. Delivery is public and in-channel.

Design notes:
  - This module owns the `tells` table and nothing else, and deliberately
    imports no IRC machinery. `deliver()` receives the bot as an argument
    (the same way ToolContext hands `bot` to the IRC-native tools), which
    is what lets the whole thing be unit-tested against a temp SQLite file.
  - Every rule lives in create(). The !tell command and the leave_message
    LLM tool both go through it, so they cannot drift apart.
  - Soft delete: delivery sets delivered_at rather than deleting the row,
    mirroring memories.deleted_at (security review M3). It leaves a record
    of what the bot relayed to whom.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config
from .db import Database, now_utc_iso

log = logging.getLogger(__name__)


# Message cap. IRC's usable line length is ~400 bytes (see MAX_LINE_LEN in
# ircclient.py) and the delivered form carries a "nick: [from x, 3h ago] "
# prefix, so a 400-char message wraps to at most two wire lines.
TELL_MAX_MESSAGE_LEN = 400
# Undelivered tells stop being relevant eventually; 30 days is long enough
# for a holiday, short enough that nobody is ambushed by a message from
# another season.
TELL_EXPIRY_DAYS = 30

# RFC 2812 nick shape: a letter or one of the "special" characters first,
# then letters, digits, specials, or '-'. Without this check,
# `!tell hi there everyone` silently files a tell for a user named "hi".
NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}\-]{0,31}$")

# Channel name prefixes per RFC 2811. Stripped from a target nick before
# validation — the LLM occasionally passes a channel name as the target
# (the same failure mode set_reminder hit in security review L5).
_CHANNEL_PREFIXES = " \t#&+!"


def actor_key(nick: str, account: str | None) -> str:
    """Resolved identity for a person: their services account if they have
    one, else their lowercased nick under a 'nick:' prefix.

    The prefix is not decoration — it keeps the namespaces apart. Without
    it, the account "alice" and the unauthenticated nick "alice" would be
    treated as the same person, which would let anyone inherit a
    registered user's pending tells simply by taking their nick."""
    if account:
        return account
    return f"nick:{(nick or '').lower()}"


@dataclass
class TellResult:
    """Outcome of a create/cancel. `message` is written to be shown to the
    user verbatim in the channel, so it explains refusals rather than just
    signalling them."""
    ok: bool
    message: str
    tell_id: int | None = None


class TellStore:
    def __init__(self, db: Database, cfg: Config):
        self.db = db
        self.cfg = cfg
        # Rate-limit state is per-instance rather than module-global (which
        # is what set_reminder does). The app builds exactly one store, so
        # the behaviour is identical — but a test can get a clean budget by
        # constructing a fresh store instead of reaching into another
        # module's globals to reset them.
        self._rate_history: dict[str, list[float]] = {}
        self._rate_lock = asyncio.Lock()
        self._rate_last_sweep: float = 0.0
        # Serialises the whole query -> post -> mark sequence in deliver().
        self._deliver_lock = asyncio.Lock()
        # Serialises the cap-check -> insert sequence in create(). See the
        # comment at its use site for why those two DB calls must be atomic.
        self._create_lock = asyncio.Lock()

    # ---- creation ----

    async def create(
        self,
        *,
        channel: str,
        from_nick: str,
        from_account: str | None,
        to_nick: str,
        to_account: str | None,
        message: str,
        bot_nick: str,
    ) -> TellResult:
        """Validate and persist a tell. Every rule lives here so the
        command path and the LLM-tool path enforce identically."""
        if not self.cfg.channel(channel).allow_tell:
            return TellResult(False, "(tells are disabled in this channel)")

        target = self._clean_target_nick(to_nick)
        if target is None:
            return TellResult(
                False,
                f"(not a usable nick: {to_nick!r} — give me the nick to leave it for)",
            )
        if target.lower() == (bot_nick or "").lower():
            return TellResult(False, "(I can't leave a message for myself)")

        body = (message or "").strip()
        if not body:
            return TellResult(False, "(the message is empty)")
        truncated = len(body) > TELL_MAX_MESSAGE_LEN
        if truncated:
            body = body[:TELL_MAX_MESSAGE_LEN]

        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=TELL_EXPIRY_DAYS)
        tell_id = await self.db.insert_returning_id(
            "INSERT INTO tells "
            "(channel, from_nick, from_account, from_key, to_nick, "
            " to_nick_lower, to_account, message, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                channel, from_nick, from_account,
                actor_key(from_nick, from_account),
                target, target.lower(), to_account,
                body, now.isoformat(), expires.isoformat(),
            ),
        )

        await self.db.log_audit(
            action="tell.create",
            channel=channel,
            actor_nick=from_nick,
            actor_account=from_account,
            details={"tell_id": tell_id, "to_nick": target, "chars": len(body)},
        )

        note = f" (truncated to {TELL_MAX_MESSAGE_LEN} chars)" if truncated else ""
        return TellResult(
            True,
            f"ok — I'll tell {target} when they're next around{note}. "
            f"[#{tell_id}]",
            tell_id,
        )

    # ---- helpers ----

    @staticmethod
    def _clean_target_nick(raw: str) -> str | None:
        """Strip channel prefixes and whitespace, then validate the shape.
        Returns None if what's left isn't a usable nick."""
        candidate = (raw or "").strip().lstrip(_CHANNEL_PREFIXES).strip()
        if not candidate or not NICK_RE.match(candidate):
            return None
        return candidate
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v
```

Expected: all tests pass. If `test_create_refuses_the_bot_itself` fails on the substring assertion, check the refusal text contains "myself".

- [ ] **Step 6: Commit**

```bash
git add bot/tells.py tests/
git commit -m "feat(tells): TellStore.create with target and message validation"
```

---

## Task 3: Rate limits and pending caps

The abuse envelope. One in-memory limit on how fast a person can create tells, and two durable limits on how many can be queued at a recipient.

**Files:**
- Modify: `bot/tells.py`
- Modify: `tests/test_tells.py`

**Interfaces:**
- Consumes: `TellStore.create()`, `actor_key()` from Task 2.
- Produces: constants `TELL_RATE_LIMIT = 5`, `TELL_RATE_WINDOW_SEC = 3600.0`, `TELL_SWEEP_INTERVAL_SEC = 3600.0`, `TELL_MAX_PENDING_PER_PAIR = 5`, `TELL_MAX_PENDING_PER_RECIPIENT = 15`. `create()` gains three new refusal paths; its signature does not change.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tells.py`:

```python
from bot.tells import (
    TELL_MAX_PENDING_PER_PAIR,
    TELL_MAX_PENDING_PER_RECIPIENT,
    TELL_RATE_LIMIT,
)


async def _leave(store, to="dave", frm="alice", account=None, channel="#test"):
    return await store.create(
        channel=channel, from_nick=frm, from_account=account,
        to_nick=to, to_account=None, message="hi", bot_nick="TestBot",
    )


async def test_hourly_rate_limit_allows_up_to_the_cap(store):
    # Each goes to a different recipient so only the hourly cap can bite.
    for i in range(TELL_RATE_LIMIT):
        r = await _leave(store, to=f"dave{i}")
        assert r.ok is True, f"create #{i + 1} should have been allowed"


async def test_hourly_rate_limit_refuses_past_the_cap(store):
    for i in range(TELL_RATE_LIMIT):
        await _leave(store, to=f"dave{i}")
    r = await _leave(store, to="someone-else")
    assert r.ok is False
    assert "rate limit" in r.message.lower()


async def test_rate_limit_is_per_actor(store):
    for i in range(TELL_RATE_LIMIT):
        await _leave(store, to=f"dave{i}", frm="alice")
    # A different person still has their own budget.
    r = await _leave(store, to="dave", frm="bob")
    assert r.ok is True


async def test_pending_per_pair_cap(store):
    # TELL_RATE_LIMIT and TELL_MAX_PENDING_PER_PAIR are both 5, but create()
    # checks the pair cap BEFORE the hourly one, so the 6th is refused for
    # the pair reason. Asserting on the message is what pins that ordering.
    for i in range(TELL_MAX_PENDING_PER_PAIR):
        r = await _leave(store, to="dave")
        assert r.ok is True, f"pair create #{i + 1} should have been allowed"
    r = await _leave(store, to="dave")
    assert r.ok is False
    assert "already" in r.message.lower()


async def test_pending_per_pair_cap_is_per_recipient(store, monkeypatch):
    # Lift the hourly cap so only the pair cap can bite. Patching the module
    # constant beats reaching into store._rate_history: it touches no
    # privates and reads as what it is.
    monkeypatch.setattr("bot.tells.TELL_RATE_LIMIT", 100)
    for _ in range(TELL_MAX_PENDING_PER_PAIR):
        await _leave(store, to="dave")
    r = await _leave(store, to="erin")   # different recipient, fresh pair
    assert r.ok is True


async def test_pending_per_recipient_cap_across_senders(store):
    # Fill dave's inbox from enough distinct senders to exceed the
    # per-recipient cap without any one of them hitting the pair cap.
    # Each sender has their own hourly budget, so no patching is needed.
    senders = [f"sender{i}" for i in range(4)]
    made = 0
    for s in senders:
        for _ in range(TELL_MAX_PENDING_PER_PAIR):
            r = await _leave(store, to="dave", frm=s)
            if r.ok:
                made += 1
            else:
                assert "too many" in r.message.lower()
                assert made == TELL_MAX_PENDING_PER_RECIPIENT
                return
    pytest.fail(f"per-recipient cap never fired (made {made})")


async def test_delivered_tells_do_not_count_against_caps(store, monkeypatch):
    monkeypatch.setattr("bot.tells.TELL_RATE_LIMIT", 100)
    for _ in range(TELL_MAX_PENDING_PER_PAIR):
        await _leave(store, to="dave")
    # Mark them all delivered; the pair budget should free up.
    await store.db.execute(
        "UPDATE tells SET delivered_at = ? WHERE delivered_at IS NULL",
        ("2026-08-27T00:00:00+00:00",),
    )
    r = await _leave(store, to="dave")
    assert r.ok is True
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v -k "rate or pending or delivered_tells"
```

Expected: `ImportError: cannot import name 'TELL_RATE_LIMIT' from 'bot.tells'`.

- [ ] **Step 3: Add the constants**

In `bot/tells.py`, after `TELL_EXPIRY_DAYS`:

```python
# How fast one person may create tells. In-memory, so it resets on restart
# — the same trade-off set_reminder documents, and acceptable here for the
# same reason: it throttles bursts, it is not the load-bearing protection.
TELL_RATE_LIMIT = 5
TELL_RATE_WINDOW_SEC = 3600.0
# Periodic cleanup of _rate_history so it can't grow without bound over a
# long uptime (the L4 sweep pattern from irc_native.py).
TELL_SWEEP_INTERVAL_SEC = 3600.0

# The load-bearing anti-harassment limits, and the reason they are DB-backed
# rather than in-memory: these are what stand between a user and a queue of
# unwanted messages from someone else, so they must survive a restart.
# Both count PENDING rows only — delivered tells free the budget up again.
TELL_MAX_PENDING_PER_PAIR = 5        # from one sender to one recipient
TELL_MAX_PENDING_PER_RECIPIENT = 15  # at one recipient, all senders combined
```

- [ ] **Step 4: Add the rate-limit check**

In `bot/tells.py`, add these two methods to `TellStore`, above `_clean_target_nick`:

```python
    async def _check_rate(self, key: str) -> tuple[bool, int]:
        """Sliding-window rate check for one actor. Returns
        (allowed, remaining_in_window)."""
        async with self._rate_lock:
            now = time.monotonic()
            # Periodic sweep: drop actors whose whole history has aged out.
            if now - self._rate_last_sweep > TELL_SWEEP_INTERVAL_SEC:
                cutoff = now - TELL_RATE_WINDOW_SEC
                stale = [
                    k for k, v in self._rate_history.items()
                    if not v or max(v) < cutoff
                ]
                for k in stale:
                    del self._rate_history[k]
                if stale:
                    log.debug("tell rate sweep: dropped %d stale key(s)", len(stale))
                self._rate_last_sweep = now

            window_start = now - TELL_RATE_WINDOW_SEC
            fresh = [t for t in self._rate_history.get(key, []) if t >= window_start]
            if len(fresh) >= TELL_RATE_LIMIT:
                self._rate_history[key] = fresh   # keep the pruned state
                return False, 0
            fresh.append(now)
            self._rate_history[key] = fresh
            return True, TELL_RATE_LIMIT - len(fresh)

    async def _count_pending(
        self, channel: str, to_nick_lower: str, from_key: str | None
    ) -> int:
        """Pending, unexpired tells for a recipient in a channel. With
        `from_key`, narrowed to those from one sender."""
        params: list[Any] = [channel, to_nick_lower, now_utc_iso()]
        sql = (
            "SELECT COUNT(*) AS n FROM tells "
            "WHERE channel = ? AND to_nick_lower = ? "
            "AND delivered_at IS NULL AND expires_at > ?"
        )
        if from_key is not None:
            sql += " AND from_key = ?"
            params.append(from_key)
        row = await self.db.fetchone(sql, params)
        return int(row["n"]) if row else 0
```

- [ ] **Step 5: Wire the checks into `create()`**

In `bot/tells.py`, inside `create()`, replace the single line
`now = datetime.now(timezone.utc)` (it sits directly after the
message-truncation block) with the following. The replacement ends with
that same line, so the rest of the method is untouched:

```python
        # Durable caps first, then the in-memory rate limit. Order matters:
        # a refusal that costs the caller a slice of their hourly budget
        # would let a griefer burn someone else's budget by targeting a
        # recipient whose queue is already full.
        from_key = actor_key(from_nick, from_account)
        target_lower = target.lower()

        # The cap checks and the INSERT must be atomic. ircclient dispatches
        # command handlers with asyncio.create_task, so two !tell calls can
        # interleave at any await boundary in between — and there are three.
        # Without this lock both callers read a count one below the cap and
        # both insert, which is precisely the flood the durable caps exist
        # to stop. _count_pending alone being DB-backed is not enough:
        # surviving a restart and being race-free are different properties.
        #
        # Lock order: _create_lock may be held while _check_rate takes
        # _rate_lock. Nothing acquires them in the other order, and deliver()
        # takes only _deliver_lock and never calls create(), so there is no
        # cycle to deadlock on.
        async with self._create_lock:
            pair_pending = await self._count_pending(channel, target_lower, from_key)
            if pair_pending >= TELL_MAX_PENDING_PER_PAIR:
                return TellResult(
                    False,
                    f"(you already have {pair_pending} message(s) waiting for "
                    f"{target} here — use !untell to clear one first)",
                )

            total_pending = await self._count_pending(channel, target_lower, None)
            if total_pending >= TELL_MAX_PENDING_PER_RECIPIENT:
                return TellResult(
                    False,
                    f"(too many messages already waiting for {target} here; "
                    f"try again once they've picked some up)",
                )

            allowed, _remaining = await self._check_rate(from_key)
            if not allowed:
                log.info("tell rate-limited for %s in %s", from_key, channel)
                return TellResult(
                    False,
                    f"(rate limit: {TELL_RATE_LIMIT} tells an hour. Try later.)",
                )

            now = datetime.now(timezone.utc)
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add bot/tells.py tests/test_tells.py
git commit -m "feat(tells): hourly rate limit + durable per-pair and per-recipient caps"
```

---

## Task 4: `pending_for()` — hybrid nick/account matching

The lookup that decides whether an arriving person is the intended recipient. This is the part the spec singles out as differing from every classic IRC memo bot, and the part most worth getting under test.

**Files:**
- Modify: `bot/tells.py`
- Modify: `tests/test_tells.py`

**Interfaces:**
- Consumes: `create()` from Tasks 2–3.
- Produces: `TellStore.pending_for(channel: str, nick: str, account: str | None) -> list[aiosqlite.Row]`, oldest first.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tells.py`:

```python
async def test_pending_for_matches_exact_nick(store):
    await _leave(store, to="dave")
    rows = await store.pending_for("#test", "dave", None)
    assert len(rows) == 1


async def test_pending_for_matches_nick_case_insensitively(store):
    await _leave(store, to="DaVe")
    rows = await store.pending_for("#test", "dAvE", None)
    assert len(rows) == 1


async def test_pending_for_matches_on_account_when_nick_changed(store):
    # The whole point of the hybrid: dave was known as "dave" with account
    # "dave_acct" when the tell was left, and comes back as "dave_".
    await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account="dave_acct", message="hi",
        bot_nick="TestBot",
    )
    rows = await store.pending_for("#test", "dave_", "dave_acct")
    assert len(rows) == 1


async def test_pending_for_ignores_unrelated_people(store):
    await _leave(store, to="dave")
    assert await store.pending_for("#test", "erin", "erin_acct") == []


async def test_pending_for_does_not_match_null_account(store):
    # A tell with to_account NULL must not match everyone who arrives
    # without an account. Pins the no-cross-user-leak behaviour itself.
    # (Note: this passes with or without the explicit IS NOT NULL guard in
    # the WHERE — SQL's `= NULL` is already never true. It pins the
    # outcome that matters, not the guard's presence.)
    await _leave(store, to="dave")
    assert await store.pending_for("#test", "erin", None) == []


async def test_pending_for_is_channel_scoped(store):
    await _leave(store, to="dave", channel="#test")
    assert await store.pending_for("#other", "dave", None) == []


async def test_pending_for_skips_expired(store):
    await _leave(store, to="dave")
    await store.db.execute(
        "UPDATE tells SET expires_at = ?", ("2020-01-01T00:00:00+00:00",)
    )
    assert await store.pending_for("#test", "dave", None) == []


async def test_pending_for_skips_delivered(store):
    await _leave(store, to="dave")
    await store.db.execute("UPDATE tells SET delivered_at = ?", (now_utc_iso(),))
    assert await store.pending_for("#test", "dave", None) == []


async def test_pending_for_is_oldest_first(store):
    for i in range(3):
        await store.create(
            channel="#test", from_nick="alice", from_account=None,
            to_nick="dave", to_account=None, message=f"msg{i}",
            bot_nick="TestBot",
        )
    rows = await store.pending_for("#test", "dave", None)
    assert [r["message"] for r in rows] == ["msg0", "msg1", "msg2"]
```

Add `from bot.db import now_utc_iso` to the imports at the top of `tests/test_tells.py`.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v -k pending_for
```

Expected: every one FAILS with `AttributeError: 'TellStore' object has no attribute 'pending_for'`.

- [ ] **Step 3: Implement `pending_for()`**

Add to `TellStore` in `bot/tells.py`, after `_count_pending`:

```python
    async def pending_for(
        self, channel: str, nick: str, account: str | None
    ) -> list[Any]:
        """Undelivered, unexpired tells waiting for this person in this
        channel, oldest first.

        Hybrid match: the arriving nick equals the stored nick (case
        insensitive), OR their account equals the account we resolved when
        the tell was left. The second arm is what catches `dave` coming
        back as `dave_`.

        The `to_account IS NOT NULL` guard is defence-in-depth, not
        load-bearing today: SQL's three-valued logic already makes
        `to_account = ?` yield NULL (not true) whenever either side is
        NULL, so an accountless arrival cannot collect NULL-account tells
        even without it. It is kept because that protection is a property
        of the comparison operator rather than of this code — an edit that
        switched to `IS`, or moved the match into Python where
        `None == None` is True, would silently turn this into a
        cross-user message leak. The guard makes the requirement explicit
        at the site that depends on it."""
        return await self.db.fetchall(
            "SELECT * FROM tells "
            "WHERE channel = ? AND delivered_at IS NULL AND expires_at > ? "
            "AND (to_nick_lower = ? "
            "     OR (to_account IS NOT NULL AND ? IS NOT NULL AND to_account = ?)) "
            "ORDER BY created_at ASC, id ASC",
            (channel, now_utc_iso(), (nick or "").lower(), account, account),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add bot/tells.py tests/test_tells.py
git commit -m "feat(tells): hybrid nick-or-account matching for pending lookups"
```

---

## Task 5: `deliver()` — posting, the per-arrival cap, and the race

**Files:**
- Modify: `bot/tells.py`
- Modify: `tests/test_tells.py`

**Interfaces:**
- Consumes: `pending_for()` from Task 4.
- Produces: `TellStore.deliver(bot: Any, channel: str, nick: str, account: str | None) -> int` (returns how many were delivered); constant `TELL_MAX_DELIVER_PER_ARRIVAL = 3`; module function `human_age(created_iso: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tells.py`:

```python
import asyncio

from bot.tells import TELL_MAX_DELIVER_PER_ARRIVAL, human_age


class FakeBot:
    """Stand-in for IRCBot. Records what would go on the wire. Only
    irc_send is needed — TellStore never touches anything else."""

    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def irc_send(self, target: str, text: str, **kwargs) -> int:
        if self.fail:
            raise RuntimeError("wire is down")
        self.sent.append((target, text))
        return 1


def test_human_age_formats():
    assert human_age(now_utc_iso()) == "just now"
    assert human_age("2020-01-01T00:00:00+00:00").endswith("d ago")
    assert human_age("not a timestamp") == "a while ago"


async def test_deliver_posts_and_marks(store):
    await _leave(store, to="dave")
    bot = FakeBot()
    n = await store.deliver(bot, "#test", "dave", None)
    assert n == 1
    assert len(bot.sent) == 1
    target, text = bot.sent[0]
    assert target == "#test"
    assert text.startswith("dave:")
    assert "alice" in text
    row = await store.db.fetchone("SELECT delivered_at FROM tells")
    assert row["delivered_at"] is not None


async def test_deliver_is_not_repeated(store):
    await _leave(store, to="dave")
    bot = FakeBot()
    assert await store.deliver(bot, "#test", "dave", None) == 1
    assert await store.deliver(bot, "#test", "dave", None) == 0
    assert len(bot.sent) == 1


async def test_deliver_noop_when_nothing_pending(store):
    bot = FakeBot()
    assert await store.deliver(bot, "#test", "nobody", None) == 0
    assert bot.sent == []


async def test_deliver_caps_per_arrival_and_flags_the_rest(store):
    for i in range(TELL_MAX_DELIVER_PER_ARRIVAL + 2):
        await store.create(
            channel="#test", from_nick=f"s{i}", from_account=None,
            to_nick="dave", to_account=None, message=f"m{i}",
            bot_nick="TestBot",
        )
    bot = FakeBot()
    n = await store.deliver(bot, "#test", "dave", None)
    assert n == TELL_MAX_DELIVER_PER_ARRIVAL
    # 3 tells + 1 overflow notice
    assert len(bot.sent) == TELL_MAX_DELIVER_PER_ARRIVAL + 1
    assert "2 more" in bot.sent[-1][1]


async def test_deliver_delivers_the_remainder_next_time(store):
    for i in range(TELL_MAX_DELIVER_PER_ARRIVAL + 2):
        await store.create(
            channel="#test", from_nick=f"s{i}", from_account=None,
            to_nick="dave", to_account=None, message=f"m{i}",
            bot_nick="TestBot",
        )
    bot = FakeBot()
    await store.deliver(bot, "#test", "dave", None)
    assert await store.deliver(bot, "#test", "dave", None) == 2


async def test_deliver_leaves_row_pending_when_send_fails(store):
    await _leave(store, to="dave")
    n = await store.deliver(FakeBot(fail=True), "#test", "dave", None)
    assert n == 0
    row = await store.db.fetchone("SELECT delivered_at FROM tells")
    assert row["delivered_at"] is None, "a failed send must not mark delivered"
    # And it still arrives on the next attempt.
    bot = FakeBot()
    assert await store.deliver(bot, "#test", "dave", None) == 1


async def test_concurrent_deliver_does_not_double_post(store):
    """The join-and-speak race: both hooks fire within milliseconds."""
    await _leave(store, to="dave")
    bot = FakeBot()
    results = await asyncio.gather(
        store.deliver(bot, "#test", "dave", None),
        store.deliver(bot, "#test", "dave", None),
    )
    assert sorted(results) == [0, 1]
    assert len(bot.sent) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v -k "deliver or human_age"
```

Expected: `ImportError: cannot import name 'TELL_MAX_DELIVER_PER_ARRIVAL'`.

- [ ] **Step 3: Add the constant and the age formatter**

In `bot/tells.py`, after `TELL_MAX_PENDING_PER_RECIPIENT`:

```python
# How many tells the bot will hand over in one arrival. More than this and
# the rest wait for the recipient's next activity. IRC servers kill clients
# that flood: ircu-based networks (Quakenet) accumulate fake-lag per
# PRIVMSG and disconnect past ~10 seconds of debt — the same failure the
# task-result wire cap exists to prevent.
TELL_MAX_DELIVER_PER_ARRIVAL = 3
```

And as a module-level function, after `actor_key`:

```python
def human_age(created_iso: str) -> str:
    """'3h ago' / '2d ago' — a rough age for the delivered line. Precision
    beyond this is noise; the point is 'is this stale or fresh'."""
    try:
        created = datetime.fromisoformat(created_iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "a while ago"
    secs = int((datetime.now(timezone.utc) - created).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"
```

- [ ] **Step 4: Implement `deliver()`**

Add to `TellStore` in `bot/tells.py`, after `pending_for`:

```python
    async def deliver(
        self, bot: Any, channel: str, nick: str, account: str | None
    ) -> int:
        """Hand over any tells waiting for this person. Returns the count
        delivered.

        The lock covers the whole read-post-mark sequence. A JOIN and the
        recipient's first message can arrive milliseconds apart and both
        call this; without the lock they would each read the same pending
        rows and post them twice.

        delivered_at is set AFTER a successful send, never before. A wire
        failure therefore leaves the row pending and it retries on their
        next activity — the same choice _fire_reminder makes. A duplicated
        tell is annoying; a silently lost one defeats the feature."""
        async with self._deliver_lock:
            rows = await self.pending_for(channel, nick, account)
            if not rows:
                return 0

            batch = rows[:TELL_MAX_DELIVER_PER_ARRIVAL]
            delivered = 0

            for row in batch:
                line = (
                    f"{nick}: [from {row['from_nick']}, "
                    f"{human_age(row['created_at'])}] {row['message']}"
                )
                # irc_send does NOT raise on a wire failure — it catches
                # internally and returns the count actually sent (see its
                # docstring in ircclient.py). So the return value, not an
                # exception, is what tells us the line went out. Treating a
                # 0 as success here would mark the tell delivered having
                # posted nothing: the exact silent loss this method's
                # retry behaviour exists to prevent. The except is kept as
                # well, in case a future irc_send does raise.
                try:
                    sent = await bot.irc_send(channel, line)
                except Exception:
                    log.exception(
                        "tell #%s: send failed, leaving pending", row["id"]
                    )
                    break
                if not sent:
                    log.warning(
                        "tell #%s: irc_send posted 0 lines, leaving pending",
                        row["id"],
                    )
                    break
                try:
                    await self.db.execute(
                        "UPDATE tells SET delivered_at = ? WHERE id = ?",
                        (now_utc_iso(), row["id"]),
                    )
                except Exception:
                    # Sent but not marked: it will be delivered again on
                    # their next activity. Logged loudly because a repeat
                    # is confusing to users and worth noticing.
                    log.exception(
                        "tell #%s: posted but failed to mark delivered",
                        row["id"],
                    )
                    continue
                delivered += 1
                await self.db.log_audit(
                    action="tell.deliver",
                    channel=channel,
                    actor_nick=row["from_nick"],
                    actor_account=row["from_account"],
                    details={"tell_id": row["id"], "to_nick": nick},
                )

            # Computed AFTER the loop, not before it. Anything still
            # pending counts as "more" — whether it was beyond the
            # per-arrival cap, or was in the batch but did not get sent.
            # Deriving it from the batch slice up front would understate
            # the remainder whenever a send failed mid-batch.
            overflow = len(rows) - delivered
            if delivered and overflow:
                try:
                    await bot.irc_send(
                        channel,
                        f"{nick}: (+{overflow} more, next time you're around)",
                    )
                except Exception:
                    log.exception("tell overflow notice failed for %s", nick)

            if delivered:
                log.info(
                    "Delivered %d tell(s) to %s in %s", delivered, nick, channel
                )
            return delivered
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add bot/tells.py tests/test_tells.py
git commit -m "feat(tells): deliver with per-arrival cap, retry-on-failure, race lock"
```

---

## Task 6: `list_from()`, `cancel()`, `prune()`, and the scheduler hook

The management surface plus retention. Grouped because they are three short queries over the same table and none is worth a separate review gate.

**Files:**
- Modify: `bot/tells.py`
- Modify: `bot/scheduler.py:395-416`
- Modify: `tests/test_tells.py`

**Interfaces:**
- Consumes: `create()`, `actor_key()`, and the `FakeBot` test helper defined in Task 5 (`test_cancel_already_delivered` uses it). Task 5 must be done first.
- Produces:
  - `TellStore.list_from(channel: str, nick: str, account: str | None) -> list[Row]`
  - `TellStore.cancel(tell_id: int, actor_nick: str, actor_account: str | None, is_operator: bool) -> TellResult`
  - `TellStore.prune() -> int`
  - `Scheduler` gains a `tells` constructor parameter (`Any`, defaults `None`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tells.py`:

```python
async def test_list_from_returns_only_your_own(store):
    await _leave(store, to="dave", frm="alice")
    await _leave(store, to="erin", frm="bob")
    rows = await store.list_from("#test", "alice", None)
    assert len(rows) == 1
    assert rows[0]["to_nick"] == "dave"


async def test_list_from_excludes_delivered(store):
    await _leave(store, to="dave", frm="alice")
    await store.db.execute("UPDATE tells SET delivered_at = ?", (now_utc_iso(),))
    assert await store.list_from("#test", "alice", None) == []


async def test_list_from_matches_on_account_not_nick(store):
    await _leave(store, to="dave", frm="alice", account="alice_acct")
    # Same person, different nick — their account is what identifies them.
    rows = await store.list_from("#test", "alice_away", "alice_acct")
    assert len(rows) == 1


async def test_cancel_by_owner(store):
    r = await _leave(store, to="dave", frm="alice")
    c = await store.cancel(r.tell_id, "alice", None, is_operator=False)
    assert c.ok is True
    assert await store.pending_for("#test", "dave", None) == []


async def test_cancel_refused_for_stranger(store):
    r = await _leave(store, to="dave", frm="alice")
    c = await store.cancel(r.tell_id, "mallory", None, is_operator=False)
    assert c.ok is False
    assert len(await store.pending_for("#test", "dave", None)) == 1


async def test_cancel_allowed_for_operator(store):
    r = await _leave(store, to="dave", frm="alice")
    c = await store.cancel(r.tell_id, "root", "root_acct", is_operator=True)
    assert c.ok is True


async def test_cancel_unknown_id(store):
    c = await store.cancel(9999, "alice", None, is_operator=False)
    assert c.ok is False
    assert "no pending tell" in c.message.lower()


async def test_cancel_already_delivered(store):
    r = await _leave(store, to="dave", frm="alice")
    await store.deliver(FakeBot(), "#test", "dave", None)
    c = await store.cancel(r.tell_id, "alice", None, is_operator=False)
    assert c.ok is False


async def test_prune_removes_old_delivered_and_expired(store):
    old = "2020-01-01T00:00:00+00:00"
    await _leave(store, to="dave")                        # keep: pending, fresh
    r2 = await _leave(store, to="erin")
    await store.db.execute(
        "UPDATE tells SET delivered_at = ? WHERE id = ?", (old, r2.tell_id)
    )                                                     # drop: long delivered
    r3 = await _leave(store, to="fred")
    await store.db.execute(
        "UPDATE tells SET expires_at = ? WHERE id = ?", (old, r3.tell_id)
    )                                                     # drop: expired
    assert await store.prune() == 2
    rows = await store.db.fetchall("SELECT to_nick FROM tells")
    assert [r["to_nick"] for r in rows] == ["dave"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v -k "list_from or cancel or prune"
```

Expected: `AttributeError: 'TellStore' object has no attribute 'list_from'`.

- [ ] **Step 3: Implement the three methods**

Add to `TellStore` in `bot/tells.py`, after `deliver`:

```python
    async def list_from(
        self, channel: str, nick: str, account: str | None
    ) -> list[Any]:
        """Pending tells this person left in this channel, oldest first.
        Identity is from_key, so it follows their account across nick
        changes rather than being defeated by one."""
        return await self.db.fetchall(
            "SELECT * FROM tells "
            "WHERE channel = ? AND from_key = ? "
            "AND delivered_at IS NULL AND expires_at > ? "
            "ORDER BY created_at ASC, id ASC",
            (channel, actor_key(nick, account), now_utc_iso()),
        )

    async def cancel(
        self,
        tell_id: int,
        actor_nick: str,
        actor_account: str | None,
        is_operator: bool,
    ) -> TellResult:
        """Withdraw a tell before it is delivered. You may cancel your own;
        operators may cancel any. Already-delivered tells cannot be
        cancelled — that message is out, and pretending otherwise would be
        worse than saying so."""
        row = await self.db.fetchone(
            "SELECT * FROM tells WHERE id = ?", (tell_id,)
        )
        if row is None or row["delivered_at"] is not None:
            return TellResult(False, f"(no pending tell #{tell_id})")

        if not is_operator and row["from_key"] != actor_key(actor_nick, actor_account):
            log.info(
                "!untell refused: %s is not the owner of tell #%d",
                actor_nick, tell_id,
            )
            return TellResult(False, f"(tell #{tell_id} isn't yours to cancel)")

        await self.db.execute("DELETE FROM tells WHERE id = ?", (tell_id,))
        await self.db.log_audit(
            action="tell.cancel",
            channel=row["channel"],
            actor_nick=actor_nick,
            actor_account=actor_account,
            details={"tell_id": tell_id, "to_nick": row["to_nick"],
                     "by_operator": is_operator},
        )
        return TellResult(
            True, f"cancelled tell #{tell_id} for {row['to_nick']}."
        )

    async def prune(self) -> int:
        """Retention sweep: drop tells delivered more than TELL_EXPIRY_DAYS
        ago, and undelivered ones past their expiry. Returns rows removed.

        A hard DELETE here, unlike delivery's soft delete: the soft delete
        exists so a recently-relayed message is still inspectable, and
        after a month that value has run out."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=TELL_EXPIRY_DAYS)
        ).isoformat()
        now = now_utc_iso()
        try:
            # Direct sqlite execution so we can read cur.rowcount; the
            # db.execute wrapper doesn't expose it. Same pattern and same
            # reason as Scheduler._prune_old_reminders.
            async with self.db.conn.execute(
                "DELETE FROM tells "
                "WHERE (delivered_at IS NOT NULL AND delivered_at < ?) "
                "   OR (delivered_at IS NULL AND expires_at < ?)",
                (cutoff, now),
            ) as cur:
                removed = cur.rowcount or 0
            await self.db.conn.commit()
            if removed:
                log.info("Pruned %d expired/delivered tell(s)", removed)
            return removed
        except Exception:
            log.exception("tell retention prune failed")
            return 0
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
".venv/Scripts/python.exe" -m pytest tests/test_tells.py -v
```

Expected: all pass.

- [ ] **Step 5: Hook `prune()` into the scheduler**

In `bot/scheduler.py`, add a parameter to `Scheduler.__init__` after `memory: Any,`:

```python
        tells: Any = None,          # bot.tells.TellStore | None
```

and in the body, after `self.memory = memory`:

```python
        self.tells = tells
```

Then in `_reminder_loop`'s hourly housekeeping block (around line 411), after `await self._refresh_channel_op_state()`:

```python
                    if self.tells is not None:
                        try:
                            await self.tells.prune()
                        except Exception:
                            log.exception("tell prune failed")
```

Update the block's comment to list four maintenance tasks rather than three, adding:

```
                #   - prune delivered/expired tells
```

- [ ] **Step 6: Verify nothing broke**

```bash
".venv/Scripts/python.exe" -m pytest tests/ -v && ".venv/Scripts/python.exe" -c "import bot.scheduler, bot.tells; print('imports ok')"
```

Expected: all tests pass, then `imports ok`.

- [ ] **Step 7: Commit**

```bash
git add bot/tells.py bot/scheduler.py tests/test_tells.py
git commit -m "feat(tells): list/cancel/prune + scheduler retention hook"
```

---

## Task 7: Wire `TellStore` into the bot and add the delivery hooks

`TellStore` is now complete and tested. This task makes the bot actually call it. From here on, verification is by hand against a live server — there is no test double for pydle.

**Files:**
- Modify: `bot/main.py:339-378`
- Modify: `bot/ircclient.py` (constructor ~line 55-68; `on_join` ~164; `on_message` ~263; `on_ctcp_action` ~377)

**Interfaces:**
- Consumes: `TellStore` from Tasks 2–6.
- Produces: `IRCBot.tell_store` (`TellStore | None`); `IRCBot._maybe_deliver_tells(channel: str, nick: str) -> None`.

- [ ] **Step 1: Add the constructor field**

In `bot/ircclient.py`, add to `IRCBot.__init__`'s keyword arguments after `task_runner: Any = None,`:

```python
        tell_store: Any = None,            # bot.tells.TellStore (untyped to avoid cycle)
```

and in the body, after `self.task_runner = task_runner`:

```python
        # TellStore owns the !tell / !tells / !untell lifecycle and the
        # presence-triggered delivery. Like task_runner it may be assigned
        # after construction; handlers refuse gracefully while it is None.
        self.tell_store = tell_store
```

- [ ] **Step 2: Add the delivery helper**

In `bot/ircclient.py`, add near the other `_cmd_*` helpers:

```python
    # ---- tell relay ----

    async def _maybe_deliver_tells(self, channel: str, nick: str) -> None:
        """Hand over any messages waiting for `nick` in `channel`.

        Called from three presence hooks (join / message / action). Cheap
        when there is nothing pending: one indexed SELECT that returns no
        rows. Swallows every exception — this runs off the back of routine
        IRC events, and a tell-relay bug must never be able to break
        on_join or on_message."""
        if self.tell_store is None or nick == self.nickname:
            return
        try:
            account = self._cached_account_or_none(nick)
            await self.tell_store.deliver(self, channel, nick, account)
        except Exception:
            log.exception("tell delivery failed for %s in %s", nick, channel)
```

Note it uses the *cached* account and never issues a WHOIS. Delivery happens on every public line in the channel; a WHOIS per message would be a self-inflicted flood. An unauthenticated arrival still matches on nick, which is the common case.

- [ ] **Step 3: Hook `on_join`**

In `bot/ircclient.py`, replace the body of `on_join`:

```python
    async def on_join(self, channel: str, user: str) -> None:
        await super().on_join(channel, user)
        if user == self.nickname:
            log.info("Joined %s", channel)
            return
        # Someone else arrived — they may have messages waiting.
        asyncio.create_task(self._maybe_deliver_tells(channel, user))
```

- [ ] **Step 4: Hook `on_message`**

In `bot/ircclient.py`, inside `on_message`, in the `if not is_dm:` block, after the `self.memory_writer.note_message(target)` line:

```python
            # Presence trigger: they spoke, so they're here and reading.
            # Covers the case a join-only trigger misses entirely — someone
            # who was already sitting in the channel, idle, when the tell
            # was left.
            asyncio.create_task(self._maybe_deliver_tells(target, source))
```

- [ ] **Step 5: Hook `on_ctcp_action`**

In `bot/ircclient.py`, inside `on_ctcp_action`, in the `if not is_dm:` block, after its `self.memory_writer.note_message(target)` line:

```python
            # A /me is activity too. Pydle routes actions here and never
            # through on_message, so without this an emoting user would
            # never trigger delivery.
            asyncio.create_task(self._maybe_deliver_tells(target, by))
```

- [ ] **Step 6: Wire it up in `main.py`**

In `bot/main.py`, add to the imports beside `from .tasks import TaskRunner`:

```python
from .tells import TellStore
```

After the `client = IRCBot(...)` construction and before the `Scheduler(...)` construction, add:

```python
    # TellStore owns the !tell relay. Constructed after the IRCBot for the
    # same reason TaskRunner is — delivery posts through the bot — and
    # assigned back by attribute.
    tell_store = TellStore(db=db, cfg=cfg)
    client.tell_store = tell_store
```

Then pass it to the scheduler by adding `tells=tell_store,` to the `Scheduler(...)` call alongside `memory=memory,`.

- [ ] **Step 7: Verify the bot still starts**

```bash
".venv/Scripts/python.exe" -c "import bot.main, bot.ircclient, bot.tells; print('imports ok')" && ".venv/Scripts/python.exe" -m pytest tests/ -q
```

Expected: `imports ok`, then all tests pass.

- [ ] **Step 8: Commit**

```bash
git add bot/ircclient.py bot/main.py
git commit -m "feat(tells): wire TellStore into the bot; join/message/action delivery hooks"
```

---

## Task 8: The `!tell`, `!tells` and `!untell` commands

**Files:**
- Modify: `bot/ircclient.py` (dispatch block ~lines 296-315; handlers near `_cmd_list_tasks`)

**Interfaces:**
- Consumes: `self.tell_store` from Task 7.
- Produces: `_cmd_tell`, `_cmd_list_tells`, `_cmd_untell` — all `(self, channel: str, source: str, cmd_text: str) -> None` except `_cmd_list_tells`, which takes `(self, channel: str, source: str)`.

- [ ] **Step 1: Add the dispatch lines**

In `bot/ircclient.py`, in `on_message` after the existing `!task` dispatch block, add:

```python
        # Tell relay. Same early-dispatch treatment as the task commands:
        # plain commands, no mention required, no LLM turn burned. Both the
        # bare and the with-args form are matched so "!tell" alone gets a
        # usage hint rather than falling through to engagement.
        if not is_dm and (cmd_text == "!tells" or cmd_text.startswith("!tells ")):
            asyncio.create_task(self._cmd_list_tells(target, source))
            return
        if not is_dm and (cmd_text == "!untell" or cmd_text.startswith("!untell ")):
            asyncio.create_task(self._cmd_untell(target, source, cmd_text))
            return
        if not is_dm and (cmd_text == "!tell" or cmd_text.startswith("!tell ")):
            asyncio.create_task(self._cmd_tell(target, source, cmd_text))
            return
```

**On ordering:** as written, order is not load-bearing — `"!tells foo"` does not match `startswith("!tell ")` (position 5 is `s`, not a space) and does not equal `"!tell"`. List `!tells` first regardless: it makes the intent obvious to the next reader, and it means a future edit that drops the trailing space from `"!tell "` cannot silently swallow `!tells`.

- [ ] **Step 2: Add the handlers**

In `bot/ircclient.py`, after `_cmd_list_tasks`:

```python
    async def _cmd_tell(self, channel: str, source: str, cmd_text: str) -> None:
        """`!tell <nick> <message>` — leave a message for an absent user."""
        if self.tell_store is None:
            await self.irc_send(channel, "(tells not available — TellStore not wired)")
            return

        rest = cmd_text[len("!tell"):].strip()
        parts = rest.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await self.irc_send(
                channel,
                "Usage: !tell <nick> <message>  "
                "(delivered publicly here when they're next around)",
            )
            return
        to_nick, body = parts[0], parts[1].strip()

        # Resolve the sender's account so the rate limit and !untell
        # ownership follow them across nick changes.
        await self._ensure_account_known(source)
        from_account = self._cached_account_or_none(source)
        # Opportunistically resolve the TARGET's account too. If they're
        # around (or were recently), this is what lets the tell still find
        # them when they come back as "dave_". A miss is fine and common —
        # we fall back to nick matching. Deliberately no WHOIS here: the
        # target is usually offline, and WHOIS on an absent nick just
        # returns 401.
        to_account = self._cached_account_or_none(to_nick)

        try:
            result = await self.tell_store.create(
                channel=channel,
                from_nick=source,
                from_account=from_account,
                to_nick=to_nick,
                to_account=to_account,
                message=body,
                bot_nick=self.nickname,
            )
        except Exception:
            log.exception("tell create failed in %s", channel)
            await self.irc_send(channel, "(failed to store that tell; see logs)")
            return

        await self.irc_send(channel, f"{source}: {result.message}")

    async def _cmd_list_tells(self, channel: str, source: str) -> None:
        """`!tells` — list the pending tells you left in this channel."""
        if self.tell_store is None:
            await self.irc_send(channel, "(tells not available — TellStore not wired)")
            return

        await self._ensure_account_known(source)
        account = self._cached_account_or_none(source)
        try:
            rows = await self.tell_store.list_from(channel, source, account)
        except Exception:
            log.exception("list_from failed for %s in %s", source, channel)
            await self.irc_send(channel, "(failed to list tells; see logs)")
            return

        if not rows:
            await self.irc_send(channel, f"{source}: no tells waiting from you here.")
            return

        for r in rows:
            preview = (r["message"] or "")[:60]
            if len(r["message"] or "") > 60:
                preview += "…"
            await self.irc_send(
                channel,
                f"  #{r['id']} -> {r['to_nick']} ({human_age(r['created_at'])}): {preview}",
            )

    async def _cmd_untell(self, channel: str, source: str, cmd_text: str) -> None:
        """`!untell <id>` — cancel a tell you left, before it's delivered."""
        if self.tell_store is None:
            await self.irc_send(channel, "(tells not available — TellStore not wired)")
            return

        rest = cmd_text[len("!untell"):].strip()
        try:
            tell_id = int(rest)
        except ValueError:
            await self.irc_send(
                channel, "Usage: !untell <id>  (the id shown by !tells)"
            )
            return

        await self._ensure_account_known(source)
        account = self._cached_account_or_none(source)
        try:
            result = await self.tell_store.cancel(
                tell_id=tell_id,
                actor_nick=source,
                actor_account=account,
                is_operator=self.auth.is_operator(account),
            )
        except Exception:
            log.exception("cancel tell #%d failed", tell_id)
            await self.irc_send(channel, "(failed to cancel; see logs)")
            return

        await self.irc_send(channel, f"{source}: {result.message}")
```

- [ ] **Step 3: Add the import**

In `bot/ircclient.py`, add to the local imports near `from .policy import ChannelPolicy`:

```python
from .tells import human_age
```

- [ ] **Step 4: Verify imports and tests**

```bash
".venv/Scripts/python.exe" -c "import bot.ircclient; print('imports ok')" && ".venv/Scripts/python.exe" -m pytest tests/ -q
```

Expected: `imports ok`, all tests pass.

- [ ] **Step 5: Manual smoke test against a live server**

Start the bot, then from another IRC client in a channel the bot has joined:

1. `!tell <a nick that is not present> hello there` → expect `you: ok — I'll tell <nick> when they're next around. [#1]`
2. `!tells` → expect one line `#1 -> <nick> (just now): hello there`
3. `!untell 1` → expect `cancelled tell #1 for <nick>.`
4. `!tell` (bare) → expect the usage line
5. `!tell` with a second connection as the target, then have that connection say anything → expect the relay line to appear in-channel
6. `CubesBot: !tell dave hi` (nick-prefixed form) → expect the same behaviour as bare `!tell`

- [ ] **Step 6: Commit**

```bash
git add bot/ircclient.py
git commit -m "feat(tells): !tell, !tells and !untell chat commands"
```

---

## Task 9: The `leave_message` LLM tool

**Files:**
- Create: `bot/tools/tell.py`
- Modify: `bot/tools/__init__.py` (the import block at the bottom)

**Interfaces:**
- Consumes: `TellStore.create()`; `ToolContext.bot` (the `IRCBot`, carrying `.tell_store` and `.nickname`).
- Produces: a registered tool named `leave_message`.

- [ ] **Step 1: Create the tool module**

```python
"""Tier-4 tool: leave_message.

The conversational path into the same relay `!tell` uses — "tell dave I
said hi when he's back". Deliberately thin: it shapes arguments and hands
off to TellStore.create(), which owns every rule. Keeping the rules in one
place is what stops the command path and this path from drifting apart.
"""

from __future__ import annotations

import logging

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


async def _leave_message(ctx: ToolContext, args: dict) -> dict:
    bot = ctx.bot
    store = getattr(bot, "tell_store", None) if bot is not None else None
    if store is None:
        return {"error": "the tell relay is not available"}
    if not ctx.channel or not ctx.channel.startswith(("#", "&")):
        return {"error": "tells can only be left in a channel, not in a DM"}

    target = (args.get("target_nick") or "").strip()
    if not target:
        return {"error": "target_nick is required"}
    message = (args.get("message") or "").strip()
    if not message:
        return {"error": "message is empty"}

    result = await store.create(
        channel=ctx.channel,
        from_nick=ctx.actor_nick,
        from_account=ctx.actor_account,
        to_nick=target,
        to_account=None,
        message=message,
        bot_nick=getattr(bot, "nickname", ""),
    )
    if not result.ok:
        return {"error": result.message}
    return {
        "left": True,
        "id": result.tell_id,
        "target_nick": target,
        "note": "delivered publicly in this channel when they're next active here",
    }


register(Tool(
    name="leave_message",
    description=(
        "Leave a message for a user who is not currently around. The bot "
        "relays it publicly in THIS channel the first time that user is "
        "next seen active here (they join, speak, or /me). Use this when "
        "someone asks you to pass something on to an absent person. "
        "target_nick must be a NICK, never a channel name — do not pass "
        "'#channel'. The message is not private: everyone in the channel "
        "will see it when it is delivered."
    ),
    schema={
        "type": "object",
        "properties": {
            "target_nick": {
                "type": "string",
                "description": "Nick of the person to leave the message for.",
            },
            "message": {
                "type": "string",
                "description": "Text to relay to them (max 400 chars).",
            },
        },
        "required": ["target_nick", "message"],
        "additionalProperties": False,
    },
    requires=set(),
    call=_leave_message,
))
```

`to_account=None` is correct here rather than an oversight: `ToolContext` has no access to the auth cache, and the LLM path is the less common one. The tell still matches on nick, which is what a plain IRC memo bot does. `!tell` remains the path that gets the stronger account match.

`requires=set()` means the tool is always in the catalog. The per-channel gate is `allow_tell`, checked inside `create()`, so a channel with tells disabled gets a clear refusal the model can relay rather than a tool that silently does not exist.

- [ ] **Step 2: Register the module**

In `bot/tools/__init__.py`, add after the `irc_native` import line:

```python
from . import tell           # noqa: E402,F401  (Tier-4: leave_message)
```

- [ ] **Step 3: Verify the tool registers**

```bash
".venv/Scripts/python.exe" -c "import bot.tools as t; print('leave_message' in t.REGISTRY, sorted(t.REGISTRY))"
```

Expected: `True` followed by the tool list including `leave_message`.

- [ ] **Step 4: Manual smoke test**

In a `chatty` channel with the bot present, say: `CubesBot: tell dave I said hi when he's back`. Expect the bot to call `leave_message` and confirm conversationally. Then have `dave` join or speak and confirm the relay fires.

- [ ] **Step 5: Commit**

```bash
git add bot/tools/tell.py bot/tools/__init__.py
git commit -m "feat(tells): leave_message LLM tool"
```

---

## Task 10: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add the README section**

In `README.md`, after the **Reminders** block in "What it does":

```markdown
**Tell relay** (`bot/tells.py` + `!tell` / `leave_message`)
- `!tell <nick> <message>` leaves a message for someone who isn't around.
- Delivered publicly in the same channel the first time that person is
  next seen active there — they join, they speak, or they `/me`.
- Matched on nick **or** Quakenet account, so `dave` returning as `dave_`
  still gets the message.
- `!tells` lists what you have pending; `!untell <id>` cancels one.
- `leave_message` tool gives the LLM the same path conversationally.
- Tells are **not private** — the whole channel sees them on delivery.
- Limits: 5/hour per person, 5 pending per sender→recipient pair,
  15 pending per recipient, 400 chars, 30-day expiry, 3 per arrival.
- Per-channel `allow_tell` (default on) disables leaving new tells;
  already-pending ones are still delivered.
```

- [ ] **Step 2: Add the CHANGELOG entry**

At the top of `CHANGELOG.md`'s entry list, matching the file's existing slice format:

```markdown
### Tell relay — leave a message for an absent user

`!tell <nick> <message>` holds a message and relays it publicly in-channel
the first time the recipient is next seen active (join, message, or `/me`).
Recipients are matched on nick **or** account, so a returning `dave_` still
gets a tell left for `dave`. `!tells` lists your pending ones, `!untell <id>`
cancels one, and a `leave_message` tool gives the LLM the same path.

New `bot/tells.py` owns the new `tells` table and every rule — both entry
points go through `TellStore.create()`, so the command and the tool cannot
enforce differently. Abuse envelope follows the `set_reminder` house style
(5/hour per actor, in-memory) but makes the per-sender/recipient and
per-recipient caps DB-backed: those are what protect a user from someone
else, so they must survive a restart. Delivery is soft-delete
(`delivered_at`), capped at 3 per arrival to stay under IRC flood limits,
and marks the row only after a successful send so a wire failure retries
rather than losing the message. Per-channel `allow_tell` gates creation
only, so switching it off can never strand pending messages.

Also adds the repo's first test suite — `pytest` + `pytest-asyncio`,
covering `TellStore` against a temp SQLite file.
```

- [ ] **Step 3: Final full verification**

```bash
".venv/Scripts/python.exe" -m pytest tests/ -v && ".venv/Scripts/python.exe" -c "import bot.main; print('bot imports clean')"
```

Expected: all tests pass, then `bot imports clean`.

- [ ] **Step 4: Commit**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: document the !tell relay"
```

---

## Manual Verification Checklist

Run against a live IRC server before calling this done. These paths have no automated coverage — they need real pydle events.

- [ ] `!tell <absent nick> <msg>` in a channel → confirmation posted
- [ ] Target joins → message relayed, prefixed with their nick and the sender's
- [ ] Target instead speaks (was already idle in-channel) → message relayed
- [ ] Target instead `/me`s → message relayed
- [ ] Message delivered exactly once — not on both join and first line
- [ ] Leave 5 for one person, have them arrive → 3 delivered + "(+2 more)"; they speak again → the remaining 2 arrive
- [ ] `!tells` lists pending with ids; `!untell <id>` cancels; a second user's `!untell` on your tell is refused
- [ ] An operator can `!untell` someone else's
- [ ] `CubesBot: !tell dave hi` (nick-prefixed) behaves as bare `!tell`
- [ ] `allow_tell = false` on a channel → `!tell` refused there, but a tell left before the change still delivers
- [ ] `CubesBot: tell dave I said hi when he's back` → `leave_message` fires
- [ ] Leave a tell in `#a`; the recipient's activity in `#b` does NOT deliver it
- [ ] Restart the bot with a pending tell → still delivered afterwards
- [ ] `sqlite3 bot.sqlite "SELECT action, details FROM audit WHERE action LIKE 'tell%'"` shows create/deliver/cancel rows

Added after Task 8's review, which pointed out these command paths have no
automated cover at all — the handlers need a live pydle, so a live run is
the only thing that exercises them:

- [ ] Bare `!untell` (no id) → usage hint, not a crash
- [ ] `!untell abc` (non-numeric id) → usage hint, not a crash
- [ ] `!untell <id>` on someone else's tell: refused for a non-operator, allowed for an operator
- [ ] `!tells` with nothing pending → "no tells waiting from you here."
- [ ] Fire several `!tell`s quickly → the rate-limit message surfaces cleanly, not garbled or doubled
- [ ] A near-miss like `!tellx foo` → treated as ordinary chat, not swallowed and not an error
- [ ] `!tells` on a fresh tell → `human_age` renders "just now" (first live exercise of that path)
- [ ] Start the bot at least once. Nothing automated executes `main.run()`, so a wrong keyword
      argument to `TellStore(...)` or `Scheduler(...)` would pass both pytest and an import check
      and only surface here.
