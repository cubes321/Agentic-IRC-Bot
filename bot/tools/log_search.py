"""Search the channel message log. Substring (LIKE) match, channel-scoped
by design — the bot can search the current channel's history but not other
channels'. Keeps per-channel privacy intact (a query in #foo cannot surface
what was said in #bar)."""

from __future__ import annotations

import logging

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


# Hard ceiling on results to keep the LLM's context bounded. The tool
# description tells the model to use a focused query rather than asking
# for hundreds of rows.
MAX_RESULTS = 30
DEFAULT_RESULTS = 10
# Per-row content truncation. Long lines (multi-paragraph pastes) blow
# the context budget for marginal added value — the LLM only needs enough
# to identify the message and the surrounding meaning.
MAX_CONTENT_CHARS = 280


def _is_channel_target(channel: str) -> bool:
    """The ctx.channel may be a bot nick in DM context; we only want to
    serve searches for real channels. Channel names start with # & + or !
    per RFC 2811."""
    return bool(channel) and channel[0] in "#&+!"


def _escape_like(s: str) -> str:
    """Escape SQL LIKE metacharacters (% and _) so a user searching for
    e.g. '_50_' gets literal underscore matches, not wildcard matches.
    Paired with `ESCAPE '\\'` in the query."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _log_search(ctx: ToolContext, args: dict) -> dict:
    if not _is_channel_target(ctx.channel):
        # Searching message_log from a DM has no obvious scope (the user is
        # in N channels; we'd have to pick one or merge). Refuse rather
        # than guess. The LLM can tell the user to ask in a channel.
        return {
            "error": (
                "log_search only works in a channel — there is no per-channel "
                "history to search from a private message."
            ),
        }

    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is empty"}
    if len(query) > 200:
        return {"error": "query too long (max 200 chars)"}

    # Optional speaker filter. Case-insensitive match against the stored nick.
    nick_filter = (args.get("nick") or "").strip() or None

    try:
        limit = int(args.get("limit", DEFAULT_RESULTS))
    except (TypeError, ValueError):
        limit = DEFAULT_RESULTS
    limit = max(1, min(limit, MAX_RESULTS))

    # Build the WHERE clauses. Always pinned to ctx.channel (privacy boundary).
    like_pattern = f"%{_escape_like(query)}%"
    sql_parts = [
        "SELECT nick, account, content, ts FROM message_log",
        "WHERE channel = ?",
        "  AND content LIKE ? ESCAPE '\\'",
    ]
    params: list = [ctx.channel, like_pattern]
    if nick_filter:
        sql_parts.append("  AND lower(nick) = lower(?)")
        params.append(nick_filter)
    sql_parts.append("ORDER BY id DESC LIMIT ?")
    params.append(limit)

    rows = await ctx.db.fetchall(" ".join(sql_parts), tuple(params))

    # Format results: most-recent first, content truncated for context safety.
    # The LLM gets enough to identify each line and quote relevant bits in
    # its reply without us shoving every multi-paragraph paste at it.
    results = []
    for r in rows:
        content = r["content"] or ""
        if len(content) > MAX_CONTENT_CHARS:
            content = content[: MAX_CONTENT_CHARS - 1] + "…"
        results.append({
            "nick": r["nick"],
            "account": r["account"],
            "content": content,
            "ts": r["ts"],
        })

    return {
        "channel": ctx.channel,
        "query": query,
        "nick_filter": nick_filter,
        "match_count": len(results),
        "results": results,
        # If we hit the limit, tell the LLM there may be more, so it can ask
        # the user for a narrower query rather than assuming this was the full set.
        "truncated": len(results) == limit,
    }


register(Tool(
    name="log_search",
    description=(
        "Search the current channel's message history for lines containing a "
        "substring. Returns matching rows newest-first with nick, timestamp, "
        "and content. Channel-scoped: can only search the channel the request "
        "came from, never other channels. Use to answer 'what did X say about "
        "Y?' or 'when did we last discuss Z?'. Use a specific phrase rather "
        "than a single common word — broad queries return generic noise."
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Substring to search for (case-sensitive in current SQLite "
                    "collation). Quote a phrase for best results."
                ),
            },
            "nick": {
                "type": ["string", "null"],
                "description": "Optional: only return lines from this nick.",
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Max results (1-{MAX_RESULTS}). Default {DEFAULT_RESULTS}."
                ),
                "default": DEFAULT_RESULTS,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    requires=set(),
    call=_log_search,
))
