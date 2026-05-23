"""Tier-4 tools: explicit memory + reminders.

These are the LLM's interface to MemoryStore. The MemoryStore lives on the
ToolContext as `ctx.memory`; if it's None (e.g. embed_model not configured)
these tools degrade with a clear error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from . import Tool, ToolContext, register
from ..db import now_utc_iso

log = logging.getLogger(__name__)


def _no_memory_error() -> dict:
    return {
        "error": "long-term memory is not configured (no embed_model in [ai] config)",
    }


# ---- recall ---------------------------------------------------------------

async def _recall(ctx: ToolContext, args: dict) -> dict:
    if ctx.memory is None:
        return _no_memory_error()
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is empty"}
    k = int(args.get("k", 5))
    k = max(1, min(k, 10))
    hits = await ctx.memory.recall(ctx.channel, query, k=k)
    return {
        "channel": ctx.channel,
        "query": query,
        "results": [
            {
                "id": h.id,
                "kind": h.kind,
                "user_account": h.user_account,
                "content": h.content,
                "similarity": round(h.similarity, 3),
                "created_at": h.created_at,
            }
            for h in hits
        ],
    }


register(Tool(
    name="recall",
    description=(
        "Search the bot's long-term memory of this channel for facts, "
        "preferences, events, or topics relevant to a query. Use whenever "
        "you might already know something about the user or the channel "
        "you'd otherwise have to guess at."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look up. Free text."},
            "k": {"type": "integer", "description": "Max results (1-10).", "default": 5},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    requires={"memory"},
    call=_recall,
))


# ---- remember -------------------------------------------------------------

async def _remember(ctx: ToolContext, args: dict) -> dict:
    if ctx.memory is None:
        return _no_memory_error()
    content = (args.get("content") or "").strip()
    if not content:
        return {"error": "content is empty"}
    kind = args.get("kind", "fact")
    if kind not in ("fact", "preference", "event", "topic"):
        return {"error": f"invalid kind: {kind!r} (must be fact|preference|event|topic)"}
    user_account = args.get("user_account") or None
    new_id = await ctx.memory.add(
        channel=ctx.channel,
        kind=kind,
        content=content,
        user_account=user_account,
        dedup=True,
    )
    if new_id is None:
        return {"stored": False, "reason": "duplicate or near-duplicate of existing memory"}
    return {"stored": True, "id": new_id}


register(Tool(
    name="remember",
    description=(
        "Save a durable fact about this channel or one of its users to "
        "long-term memory. The bot also extracts facts automatically every "
        "20 messages, but call this when something is worth pinning down "
        "right now."
    ),
    schema={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Short, declarative sentence stating the fact.",
            },
            "kind": {
                "type": "string",
                "enum": ["fact", "preference", "event", "topic"],
                "default": "fact",
            },
            "user_account": {
                "type": ["string", "null"],
                "description": "Account name the fact is about, or null for channel-wide.",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
    requires={"memory"},
    call=_remember,
))


# ---- forget ---------------------------------------------------------------

async def _forget(ctx: ToolContext, args: dict) -> dict:
    if ctx.memory is None:
        return _no_memory_error()
    try:
        memory_id = int(args["memory_id"])
    except (TypeError, ValueError):
        return {"error": "memory_id must be an integer"}

    # Look up the memory FIRST so we can report its content back to the LLM.
    # If the LLM mistakenly picks the wrong id, the human will see exactly
    # what was deleted in the bot's reply ("I removed: 'X'") and can correct.
    # Includes deleted_at so we can distinguish "already-forgotten" from
    # "never existed" — soft-deleted rows still exist for audit (M3).
    row = await ctx.db.fetchone(
        "SELECT id, kind, user_account, content, channel, deleted_at "
        "FROM memories WHERE id = ?",
        (memory_id,),
    )
    if row is None:
        return {"forgotten": False, "id": memory_id, "error": "no memory with that id"}
    if row["channel"] != ctx.channel:
        # Refuse cross-channel deletes: each channel's memory is its own scope.
        return {
            "forgotten": False,
            "id": memory_id,
            "error": (
                f"that memory belongs to {row['channel']}, not this channel "
                f"({ctx.channel}). Run recall() in {row['channel']} to manage it."
            ),
        }
    if row["deleted_at"] is not None:
        # Already soft-deleted on a previous call. Idempotent success;
        # don't double-audit and don't pretend we did the work.
        return {
            "forgotten": True,
            "id": memory_id,
            "kind": row["kind"],
            "user_account": row["user_account"],
            "content": row["content"],
            "note": "memory was already forgotten",
        }

    ok = await ctx.memory.forget(memory_id)

    # Audit log (M3): record who forgot what, when, with content preview.
    # Best-effort — log_audit catches its own failures so an audit-write
    # error never poisons the user-facing outcome of the forget itself.
    if ok:
        await ctx.db.log_audit(
            action="memory.forget",
            channel=ctx.channel,
            actor_nick=ctx.actor_nick,
            actor_account=ctx.actor_account,
            details={
                "memory_id": memory_id,
                "kind": row["kind"],
                "user_account": row["user_account"],
                "content_preview": (row["content"] or "")[:200],
            },
        )

    return {
        "forgotten": ok,
        "id": memory_id,
        "kind": row["kind"],
        "user_account": row["user_account"],
        "content": row["content"],  # echo so the LLM can confirm in its reply
    }


register(Tool(
    name="forget",
    description=(
        "Delete a single memory by id. CRITICAL: ALWAYS call recall() first, "
        "review its results, and pick the id whose 'content' matches what the "
        "user wants forgotten. Do not guess at ids. After a successful forget(), "
        "tell the user exactly what was deleted by quoting the returned 'content' "
        "field verbatim, so they can correct you if it was wrong. Refuses to "
        "delete memories that belong to a different channel."
    ),
    schema={
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "integer",
                "description": "ID from a recent recall() result. Do not guess.",
            },
        },
        "required": ["memory_id"],
        "additionalProperties": False,
    },
    requires={"memory"},
    call=_forget,
))


# ---- set_reminder ---------------------------------------------------------

def _parse_when(spec: str) -> datetime | None:
    """Accept either an ISO-8601 timestamp or a relative offset like
    '5m', '2h', '1d', '1h30m'. Returns aware UTC datetime."""
    spec = spec.strip()
    if not spec:
        return None
    # Try ISO first.
    try:
        dt = datetime.fromisoformat(spec.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Relative: number+unit pairs, e.g. "1h30m".
    import re as _re
    total_sec = 0
    matches = _re.findall(r"(\d+)\s*([smhdw])", spec.lower())
    if not matches:
        return None
    unit_to_sec = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    for n, unit in matches:
        total_sec += int(n) * unit_to_sec[unit]
    if total_sec <= 0:
        return None
    return datetime.now(timezone.utc).replace(microsecond=0) + _td(total_sec)


def _td(seconds: int):
    return timedelta(seconds=seconds)


# Per-actor rate limit on set_reminder calls (security review H3).
# In-memory, resets on restart — same pattern as private_msg's _dm_history.
# The trade-off: a determined attacker can spam through a restart, but the
# DB-backed alternative would require a schema change and a query per
# write. For a hobby bot the in-memory state lasts long enough to be
# effective. Adding owner_account to the reminder payload (further below)
# unblocks a future DB-backed enforcement upgrade if needed.
_REMINDER_RATE_LIMIT = 5            # max creations per actor per window
_REMINDER_RATE_WINDOW_SEC = 3600.0  # 1 hour
_REMINDER_MAX_HORIZON_SEC = 90 * 86400  # 90 days — cap on `when`
# See L4 in irc_native.py for the rationale on periodic sweeps. Same
# pattern: actors who set a reminder once never have their entry
# cleaned up otherwise.
_REMINDER_SWEEP_INTERVAL_SEC = 3600.0  # 1 hour (matches the rate window)

_reminder_history: dict[str, list[float]] = {}
_reminder_history_lock = asyncio.Lock()
_reminder_history_last_sweep: float = 0.0


async def _check_reminder_rate(actor_key: str) -> tuple[bool, int]:
    """Returns (allowed, remaining_in_window). actor_key should be the
    requester's account (preferred) or `nick:<lowernick>` as fallback
    so account and nick namespaces can't collide."""
    global _reminder_history_last_sweep
    async with _reminder_history_lock:
        now = time.monotonic()
        # Periodic sweep: drop entries whose entire list is older than the
        # rate window. Prevents unbounded dict growth under long-running
        # operation. (Security review L4.)
        if now - _reminder_history_last_sweep > _REMINDER_SWEEP_INTERVAL_SEC:
            cutoff = now - _REMINDER_RATE_WINDOW_SEC
            stale = [k for k, v in _reminder_history.items() if not v or max(v) < cutoff]
            for k in stale:
                del _reminder_history[k]
            if stale:
                log.debug("reminder rate sweep: dropped %d stale key(s)", len(stale))
            _reminder_history_last_sweep = now
        window_start = now - _REMINDER_RATE_WINDOW_SEC
        hist = _reminder_history.get(actor_key, [])
        fresh = [t for t in hist if t >= window_start]
        if len(fresh) >= _REMINDER_RATE_LIMIT:
            _reminder_history[actor_key] = fresh  # preserve pruned state
            return False, 0
        fresh.append(now)
        _reminder_history[actor_key] = fresh
        return True, _REMINDER_RATE_LIMIT - len(fresh)


