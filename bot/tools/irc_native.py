"""Tier-5 tools: IRC-native actions (me / topic / private message).

These are the only tools that *act on the IRC layer itself* rather than
returning information for the LLM to weave into a reply. Each one is gated
by per-channel `allow_actions` policy via the `action:<kind>` requires-tag
that `build_catalog` understands — so adding/removing entries in a channel's
`allow_actions` list changes which tools the LLM even sees in that channel.

Sanitisation: text passes through the same IRC-line cleanup as `irc_send`
(printable chars, trimmed, ≤400 bytes). For private_msg we reuse `irc_send`
directly so multi-line DMs follow the same rate-paced delivery as channel
replies. For me_action and set_topic we sanitise inline because both are
inherently single-line.
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


# ---- inline sanitisation (shared by me_action + set_topic) -----------------

_MAX_LINE = 400  # matches MAX_LINE_LEN in ircclient.py


def _clean_single_line(text: str) -> str:
    """Strip control chars, collapse newlines to spaces, truncate to ≤400.

    me_action and set_topic are single-line by design: actions render with a
    `/me` prefix in clients (a newline mid-action looks broken), and topics
    are one string at the IRC protocol level. Collapse rather than refuse —
    the LLM occasionally adds a stray newline and we don't want to make it
    re-call the tool over a trivial fixup.
    """
    if not text:
        return ""
    # Newlines collapse to spaces; other non-printables (except space) are dropped.
    out = []
    for c in text.replace("\n", " ").replace("\r", " "):
        if c == " " or c.isprintable():
            out.append(c)
    cleaned = "".join(out).strip()
    if len(cleaned) > _MAX_LINE:
        cleaned = cleaned[: _MAX_LINE - 1] + "…"
    return cleaned


def _is_channel(target: str) -> bool:
    """IRC channels start with #, &, +, or ! (RFC 2811). Anything else is a nick."""
    return bool(target) and target[0] in "#&+!"


# ---- me_action -------------------------------------------------------------

def _normalise_action_text(text: str) -> str:
    """Strip common model fumbles from action text before wire-encoding.

    Local models sometimes pass `/me waves` or `*waves*` as `text`, having
    pattern-matched on the IRC convention rather than the tool's contract.
    Without this normalisation, the wire output becomes
    `* BotNick /me waves` or `* BotNick *waves*` — both visibly broken.

    Defensive but bounded: only strips leading `/me ` and a single layer
    of surrounding `*` (and only if the WHOLE string is wrapped). Anything
    more elaborate is the model's intent and stays."""
    t = text.strip()
    # Strip a leading "/me" (with or without space) — model echoing the IRC verb.
    lower = t.lower()
    if lower.startswith("/me "):
        t = t[4:].lstrip()
    elif lower == "/me":
        t = ""
    # Strip surrounding asterisks if the whole thing is wrapped: "*waves*" -> "waves".
    if len(t) >= 2 and t.startswith("*") and t.endswith("*") and t.count("*") == 2:
        t = t[1:-1].strip()
    return t


async def _me_action(ctx: ToolContext, args: dict) -> dict:
    raw = args.get("text", "")
    text = _clean_single_line(_normalise_action_text(raw))
    if not text:
        return {"error": "text is empty (after stripping leading /me or surrounding asterisks)"}
    if not _is_channel(ctx.channel):
        return {"error": f"me_action can only be used in a channel, not in {ctx.channel}"}
    # CTCP ACTION is the wire-level format clients render as `/me ...`.
    # Pydle has no first-class action() helper; the canonical form is a
    # PRIVMSG wrapped in \x01ACTION ...\x01.
    try:
        await ctx.bot.message(ctx.channel, f"\x01ACTION {text}\x01")
    except Exception as e:
        log.exception("me_action failed in %s", ctx.channel)
        return {"error": f"send failed: {e}"}
    log.info("me_action in %s: %r", ctx.channel, text[:120])
    return {"sent": True, "channel": ctx.channel, "text": text}


register(Tool(
    name="me_action",
    description=(
        "Perform an IRC `/me` action in the current channel — the ONLY correct "
        "way to do third-person actions in IRC. Renders as '* BotNick waves' "
        "in clients. ALWAYS use this tool for actions; DO NOT write plain text "
        "wrapped in asterisks like '*waves*' or '*me waves*' — those render "
        "literally as the characters '*waves*', not as an action, and look "
        "broken. Pass just the action verb phrase as `text`, without the "
        "leading '/me' or asterisks (e.g. text='waves at Alice', NOT "
        "text='/me waves' or text='*waves*'). Channel-only; cannot be used "
        "in DMs. Use sparingly for personality flourishes."
    ),
    schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The action text (after the `/me`). Single line, ≤400 chars.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    requires={"action:me"},
    call=_me_action,
))


# ---- set_topic -------------------------------------------------------------

