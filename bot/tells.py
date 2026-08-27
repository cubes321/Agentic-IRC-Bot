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

    async def pending_for(
        self, channel: str, nick: str, account: str | None
    ) -> list[Any]:
        """Undelivered, unexpired tells waiting for this person in this
        channel, oldest first.

        Hybrid match: the arriving nick equals the stored nick (case
        insensitive), OR their account equals the account we resolved when
        the tell was left. The second arm is what catches `dave` coming
        back as `dave_`.

        The `to_account IS NOT NULL` guard is load-bearing. Without it, an
        arriving user with no account (account = None) would match every
        tell whose to_account is also NULL — i.e. the first person to speak
        would collect everyone's messages."""
        return await self.db.fetchall(
            "SELECT * FROM tells "
            "WHERE channel = ? AND delivered_at IS NULL AND expires_at > ? "
            "AND (to_nick_lower = ? "
            "     OR (to_account IS NOT NULL AND ? IS NOT NULL AND to_account = ?)) "
            "ORDER BY created_at ASC, id ASC",
            (channel, now_utc_iso(), (nick or "").lower(), account, account),
        )

    @staticmethod
    def _clean_target_nick(raw: str) -> str | None:
        """Strip channel prefixes and whitespace, then validate the shape.
        Returns None if what's left isn't a usable nick."""
        candidate = (raw or "").strip().lstrip(_CHANNEL_PREFIXES).strip()
        if not candidate or not NICK_RE.match(candidate):
            return None
        return candidate
