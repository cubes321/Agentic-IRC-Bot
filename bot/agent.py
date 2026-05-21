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


# Text-format tool call detection. Some local models emit tool calls as
# plain text using their training-time format (Hermes XML, Mistral
# brackets, Qwen flower markers) instead of via the structured tool_calls
# API. LM Studio's compat adapter doesn't always convert these to the
# expected shape, so they leak through as plain content. We detect them
# and treat as a repair-worthy mistake.
_TEXT_TOOL_CALL_MARKERS = (
    "<tool_call>",        # Hermes / Llama 3.1 / many Qwen variants
    "<function=",         # Hermes function tag, sometimes without <tool_call> wrapper
    "[tool_calls]",       # Mistral
    "✿function✿",         # Qwen2.5 instruct variants
    "✿args✿",
)


def _looks_like_text_tool_call(text: str) -> bool:
    """True if the model's text content appears to contain a tool call
    written as plain text instead of via the structured tool_calls API.
    Conservative: only matches well-known format markers, not casual
    mentions of the word 'tool_call' in prose."""
    if not text:
        return False
    lower = text.lower()
    return any(marker in lower for marker in _TEXT_TOOL_CALL_MARKERS)


# Regex used as a safety net to strip text-format tool calls from the
# FINAL returned text in paths where retry isn't possible (e.g. the
# budget-exhausted summary). Replaces the block with a brief marker so
# the user sees a hint that something was suppressed rather than just
# missing context.
_STRIP_TOOL_CALL_RE = _re.compile(
    r"<tool_call>.*?</tool_call>",
    flags=_re.IGNORECASE | _re.DOTALL,
)


def _strip_text_tool_calls(text: str) -> str:
    """Remove obvious text-format tool call blocks. Safety net only —
    the preferred fix is repair-and-retry inside _run_loop."""
    if not text:
        return text
    cleaned = _STRIP_TOOL_CALL_RE.sub("[malformed tool call removed]", text)
    return cleaned.strip()


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

    async def run_task_turn(
        self,
        bot: Any,
        channel: str,
        policy: ChannelPolicy,
        owner_nick: str,
        owner_account: str | None,
        goal: str,
        http: Any,
        cancel_event: asyncio.Event,
    ) -> str | None:
        """Run the agent loop in task mode: 30-step / 30-min budgets (per
        policy), TASK_SYSTEM prompt, no recent-buffer context (the goal is
        the context), cancellation support via cancel_event.

        Returns the final text to post as the task result. May return the
        sentinel string "__CANCELLED__" if the cancel_event fired mid-run;
        callers (TaskRunner) detect this and write an appropriate
        cancellation message instead of posting the sentinel verbatim."""
        step_cap = policy.step_cap_for("task")
        wall_sec = policy.wall_cap_for("task")

        tools = build_catalog(self.capabilities, set(policy.cfg.allow_actions))
        tools_schema = to_openai_schema(tools) if tools else None

        # Task prompt carries the goal AND the budget — the model can plan
        # accordingly ("I have 30 steps; I should pick efficient tools").
        # No recent buffer: task context is the goal itself; channel chatter
        # would be distracting noise relative to the task at hand.
        messages: list[dict] = [
            {
                "role": "system",
                "content": prompts.TASK_SYSTEM.format(
                    persona=policy.persona,
                    channel=channel,
                    owner_nick=owner_nick,
                    goal=goal,
                    step_cap=step_cap,
                    wall_sec=wall_sec,
                ),
            },
            # A synthetic "user message" tells the model what to do right now,
            # matching the user/assistant alternation Qwen 3-class chat templates
            # require. Without it, some templates refuse to render with system-only.
            {
                "role": "user",
                "content": f"Begin the task. Use the tools available; stop when done or stuck.",
            },
        ]

        ctx = ToolContext(
            bot=bot,
            channel=channel,
            actor_nick=owner_nick,
            actor_account=owner_account,
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
            step_cap=step_cap,
            wall_sec=wall_sec,
            purpose="task",
            cancel_event=cancel_event,
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
        cancel_event: asyncio.Event | None = None,
    ) -> str | None:
        """The agent loop. Honoured by all turn types (reply, task, future).

        cancel_event (new in slice 2c, optional): checked between steps. When
        set, the loop returns a cancellation marker rather than continuing.
        Tasks use this to support !cancel mid-run; reply turns don't set it
        because reply turns are short enough that cancellation has no use case.
        """
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
            # Cancellation gate: checked at the TOP of every step so we never
            # start a new LLM call after a !cancel has been issued. The marker
            # is sentinel text the runner unpacks; cancel handling is the
            # runner's job, not the loop's.
            if cancel_event is not None and cancel_event.is_set():
                log.info("agent: cancel_event observed at step %d, exiting loop", step)
                return "__CANCELLED__"
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
                # Text-format tool call detection: some local models emit
                # tool calls as plain text (Hermes XML / Mistral brackets /
                # Qwen flowers) instead of via the structured tool_calls
                # API. Don't return that to the channel as final content —
                # nudge the model to retry using the actual mechanism.
                # Re-checking inside the loop means we get to use the next
                # step to recover; the safety-net strip below catches the
                # case where we ran out of steps.
                if _looks_like_text_tool_call(cleaned):
                    log.warning(
                        "agent: step %d emitted a text-format tool call instead "
                        "of using the tools API; injecting repair and continuing",
                        step + 1,
                    )
                    # Echo the assistant message into history so the next
                    # turn has full context of what it just did wrong.
                    messages.append({
                        "role": "assistant",
                        "content": msg.content or "",
                    })
                    messages.append({
                        "role": "system",
                        "content": prompts.REPAIR_TEXT_TOOL_CALL,
                    })
                    continue
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
        # Safety net: the budget-exhausted summary can't be retried, so
        # strip any text-format tool calls in place rather than posting them.
        # This rarely fires in practice (most models stop trying to call
        # tools when told "no more tools"), but the cost is one regex pass.
        if _looks_like_text_tool_call(cleaned):
            log.warning(
                "agent: budget-exhausted summary contained a text-format tool "
                "call; stripping (no remaining steps to repair)"
            )
            cleaned = _strip_text_tool_calls(cleaned)
        return cleaned or None
