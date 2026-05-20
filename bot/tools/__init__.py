"""Tool registry. Each tool module imports `register` and registers itself at
import time. The agent core asks for a per-channel catalog by intersecting the
registry with the channel's policy and the bot's capabilities (e.g. whether a
vision model is loaded, whether a search backend is configured)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    """Everything a tool's call() function needs. Built per turn."""
    bot: Any                # IRCBot instance, for IRC-native tools
    channel: str
    actor_nick: str
    actor_account: str | None
    cfg: Any                # bot.config.Config
    db: Any                 # bot.db.Database
    http: Any               # httpx.AsyncClient
    llm_chat: Any           # openai.AsyncOpenAI client
    llm_vision: Any         # openai.AsyncOpenAI client (may equal llm_chat)
    memory: Any = None      # bot.memory.MemoryStore | None


@dataclass
class Tool:
    name: str
    description: str
    schema: dict             # JSON Schema for parameters; OpenAI tools API
    requires: set[str]       # capability tags ("vision", "brave_key", "action:topic", ...)
    call: Callable[[ToolContext, dict], Awaitable[dict]]


REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    if tool.name in REGISTRY:
        raise ValueError(f"duplicate tool: {tool.name}")
    REGISTRY[tool.name] = tool
    return tool


def build_catalog(capabilities: set[str], policy_action_filter: set[str] | None = None) -> list[Tool]:
    """Return tools whose requires-set is satisfied. `policy_action_filter` is a
    set of action kinds the channel allows; tools tagged `action:<kind>` are
    only included if `<kind>` is in this set."""
    out: list[Tool] = []
    for tool in REGISTRY.values():
        # Capability check
        if not tool.requires.issubset(capabilities):
            # action:<kind> requirements are checked separately below; treat
            # them as implicitly satisfied at the capability layer.
            non_action = {r for r in tool.requires if not r.startswith("action:")}
            if not non_action.issubset(capabilities):
                continue
        # Action gating
        action_reqs = {r[len("action:"):] for r in tool.requires if r.startswith("action:")}
        if action_reqs:
            if policy_action_filter is None or not action_reqs.issubset(policy_action_filter):
                continue
        out.append(tool)
    return out


def to_openai_schema(tools: list[Tool]) -> list[dict]:
    """Convert Tool objects to the OpenAI tools API schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.schema,
            },
        }
        for t in tools
    ]


# Import side-effect: each module's import calls register(...).
# Comment out a line here to disable a tool category.
from . import web           # noqa: E402,F401
from . import url           # noqa: E402,F401
from . import wiki          # noqa: E402,F401
from . import calc_tool     # noqa: E402,F401
from . import unit_convert  # noqa: E402,F401
from . import youtube       # noqa: E402,F401
from . import log_search    # noqa: E402,F401
from . import vision        # noqa: E402,F401  (gated by requires={"vision"})
from . import memory_tools  # noqa: E402,F401  (Tier-4: recall/remember/forget/set_reminder)
from . import irc_native    # noqa: E402,F401  (Tier-5: me_action/set_topic/private_msg)
