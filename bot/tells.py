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

        note = " (truncated to 400 chars)" if truncated else ""
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
