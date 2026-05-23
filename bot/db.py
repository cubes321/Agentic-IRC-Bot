"""SQLite schema + thin async wrapper. All persistent state lives here."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

log = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  user_account TEXT,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  embedding BLOB,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_channel ON memories(channel);

CREATE TABLE IF NOT EXISTS message_log (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  nick TEXT NOT NULL,
  account TEXT,
  content TEXT NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_channel_ts ON message_log(channel, ts);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  owner_account TEXT,
  owner_nick TEXT,
  goal TEXT NOT NULL,
  status TEXT NOT NULL,
  step_log TEXT NOT NULL DEFAULT '[]',
  result TEXT,
  created_at TEXT NOT NULL,
  deadline TEXT,
  step_cap INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  fire_at TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS open_threads (
  id INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,
  kind TEXT NOT NULL,
  ready_at TEXT,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_cache (
  nick TEXT PRIMARY KEY,
  account TEXT,
  cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  purpose TEXT NOT NULL,
  channel TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON token_usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_purpose ON token_usage(purpose);

-- Forensic audit trail for Tier-5 IRC actions (me_action / set_topic /
-- private_msg) and memory.forget. Each row records who did what when,
-- with action-specific details as a JSON blob. Append-only by design;
-- there's no UPDATE or DELETE on this table from any code path.
-- (Security review M3, 2026-05-22.)
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor_account TEXT,
  actor_nick TEXT,
  channel TEXT,
  action TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action);
"""


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Single-connection async SQLite wrapper. SQLite serialises writes; one
    connection is enough for our scale."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not opened")
        return self._conn

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        # CREATE TABLE IF NOT EXISTS is safe on fresh + existing DBs.
        # Stripping ';' lets us run multi-statement strings.
        for stmt in [s for s in SCHEMA.split(";") if s.strip()]:
            await self._conn.execute(stmt)
        await self._conn.commit()
        # Migrations: additive schema changes for existing DBs. Each is
        # idempotent (catches "duplicate column" so re-running is safe).
        # New deployments get the columns via the CREATE TABLE statements
        # above; the ALTER paths are no-ops for them.
        await self._migrate()
        log.info("Database opened at %s", self.path)

    async def _migrate(self) -> None:
        """Apply additive schema migrations to existing DBs.

        SQLite's ALTER TABLE ADD COLUMN raises OperationalError
        ('duplicate column name') if the column already exists; we
        swallow that specific case so the migration is idempotent.
        Anything else is a real failure and gets logged. New rows in
        the new columns get NULL by default."""
        migrations: list[tuple[str, str]] = [
            # (description, SQL) — keep ordered by introduction date for clarity.
            ("memories.deleted_at column (M3, 2026-05-22)",
             "ALTER TABLE memories ADD COLUMN deleted_at TEXT"),
        ]
        for desc, sql in migrations:
            try:
                await self._conn.execute(sql)
                await self._conn.commit()
                log.info("migration applied: %s", desc)
            except aiosqlite.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" in msg:
                    log.debug("migration no-op (already present): %s", desc)
                else:
                    log.exception("migration failed: %s — sql=%r", desc, sql)
            except Exception:
                log.exception("migration failed unexpectedly: %s", desc)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        await self.conn.executemany(sql, seq)
        await self.conn.commit()

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def insert_returning_id(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self.conn.execute(sql, params) as cur:
            await self.conn.commit()
            return cur.lastrowid or 0

    # ---- convenience helpers used by other modules ----

    async def log_message(
        self,
        channel: str,
        nick: str,
        account: str | None,
        content: str,
    ) -> None:
        await self.execute(
            "INSERT INTO message_log (channel, nick, account, content, ts) VALUES (?, ?, ?, ?, ?)",
            (channel, nick, account, content, now_utc_iso()),
        )

    async def recent_messages(
        self,
        channel: str,
        n: int = 30,
        since_iso: str | None = None,
    ) -> list[aiosqlite.Row]:
        """Return up to `n` most recent messages from a channel, oldest-first.

        If `since_iso` is given, only messages with `ts >= since_iso` are
        included. Useful for ticks that should ignore stale conversation
        history after a quiet stretch.
        """
        if since_iso:
            rows = await self.fetchall(
                "SELECT nick, account, content, ts FROM message_log "
                "WHERE channel = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
                (channel, since_iso, n),
            )
        else:
            rows = await self.fetchall(
                "SELECT nick, account, content, ts FROM message_log "
                "WHERE channel = ? ORDER BY id DESC LIMIT ?",
                (channel, n),
            )
        return list(reversed(rows))

    async def cache_account(self, nick: str, account: str | None) -> None:
        await self.execute(
            "INSERT INTO auth_cache (nick, account, cached_at) VALUES (?, ?, ?) "
            "ON CONFLICT(nick) DO UPDATE SET account=excluded.account, cached_at=excluded.cached_at",
            (nick, account, now_utc_iso()),
        )

    async def get_cached_account(self, nick: str) -> tuple[str | None, str | None]:
        """Return (account, cached_at_iso) or (None, None) if no row."""
        row = await self.fetchone(
            "SELECT account, cached_at FROM auth_cache WHERE nick = ?", (nick,)
        )
        if row is None:
            return None, None
        return row["account"], row["cached_at"]

    # ---- token usage ----

    async def log_usage(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        purpose: str,
        channel: str | None = None,
    ) -> None:
        """Record one LLM call's token consumption. Call sites should pass 0
        for completion_tokens on embedding calls (those have no completion)."""
        total = (prompt_tokens or 0) + (completion_tokens or 0)
        await self.execute(
            "INSERT INTO token_usage "
            "(ts, model, prompt_tokens, completion_tokens, total_tokens, purpose, channel) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_utc_iso(), model, prompt_tokens or 0, completion_tokens or 0,
             total, purpose, channel),
        )

    async def log_audit(
        self,
        *,
        action: str,
        channel: str | None,
        actor_nick: str | None,
        actor_account: str | None,
        details: dict | None = None,
    ) -> None:
        """Record a security-relevant action (Tier-5 IRC, memory.forget) to
        the `audit` table for forensic review. Failures are caught and
        logged — audit is best-effort; never raise out of this call,
        because raising here would interfere with the action that the
        audit is recording. (Security review M3, 2026-05-22.)"""
        try:
            await self.execute(
                "INSERT INTO audit "
                "(ts, actor_account, actor_nick, channel, action, details) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now_utc_iso(),
                    actor_account,
                    actor_nick,
                    channel,
                    action,
                    json.dumps(details or {}, default=str),
                ),
            )
        except Exception:
            log.exception("audit log failed for action=%s channel=%s", action, channel)

    async def usage_summary(
        self,
        since_iso: str | None = None,
    ) -> dict:
        """Return totals + per-purpose + per-model breakdowns. If since_iso is
        provided, only counts rows with ts >= since_iso."""
        where = " WHERE ts >= ?" if since_iso else ""
        params: tuple = (since_iso,) if since_iso else ()

        totals_row = await self.fetchone(
            "SELECT COALESCE(SUM(prompt_tokens), 0) AS p, "
            "       COALESCE(SUM(completion_tokens), 0) AS c, "
            "       COALESCE(SUM(total_tokens), 0) AS t, "
            "       COUNT(*) AS n "
            f"FROM token_usage{where}",
            params,
        )

        purpose_rows = await self.fetchall(
            "SELECT purpose, "
            "       COALESCE(SUM(prompt_tokens), 0) AS p, "
            "       COALESCE(SUM(completion_tokens), 0) AS c, "
            "       COUNT(*) AS n "
            f"FROM token_usage{where} "
            "GROUP BY purpose ORDER BY (SUM(total_tokens)) DESC",
            params,
        )

        model_rows = await self.fetchall(
            "SELECT model, "
            "       COALESCE(SUM(prompt_tokens), 0) AS p, "
            "       COALESCE(SUM(completion_tokens), 0) AS c, "
            "       COUNT(*) AS n "
            f"FROM token_usage{where} "
            "GROUP BY model ORDER BY (SUM(total_tokens)) DESC",
            params,
        )

        return {
            "totals": dict(totals_row) if totals_row else {"p": 0, "c": 0, "t": 0, "n": 0},
            "by_purpose": [dict(r) for r in purpose_rows],
            "by_model": [dict(r) for r in model_rows],
        }

    # ---- reminders ----

    async def due_reminders(self, now_iso: str | None = None) -> list[aiosqlite.Row]:
        """Return reminders whose fire_at <= now_iso (default: current UTC).
        Used by Scheduler each poll cycle. Caller deletes after delivery."""
        ts = now_iso or now_utc_iso()
        return await self.fetchall(
            "SELECT id, channel, fire_at, payload FROM reminders "
            "WHERE fire_at <= ? ORDER BY fire_at ASC",
            (ts,),
        )

    async def delete_reminder(self, reminder_id: int) -> None:
        await self.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))

    # ---- memory diagnostics ----

    async def memory_stats(self, channel: str | None = None) -> dict:
        """Per-channel memory statistics for the !memory_stats command.
        Returns counts, oldest/newest dates, and per-kind breakdown.
        Only counts ACTIVE memories (deleted_at IS NULL); soft-deleted
        rows are excluded so the operator sees the same total the LLM
        sees via recall. (M3.)"""
        # The base condition is "active row" — deleted_at IS NULL.
        # Channel filter is layered on top when supplied.
        if channel:
            where = " WHERE channel = ? AND deleted_at IS NULL"
            params: tuple = (channel,)
        else:
            where = " WHERE deleted_at IS NULL"
            params = ()

        totals = await self.fetchone(
            "SELECT COUNT(*) AS n, "
            "       MIN(created_at) AS oldest, "
            "       MAX(created_at) AS newest "
            f"FROM memories{where}",
            params,
        )

        kinds = await self.fetchall(
            "SELECT kind, COUNT(*) AS n "
            f"FROM memories{where} "
            "GROUP BY kind ORDER BY n DESC",
            params,
        )

        # Top users with the most memories about them (channel-scoped if given).
        top_users = await self.fetchall(
            "SELECT COALESCE(user_account, '(channel-wide)') AS who, "
            "       COUNT(*) AS n "
            f"FROM memories{where} "
            "GROUP BY who ORDER BY n DESC LIMIT 5",
            params,
        )

        return {
            "channel": channel,
            "total": (totals["n"] if totals else 0),
            "oldest": (totals["oldest"] if totals else None),
            "newest": (totals["newest"] if totals else None),
            "by_kind": [dict(r) for r in kinds],
            "top_users": [dict(r) for r in top_users],
        }