async def _set_topic(ctx: ToolContext, args: dict) -> dict:
    text = _clean_single_line(args.get("topic", ""))
    if not text:
        return {"error": "topic is empty"}
    if not _is_channel(ctx.channel):
        return {"error": f"set_topic can only be used in a channel, not in {ctx.channel}"}
    # Pydle's set_topic sends TOPIC #chan :text. Falls back to rawmsg for
    # maximum portability if the method is absent on an older pydle.
    try:
        if hasattr(ctx.bot, "set_topic"):
            await ctx.bot.set_topic(ctx.channel, text)
        else:
            await ctx.bot.rawmsg("TOPIC", ctx.channel, text)
    except Exception as e:
        # Most common cause: bot lacks +o on the channel and the IRCd
        # rejects the TOPIC with ERR_CHANOPRIVSNEEDED (482). Pydle doesn't
        # raise on numerics; this catch is mostly for true network errors.
        log.exception("set_topic failed in %s", ctx.channel)
        return {"error": f"send failed: {e}"}
    log.info("set_topic in %s: %r", ctx.channel, text[:120])
    # Note: we report 'sent' rather than 'confirmed' because IRC TOPIC is
    # fire-and-forget at this layer — the server may still reject it for
    # permission reasons (numeric 482) which arrives as a server message,
    # not an exception. If the bot doesn't see the topic change reflected,
    # it can ask the user and try a different tactic.
    return {"sent": True, "channel": ctx.channel, "topic": text}


register(Tool(
    name="set_topic",
    description=(
        "Set the topic of the current channel. The bot must be opped (+o) or "
        "the channel must allow non-op topic changes (mode -t) for this to "
        "take effect — the IRCd silently rejects unauthorised TOPIC commands. "
        "Returns 'sent' on transmission, not 'applied'; if the topic does not "
        "change in the channel, the bot lacks permission. Channel-only."
    ),
    schema={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "New channel topic. Single line, ≤400 chars.",
            },
        },
        "required": ["topic"],
        "additionalProperties": False,
    },
    requires={"action:topic"},
    call=_set_topic,
))


# ---- private_msg -----------------------------------------------------------

# Module-global rate-limit state. Key: lower-cased target nick. Value: list of
# epoch-seconds timestamps of recent sends. We prune entries older than
# RATE_WINDOW_SEC on every check, then enforce ≤RATE_LIMIT sends per window.
# Anti-spam scope — per-target, not per-channel — so a target who's been DMed
# 3 times in the last 60s from #foo cannot be hit again from #bar this minute.
# Resets on bot restart; that's acceptable for anti-spam (not for auditing).
RATE_WINDOW_SEC = 60.0
RATE_LIMIT = 3
_dm_history: dict[str, list[float]] = {}
_dm_history_lock = asyncio.Lock()


async def _check_and_record_dm(target_lower: str) -> tuple[bool, int]:
    """Returns (allowed, remaining_in_window). If not allowed, remaining=0."""
    async with _dm_history_lock:
        now = time.monotonic()
        window_start = now - RATE_WINDOW_SEC
        hist = _dm_history.setdefault(target_lower, [])
        # Prune in place — keeps the dict from growing without bound for chatty targets.
        fresh = [t for t in hist if t >= window_start]
        _dm_history[target_lower] = fresh
        if len(fresh) >= RATE_LIMIT:
            return False, 0
        fresh.append(now)
        return True, RATE_LIMIT - len(fresh)


async def _private_msg(ctx: ToolContext, args: dict) -> dict:
    target = (args.get("target_nick") or "").strip()
    if not target:
        return {"error": "target_nick is empty"}
    if _is_channel(target):
        return {
            "error": (
                f"target_nick {target!r} looks like a channel. Use a plain "
                "reply for channel messages; private_msg is for DMs only."
            ),
        }
    if target.lower() == ctx.bot.nickname.lower():
        return {"error": "refusing to DM myself"}

    text = (args.get("message") or "").strip()
    if not text:
        return {"error": "message is empty"}

    allowed, remaining = await _check_and_record_dm(target.lower())
    if not allowed:
        log.info("private_msg: rate-limited to %s", target)
        return {
            "error": (
                f"rate-limited: {RATE_LIMIT} DMs already sent to {target} in "
                f"the last {int(RATE_WINDOW_SEC)}s. Try again later."
            ),
        }

    # Reuse the bot's irc_send so DMs get the same sanitisation, line-splitting,
    # MAX_LINES_PER_REPLY cap, and INTER_LINE_DELAY pacing as channel replies.
    try:
        await ctx.bot.irc_send(target, text)
    except Exception as e:
        log.exception("private_msg send to %s failed", target)
        return {"error": f"send failed: {e}"}

    log.info(
        "private_msg to %s (from %s in %s): %r",
        target, ctx.actor_nick, ctx.channel, text[:120],
    )
    return {
        "sent": True,
        "target_nick": target,
        "rate_remaining": remaining,
    }


register(Tool(
    name="private_msg",
    description=(
        "Send a private message (DM) to a specific user. Rate-limited to "
        f"{RATE_LIMIT} DMs per {int(RATE_WINDOW_SEC)}s per target across all "
        "channels — be sparing. Use only when the message is specifically for "
        "that user and would be noise in the channel (e.g. delivering a "
        "private reminder, sharing a long quote, replying to a sensitive "
        "question). For normal channel conversation, just reply in the channel."
    ),
    schema={
        "type": "object",
        "properties": {
            "target_nick": {
                "type": "string",
                "description": "Nick to DM. Must not be a channel name or the bot itself.",
            },
            "message": {
                "type": "string",
                "description": "Message text. Multi-line allowed; will be split per line.",
            },
        },
        "required": ["target_nick", "message"],
        "additionalProperties": False,
    },
    requires={"action:msg"},
    call=_private_msg,
))