async def _set_reminder(ctx: ToolContext, args: dict) -> dict:
    when = _parse_when(args.get("when", ""))
    if when is None:
        return {"error": "could not parse 'when' (try ISO timestamp or e.g. '15m', '2h', '1d')"}

    # Horizon and past-time bounds. Without these, the LLM (under prompt
    # injection or aggressive user request) could set reminders for
    # year-2099 timestamps that survive across many restarts, or pass
    # an ISO date in the past which would fire immediately. Both are
    # abuse vectors. (Security review H3.)
    now_dt = datetime.now(timezone.utc)
    if when > now_dt + timedelta(seconds=_REMINDER_MAX_HORIZON_SEC):
        return {
            "error": (
                f"reminder too far in the future: {when.isoformat()} "
                f"(max {_REMINDER_MAX_HORIZON_SEC // 86400} days from now). "
                "Pick a sooner time."
            ),
        }
    # Allow a 60-second slack for clock skew; anything older is real-past.
    if when < now_dt - timedelta(minutes=1):
        return {
            "error": (
                f"reminder fire_at is in the past: {when.isoformat()}. "
                "Pick a future time."
            ),
        }

    target_nick = args.get("target_nick") or ctx.actor_nick
    # Strip IRC channel-prefix characters from target_nick: the LLM
    # occasionally picks `target_nick = "#geeks"` which would produce a
    # nonsense `reminder for #geeks: ...` post at fire time. Channel
    # names start with #, &, +, or ! per RFC 2811 — strip those plus
    # any leading whitespace so the resulting text reads sensibly.
    # (Security review L5, 2026-05-22.)
    target_nick = target_nick.lstrip(" \t#&+!")
    if not target_nick:
        target_nick = ctx.actor_nick  # fall back to requester if we stripped everything

    message = (args.get("message") or "").strip()
    if not message:
        return {"error": "message is empty"}

    # Per-actor rate limit. Prefer account (stable across nick changes),
    # fall back to nick prefixed with 'nick:' so the namespaces don't
    # collide (account "alice" vs nick "alice" must hash separately).
    actor_key = (
        ctx.actor_account if ctx.actor_account
        else f"nick:{(ctx.actor_nick or '').lower()}"
    )
    allowed, remaining = await _check_reminder_rate(actor_key)
    if not allowed:
        log.info("set_reminder rate-limited for %s", actor_key)
        return {
            "error": (
                f"reminder rate limit: you've set {_REMINDER_RATE_LIMIT} "
                f"reminders in the last hour. Try again later."
            ),
        }

    # Payload carries the owner so the scheduler can log who set what at
    # fire time, and so a future audit / cancellation feature can scope
    # to "reminders set by X." Older rows lack these fields; the fire
    # loop reads with .get() so backward-compatible.
    payload = json.dumps({
        "target_nick": target_nick,
        "message": message,
        "owner_account": ctx.actor_account,
        "owner_nick": ctx.actor_nick,
    })
    new_id = await ctx.db.insert_returning_id(
        "INSERT INTO reminders (channel, fire_at, payload) VALUES (?, ?, ?)",
        (ctx.channel, when.isoformat(), payload),
    )
    return {
        "scheduled": True,
        "id": new_id,
        "fire_at": when.isoformat(),
        "target_nick": target_nick,
        "rate_remaining": remaining,
    }


register(Tool(
    name="set_reminder",
    description=(
        "Schedule a reminder to be posted to this channel at a future time. "
        "When fires, the bot will message the channel mentioning the target "
        "user. NOTE: actual delivery requires the scheduler (slice 2b); "
        "this call persists the reminder regardless."
    ),
    schema={
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "description": (
                    "Either an ISO-8601 UTC timestamp ('2026-04-29T12:00:00Z') or "
                    "a relative offset ('15m', '2h', '1h30m', '1d', '1w')."
                ),
            },
            "target_nick": {
                "type": ["string", "null"],
                "description": "Nick to mention in the reminder. Defaults to the user who asked.",
            },
            "message": {
                "type": "string",
                "description": "Text of the reminder.",
            },
        },
        "required": ["when", "message"],
        "additionalProperties": False,
    },
    requires={"memory"},
    call=_set_reminder,
))
