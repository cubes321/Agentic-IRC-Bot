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
