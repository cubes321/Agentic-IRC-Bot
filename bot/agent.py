"""Agent core: the LLM tool-call loop.

Defensive features (the local-model failure modes the design called out):
  - hard step cap + wall-clock deadline
  - hallucinated tool name -> repair message lists real tools
  - bad-JSON args -> repair message echoes the schema
  - same (name, args) twice in a row -> nudge to stop or change
  - on budget exhaustion -> one final no-tools call to write a polite summary
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from openai import AsyncOpenAI
from openai import APIError, APIConnectionError

from . import prompts
from .config import Config
from .db import Database
from .policy import ChannelPolicy
from .tools import Tool, ToolContext, build_catalog, to_openai_schema

log = logging.getLogger(__name__)


class _NullCtx:
    """No-op async context manager. Used when no semaphore is configured so
    the same `async with _maybe_sem(...)` site works in both modes."""
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False


def _maybe_sem(sem: asyncio.Semaphore | None):
    return sem if sem is not None else _NullCtx()


# Models in the Qwen 3 family (and a few others, including some DeepSeek
# variants) emit explicit reasoning between <think>...</think> tags before
# the actual response. We never want that posted to IRC. The strip is also
# defensive against case variants and unclosed tags ("the model started
# thinking and ran out of budget mid-thought").
import re as _re
_THINK_RE = _re.compile(r"<think>.*?</think>", flags=_re.IGNORECASE | _re.DOTALL)
_OPEN_THINK_RE = _re.compile(r"<think>.*$", flags=_re.IGNORECASE | _re.DOTALL)


def _strip_thinking(text: str) -> tuple[str, int]:
    """Strip <think>...</think> blocks from a model's reply. Returns
    (stripped_text, chars_removed). The char count is a proxy for "how
    much did the model burn on reasoning" — useful for diagnostics."""
    if not text:
        return text, 0
    original_len = len(text)
    cleaned = _THINK_RE.sub("", text)
    # Defense against truncated / unclosed thinking blocks
    cleaned = _OPEN_THINK_RE.sub("", cleaned)
    return cleaned.strip(), original_len - len(cleaned.strip())


def _format_recent(rows: list) -> str:
    if not rows:
        return "(no recent messages)"
    lines = []
    for r in rows:
        nick = r["nick"]
        content = r["content"]
        lines.append(f"<{nick}> {content}")
    return "\n".join(lines)


def _validate_args(args_str: str, schema: dict) -> tuple[dict | None, str | None]:
    """Returns (args_dict, error_message). Lightweight: parses JSON, checks
    required fields. Tools may do further validation in their call()."""
    if not args_str:
        args_str = "{}"
    try:
        args = json.loads(args_str)
    except json.JSONDecodeError as e:
        return None, f"arguments are not valid JSON: {e.msg}"
    if not isinstance(args, dict):
        return None, "arguments must be a JSON object"
    required = schema.get("required") or []
    missing = [k for k in required if k not in args]
    if missing:
        return None, f"missing required argument(s): {', '.join(missing)}"
    return args, None


