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

# How many tells the bot will hand over in one arrival. More than this and
# the rest wait for the recipient's next activity. IRC servers kill clients
# that flood: ircu-based networks (Quakenet) accumulate fake-lag per
# PRIVMSG and disconnect past ~10 seconds of debt — the same failure the
# task-result wire cap exists to prevent.
TELL_MAX_DELIVER_PER_ARRIVAL = 3

# Cap on how many tells `!tells` lists. Mirrors tasks.LIST_LIMIT. The store
# enforces it rather than trusting the caller: an unbounded list handed to a
# handler that posts one line per row is an excess-flood disconnect waiting
# to happen (see the task-result cap in tasks.py for the same lesson).
# Deliberately one more than MAX_TELL_LIST_WIRE_LINES in ircclient: the
# extra row is what tells the handler there IS a remainder to flag. Fetching
# more than that would be rows nobody can ever be shown.
TELL_LIST_LIMIT = 9

# RFC 2812 nick shape: a letter or one of the "special" characters first,
# then letters, digits, specials, or '-'. Without this check,
# `!tell hi there everyone` silently files a tell for a user named "hi".
NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}\-]{0,31}$")

# One definition of "is this tell for this person", shared by the delivery
# lookup and the cap counter. Written once because the two diverged the
# first time round: pending_for matched nick-OR-account while _count_pending
# counted nick only, so a sender could walk straight past the per-recipient
# cap by spelling the target's nick three different ways. Parameters, in
# order: to_nick_lower, account, account.
_RECIPIENT_MATCH_SQL = (
    "(to_nick_lower = ? "
    " OR (to_account IS NOT NULL AND ? IS NOT NULL AND to_account = ?))"
)

# Channel name prefixes per RFC 2811 — one definition, exported, because
# two places need it and a narrower second copy is a bug waiting to happen:
# a `+`- or `!`-prefixed channel misread as a DM would refuse a legitimate
# tell. `bot/tools/tell.py` imports this for its is-this-a-channel check.
CHANNEL_PREFIXES = "#&+!"
# The lstrip variant adds whitespace. Stripped from a target nick before
# validation — the LLM occasionally passes a channel name as the target
# (the same failure mode set_reminder hit in security review L5).
_CHANNEL_PREFIXES = " \t" + CHANNEL_PREFIXES


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

        target = self.clean_target_nick(to_nick)
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
            pair_pending = await self._count_pending(
                channel, target_lower, to_account, from_key
            )
            if pair_pending >= TELL_MAX_PENDING_PER_PAIR:
                return TellResult(
                    False,
                    f"(you already have {pair_pending} message(s) waiting for "
                    f"{target} here — use !untell to clear one first)",
                )

            total_pending = await self._count_pending(
                channel, target_lower, to_account, None
            )
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
            details={
                "tell_id": tell_id,
                "to_nick": target,
                "chars": len(body),
                "message_preview": body[:200],
            },
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
        self,
        channel: str,
        to_nick_lower: str,
        to_account: str | None,
        from_key: str | None,
    ) -> int:
        """Pending, unexpired tells for a recipient in a channel. With
        `from_key`, narrowed to those from one sender.

        Uses the same nick-OR-account predicate as pending_for
        (_RECIPIENT_MATCH_SQL) rather than nick alone — a narrower
        predicate here than in pending_for would let a sender land more
        tells on one account-holding recipient than the cap allows, simply
        by spelling their nick a few different ways."""
        params: list[Any] = [channel, now_utc_iso(), to_nick_lower, to_account, to_account]
        sql = (
            "SELECT COUNT(*) AS n FROM tells "
            "WHERE channel = ? AND delivered_at IS NULL AND expires_at > ? "
            f"AND {_RECIPIENT_MATCH_SQL}"
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
            f"AND {_RECIPIENT_MATCH_SQL} "
            "ORDER BY created_at ASC, id ASC",
            (channel, now_utc_iso(), (nick or "").lower(), account, account),
        )

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

    async def list_from(
        self, channel: str, nick: str, account: str | None
    ) -> list[Any]:
        """Pending tells this person left in this channel, oldest first.
        Identity is from_key, so it follows their account across nick
        changes rather than being defeated by one.

        Capped at TELL_LIST_LIMIT: the caller posts one wire line per row,
        so an unbounded result here is an excess-flood disconnect waiting
        to happen (see the module docstring on TELL_LIST_LIMIT)."""
        return await self.db.fetchall(
            "SELECT * FROM tells "
            "WHERE channel = ? AND from_key = ? "
            "AND delivered_at IS NULL AND expires_at > ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (channel, actor_key(nick, account), now_utc_iso(), TELL_LIST_LIMIT),
        )

    async def cancel(
        self,
        channel: str,
        tell_id: int,
        actor_nick: str,
        actor_account: str | None,
        is_operator: bool,
    ) -> TellResult:
        """Withdraw a tell before it is delivered. You may cancel your own;
        operators may cancel any. Already-delivered tells cannot be
        cancelled — that message is out, and pretending otherwise would be
        worse than saying so.

        Channel-scoped (an id from #other is invisible here), and the two
        refusal cases — no such id, and "it's not yours" — return the
        identical message. Without both properties, probing !untell across
        ids would let anyone map which ids are live pending tells anywhere
        on the bot, and the success message would otherwise echo a
        recipient nick from another channel into this one."""
        row = await self.db.fetchone(
            "SELECT * FROM tells WHERE id = ? AND channel = ?", (tell_id, channel)
        )
        refusal = TellResult(False, f"(no pending tell #{tell_id} of yours here)")
        if row is None or row["delivered_at"] is not None:
            return refusal

        if not is_operator and row["from_key"] != actor_key(actor_nick, actor_account):
            log.info(
                "!untell refused: %s is not the owner of tell #%d",
                actor_nick, tell_id,
            )
            return refusal

        await self.db.execute("DELETE FROM tells WHERE id = ?", (tell_id,))
        await self.db.log_audit(
            action="tell.cancel",
            channel=row["channel"],
            actor_nick=actor_nick,
            actor_account=actor_account,
            details={
                "tell_id": tell_id,
                "to_nick": row["to_nick"],
                "by_operator": is_operator,
                "message_preview": (row["message"] or "")[:200],
            },
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

    @staticmethod
    def clean_target_nick(raw: str) -> str | None:
        """Strip channel prefixes and whitespace, then validate the shape.
        Returns None if what's left isn't a usable nick.

        Public because callers must resolve a target's account against the
        SAME cleaned token this method will store — looking up the raw
        token misses the account cache and quietly files an account-less
        tell."""
        candidate = (raw or "").strip().lstrip(_CHANNEL_PREFIXES).strip()
        # People address each other as "dave," or "dave:" in chat, so those
        # arrive as part of the target token. Strip them rather than refusing
        # a request that is obviously well-formed to a human.
        candidate = candidate.rstrip(",:").strip()
        if not candidate or not NICK_RE.match(candidate):
            return None
        return candidate