class AgentCore:
    def __init__(
        self,
        chat_client: AsyncOpenAI,
        vision_client: AsyncOpenAI,
        db: Database,
        cfg: Config,
        capabilities: set[str],
        memory: Any = None,                          # bot.memory.MemoryStore | None
        chat_semaphore: asyncio.Semaphore | None = None,
    ):
        self.chat = chat_client
        self.vision = vision_client
        self.db = db
        self.cfg = cfg
        self.capabilities = capabilities
        self.memory = memory
        # Concurrency cap on outbound chat completions. Shared with MemoryStore
        # and Scheduler so all background users contend for the same slot pool.
        # If None, no cap is applied (single-call workloads, tests).
        self._chat_sem = chat_semaphore

    async def _log_usage(
        self,
        resp: Any,
        model: str,
        purpose: str,
        channel: str | None,
    ) -> None:
        """Record token usage from an OpenAI SDK response if present.
        Failures here are logged but never raised — usage tracking is
        diagnostic, not load-bearing."""
        try:
            usage = getattr(resp, "usage", None)
            if usage is None:
                return
            await self.db.log_usage(
                model=model,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                purpose=purpose,
                channel=channel,
            )
        except Exception:
            log.exception("failed to log token usage for %s", purpose)

    async def run_reply_turn(
        self,
        bot: Any,        # IRCBot, kept untyped to avoid circular imports
        channel: str,
        policy: ChannelPolicy,
        actor_nick: str,
        actor_account: str | None,
        trigger_text: str,
        http: Any,
    ) -> str | None:
        """Runs the agent loop for a 'reply' turn (mention or addressed-to-bot).
        Returns the final text to post, or None if the model said nothing."""
        tools = build_catalog(self.capabilities, set(policy.cfg.allow_actions))
        tools_schema = to_openai_schema(tools) if tools else None

        recent = await self.db.recent_messages(channel, n=20)
        messages: list[dict] = [
            {
                "role": "system",
                "content": prompts.REPLY_SYSTEM.format(
                    persona=policy.persona,
                    channel=channel,
                    nick=self.cfg.server.nick,
                ),
            },
            {
                "role": "system",
                "content": "Recent channel activity (most recent last):\n" + _format_recent(recent),
            },
        ]

        # Auto-recall: if enabled, do an implicit memory lookup on the user's
        # message and prepend the hits as a system message. This eliminates
        # the need for the model to remember to call recall() itself.
        if policy.auto_recall and self.memory is not None:
            try:
                hits = await self.memory.recall(
                    channel, trigger_text, k=policy.auto_recall_k,
                )
                if hits:
                    lines = [
                        f"  [{h.id}] ({h.kind}, {h.user_account or 'channel'}, "
                        f"sim={h.similarity:.2f}): {h.content}"
                        for h in hits
                    ]
                    messages.append({
                        "role": "system",
                        "content": (
                            "Possibly relevant facts from your long-term memory "
                            "(use them if they apply, ignore if not):\n"
                            + "\n".join(lines)
                        ),
                    })
                    log.debug("auto_recall in %s: %d hit(s)", channel, len(hits))
            except Exception:
                log.exception("auto_recall failed in %s", channel)

        messages.append({
            "role": "user",
            "content": f"<{actor_nick}> {trigger_text}",
        })
        ctx = ToolContext(
            bot=bot,
            channel=channel,
            actor_nick=actor_nick,
            actor_account=actor_account,
            cfg=self.cfg,
            db=self.db,
            http=http,
            llm_chat=self.chat,
            llm_vision=self.vision,
            memory=self.memory,
        )
        return await self._run_loop(
            messages=messages,
            tools=tools,
            tools_schema=tools_schema,
            ctx=ctx,
            step_cap=policy.step_cap_for("reply"),
            wall_sec=policy.wall_cap_for("reply"),
            purpose="reply",
        )

    async def _run_loop(
        self,
        messages: list[dict],
        tools: list[Tool],
        tools_schema: list[dict] | None,
        ctx: ToolContext,
        step_cap: int,
        wall_sec: int,
        purpose: str = "reply",
    ) -> str | None:
        deadline = time.monotonic() + wall_sec
        tools_by_name = {t.name: t for t in tools}
        last_sig: tuple[str, str] | None = None

        # Per-call HTTP timeout is decoupled from the per-turn wall budget:
        # the wall budget governs whether we *start* another step, but a single
        # in-flight call always gets a generous fixed timeout. Local models
        # routinely take 30-60s for one completion; clamping the SDK timeout
        # to "remaining budget" caused spurious ReadTimeouts.
        call_timeout = float(self.cfg.budgets.llm_call_timeout_sec)
        for step in range(step_cap):
            if time.monotonic() > deadline:
                log.info("agent: wall-clock deadline reached at step %d", step)
                break
            log.debug("agent: step %d/%d (timeout=%.0fs)", step + 1, step_cap, call_timeout)
            try:
                async with _maybe_sem(self._chat_sem):
                    resp = await self.chat.chat.completions.create(
                        model=self.cfg.ai.chat_model,
                        messages=messages,
                        tools=tools_schema,
                        timeout=call_timeout,
                    )
            except (APIError, APIConnectionError, asyncio.TimeoutError) as e:
                log.warning("agent: LLM call failed at step %d: %s", step + 1, e)
                return f"(LLM error: {e})"
            await self._log_usage(resp, self.cfg.ai.chat_model, purpose, ctx.channel)

            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []

            if not tool_calls:
                cleaned, removed = _strip_thinking(msg.content or "")
                if removed:
                    log.debug(
                        "agent: stripped %d chars of <think> reasoning from final reply",
                        removed,
                    )
                return cleaned or None

            # Echo the assistant message back into history before tool results.
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                name = tc.function.name
                args_str = tc.function.arguments or "{}"

                tool = tools_by_name.get(name)
                if tool is None:
                    available = ", ".join(sorted(tools_by_name)) or "(none)"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": prompts.REPAIR_BAD_TOOL_NAME.format(
                            name=name, available=available,
                        ),
                    })
                    continue

                args, err = _validate_args(args_str, tool.schema)
                if err is not None:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": prompts.REPAIR_BAD_ARGS.format(
                            name=name, error=err, schema=json.dumps(tool.schema),
                        ),
                    })
                    continue

                sig = (name, json.dumps(args, sort_keys=True))
                if sig == last_sig:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": prompts.LOOP_DETECTOR_NUDGE.format(name=name),
                    })
                    continue
                last_sig = sig

                try:
                    result = await tool.call(ctx, args)
                except Exception as e:
                    log.exception("tool %s raised", name)
                    result = {"error": f"tool raised: {e!r}"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str)[:8000],
                })

        return await self._summarise_and_bail(messages, ctx.channel)

    async def _summarise_and_bail(self, messages: list[dict], channel: str | None) -> str | None:
        messages.append({"role": "system", "content": prompts.BUDGET_EXHAUSTED_SUMMARY})
        try:
            async with _maybe_sem(self._chat_sem):
                resp = await self.chat.chat.completions.create(
                    model=self.cfg.ai.chat_model,
                    messages=messages,
                    timeout=float(self.cfg.budgets.llm_call_timeout_sec),
                )
        except (APIError, APIConnectionError, asyncio.TimeoutError) as e:
            return f"(out of budget; LLM error during summary: {e})"
        await self._log_usage(resp, self.cfg.ai.chat_model, "summary", channel)
        cleaned, removed = _strip_thinking(resp.choices[0].message.content or "")
        if removed:
            log.debug("agent: stripped %d chars of <think> reasoning from summary", removed)
        return cleaned or None
