"""TaskRunner: orchestrates user-issued multi-step background tasks.

A task is a row in the `tasks` table plus an asyncio.Task running the agent
loop in 'task' mode. Lifecycle:

    pending -> running -> done | failed | cancelled

Issuance:
    `<bot>: !task <goal>` (auth gated by ChannelPolicy.can_issue_tasks)
    Persisted immediately so a crash mid-issuance doesn't lose the request.
    Spawned as a coroutine via asyncio.create_task; tracked in
    `self._running` keyed by task id.

Cancellation:
    `!cancel <id>` sets the task's asyncio.Event. The agent loop checks
    this at the top of every step and returns the sentinel "__CANCELLED__"
    when set. The runner unpacks the sentinel and posts a cancellation
    message instead of the sentinel verbatim.

Restart safety:
    On startup, any rows left in 'running' state from the previous run get
    marked 'cancelled' with result "[interrupted by restart]". Resuming a
    partial agent loop is unsafe with local models (no determinism, no
    idempotency guarantees from tools); the safer policy is to surface the
    interruption and let the user re-issue.

Shutdown:
    `shutdown_all()` sets every running task's cancel event and waits a
    brief moment for the agent loops to wind down cleanly. Anything that
    doesn't unwind in the budget gets a hard asyncio cancel.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from openai import AsyncOpenAI

from .agent import AgentCore
from .config import Config
from .db import Database, now_utc_iso
from .policy import ChannelPolicy

log = logging.getLogger(__name__)


# Marker the agent loop returns when cancel_event was observed mid-loop.
# Defined identically in agent.py; duplicated here only as a comparison
# constant so both modules stay readable on their own.
_CANCELLED_SENTINEL = "__CANCELLED__"

# Cap on how many tasks `!tasks` lists per channel. Mostly to keep the IRC
# reply short rather than for any real safety reason.
LIST_LIMIT = 10

# Wall-clock budget for the shutdown task-cleanup phase. Tasks may be
# mid-LLM-call; we set their cancel events and give the agent loops a
# brief window to unwind cleanly before hard-cancelling the asyncio task.
SHUTDOWN_TASK_GRACE_SEC = 10.0


@dataclass
class _RunningTask:
    """In-process state for a task that's currently executing."""
    task_id: int
    channel: str
    owner_nick: str
    owner_account: str | None
    goal: str
    cancel_event: asyncio.Event
    asyncio_task: asyncio.Task
    created_at: float = field(default_factory=lambda: 0.0)


class TaskRunner:
    """Owns the set of currently-running tasks and exposes lifecycle methods.

    Constructed once in main.py. The IRCBot command handlers (`!task`,
    `!cancel`, `!tasks`) delegate here. main.py also calls
    `startup_cleanup()` on boot and `shutdown_all()` during graceful shutdown.
    """

    def __init__(
        self,
        cfg: Config,
        db: Database,
        agent: AgentCore,
        chat_client: AsyncOpenAI,
        http: httpx.AsyncClient,
        ircbot: Any,            # IRCBot — kept untyped to avoid circular import
    ):
        self.cfg = cfg
        self.db = db
        self.agent = agent
        self.chat = chat_client
        self.http = http
        self.bot = ircbot
        # task_id -> _RunningTask. Mutated only from the asyncio loop, so no
        # lock needed for read/write — but be careful never to await between
        # check-and-insert sequences (we don't).
        self._running: dict[int, _RunningTask] = {}

    # ---- startup / shutdown hooks ----

    async def startup_cleanup(self) -> int:
        """Mark any 'running' tasks from the previous bot lifetime as
        'cancelled'. Returns the number cleaned up. Called from main()
        after DB open, before IRC connect — so the model can never observe
        a stale 'running' task as the live state."""
        rows = await self.db.fetchall(
            "SELECT id, channel FROM tasks WHERE status = 'running'",
        )
        if not rows:
            return 0
        for r in rows:
            await self.db.execute(
                "UPDATE tasks SET status = 'cancelled', result = ? WHERE id = ?",
                ("[interrupted by restart]", r["id"]),
            )
        log.info(
            "Task restart cleanup: marked %d stale running task(s) as cancelled",
            len(rows),
        )
        return len(rows)

    async def shutdown_all(self) -> int:
        """Cancel every running task during graceful shutdown. Sets each
        task's cancel_event, waits up to SHUTDOWN_TASK_GRACE_SEC for the
        agent loops to wind down, then hard-cancels anything still alive.

        Each task's _run_task() coroutine handles the cancellation by
        updating the DB row and posting a brief "[task #N] interrupted by
        shutdown" line to the channel, so users see what happened."""
        if not self._running:
            return 0
        count = len(self._running)
        log.info(
            "Task shutdown: cancelling %d running task(s) (grace %.0fs)",
            count, SHUTDOWN_TASK_GRACE_SEC,
        )
        # Snapshot tasks BEFORE setting events — once events fire, _run_task
        # may complete and remove itself from self._running mid-iteration.
        snapshot = list(self._running.values())
        for rt in snapshot:
            rt.cancel_event.set()
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(rt.asyncio_task for rt in snapshot),
                    return_exceptions=True,
                ),
                timeout=SHUTDOWN_TASK_GRACE_SEC,
            )
        except asyncio.TimeoutError:
            still = [rt for rt in snapshot if not rt.asyncio_task.done()]
            log.warning(
                "Task shutdown: %d task(s) did not wind down in time, hard-cancelling: %s",
                len(still), ", ".join(f"#{rt.task_id}" for rt in still),
            )
            for rt in still:
                rt.asyncio_task.cancel()
            await asyncio.gather(
                *(rt.asyncio_task for rt in still), return_exceptions=True,
            )
        return count

    # ---- public surface (called from IRCBot command handlers) ----

    async def issue_task(
        self,
        channel: str,
        owner_nick: str,
        owner_account: str | None,
        goal: str,
    ) -> int:
        """Persist a new task and spawn its runner coroutine.

        Returns the new task id. Caller (IRCBot._cmd_task) posts the
        '[task #N] starting' ack to the channel using the returned id.
        Auth check (ChannelPolicy.can_issue_tasks) happens in the caller
        BEFORE this method — TaskRunner trusts what reaches it.
        """
        cap = self.cfg.budgets.task_step_cap
        wall_sec = self.cfg.budgets.task_wall_sec
        deadline = (
            datetime.now(timezone.utc).timestamp() + wall_sec
        )
        deadline_iso = datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat()

        task_id = await self.db.insert_returning_id(
            "INSERT INTO tasks "
            "(channel, owner_account, owner_nick, goal, status, step_log, "
            " result, created_at, deadline, step_cap) "
            "VALUES (?, ?, ?, ?, 'pending', '[]', NULL, ?, ?, ?)",
            (channel, owner_account, owner_nick, goal,
             now_utc_iso(), deadline_iso, cap),
        )

        cancel_event = asyncio.Event()
        coro = self._run_task(
            task_id=task_id,
            channel=channel,
            owner_nick=owner_nick,
            owner_account=owner_account,
            goal=goal,
            cancel_event=cancel_event,
        )
        asyncio_task = asyncio.create_task(coro, name=f"task.{task_id}")
        self._running[task_id] = _RunningTask(
            task_id=task_id,
            channel=channel,
            owner_nick=owner_nick,
            owner_account=owner_account,
            goal=goal,
            cancel_event=cancel_event,
            asyncio_task=asyncio_task,
        )
        # Auto-clean from _running on completion (any path).
        asyncio_task.add_done_callback(
            lambda _t, tid=task_id: self._running.pop(tid, None)
        )
        log.info(
            "Task #%d issued in %s by %s: %r",
            task_id, channel, owner_nick, goal[:120],
        )
        return task_id

    async def cancel_task(
        self,
        task_id: int,
        cancelled_by_nick: str,
        cancelled_by_account: str | None,
        is_operator: bool,
    ) -> str:
        """Try to cancel a task by id. Returns a user-facing message.

        Authorization: the issuer of the task, OR an operator, may cancel.
        Anyone else gets a polite refusal. This authorization runs HERE,
        not in the caller — it depends on knowing the task's owner_account
        which we have to look up.
        """
        # If currently running, we have its owner in memory.
        rt = self._running.get(task_id)
        if rt is not None:
            owner_account = rt.owner_account
            owner_nick = rt.owner_nick
        else:
            # Not currently running — check DB. Might have already finished
            # naturally, OR might be in 'pending' state (unlikely; we never
            # leave pending). Either way, treat as "no live task to cancel."
            row = await self.db.fetchone(
                "SELECT owner_account, owner_nick, status FROM tasks WHERE id = ?",
                (task_id,),
            )
            if row is None:
                return f"(no task #{task_id})"
            if row["status"] in ("done", "failed", "cancelled"):
                return f"(task #{task_id} already {row['status']})"
            owner_account = row["owner_account"]
            owner_nick = row["owner_nick"]

        # Authorisation check. Operator beats everything. Otherwise: only
        # the task's original owner (by account, fallback to nick) can cancel.
        # Account-based comparison is correct because nicks can change.
        is_owner = False
        if owner_account and cancelled_by_account:
            is_owner = owner_account == cancelled_by_account
        elif owner_account is None and cancelled_by_account is None:
            # Neither side has an account; fall back to nick comparison.
            # Imperfect (nick spoofing) but the alternative is "nobody can
            # cancel" which is worse.
            is_owner = owner_nick.lower() == cancelled_by_nick.lower()

        if not (is_owner or is_operator):
            return (
                f"(refusing !cancel: task #{task_id} was issued by "
                f"{owner_nick}; only the issuer or an operator can cancel)"
            )

        if rt is None:
            # Task already finished between our DB lookup and now (race).
            # Re-check the status; treat as "nothing to do."
            return f"(task #{task_id} not currently running)"

        # Set the event; the agent loop picks it up at its next step and
        # returns the cancellation sentinel. _run_task posts the result.
        rt.cancel_event.set()
        log.info(
            "Task #%d cancellation requested by %s (operator=%s)",
            task_id, cancelled_by_nick, is_operator,
        )
        return f"[task #{task_id}] cancelling…"

    async def list_tasks(self, channel: str) -> list[dict]:
        """Return recent tasks for a channel (most recent first). Includes
        all statuses so users can see history, not just live ones."""
        rows = await self.db.fetchall(
            "SELECT id, status, goal, created_at, owner_nick "
            "FROM tasks WHERE channel = ? "
            "ORDER BY id DESC LIMIT ?",
            (channel, LIST_LIMIT),
        )
        return [dict(r) for r in rows]

    # ---- internal: the per-task runner coroutine ----

    async def _run_task(
        self,
        task_id: int,
        channel: str,
        owner_nick: str,
        owner_account: str | None,
        goal: str,
        cancel_event: asyncio.Event,
    ) -> None:
        """The main coroutine for one task. Runs the agent loop, handles
        cancellation, persists the outcome, posts the result."""
        # Transition pending -> running. Done as a single UPDATE rather than
        # in two steps so an external observer (memory_stats, future UI)
        # never sees the task in 'pending' for more than a few ms.
        await self.db.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,),
        )

        # Build the policy snapshot. Channel may have been removed from the
        # config between issuance and now (unlikely; reload would have
        # required a restart anyway), so fall back to the default policy
        # if the section is gone.
        ch_cfg = self.cfg.channel(channel)
        policy = ChannelPolicy(channel, ch_cfg, self.cfg)

        # Run the agent loop. Returns the final text or "__CANCELLED__".
        result_text: str | None = None
        outcome_status = "done"
        try:
            result_text = await self.agent.run_task_turn(
                bot=self.bot,
                channel=channel,
                policy=policy,
                owner_nick=owner_nick,
                owner_account=owner_account,
                goal=goal,
                http=self.http,
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            # Hard cancel (from shutdown_all's fallback). Mark accordingly
            # and re-raise so the asyncio loop notices.
            outcome_status = "cancelled"
            result_text = "[interrupted by shutdown]"
            await self._persist_outcome(task_id, outcome_status, result_text)
            await self._post_result(channel, task_id, outcome_status, result_text)
            raise
        except Exception as e:
            log.exception("task #%d crashed", task_id)
            outcome_status = "failed"
            result_text = f"(internal error: {e!r})"

        # Cancellation via the sentinel: the agent loop saw cancel_event.
        if result_text == _CANCELLED_SENTINEL:
            outcome_status = "cancelled"
            result_text = "[cancelled]"

        # Empty / None result means the loop ran but the model produced no
        # text. Treat as "done" with a placeholder — at least the user knows
        # the task finished.
        if not result_text:
            result_text = "(no result produced)"

        await self._persist_outcome(task_id, outcome_status, result_text)
        await self._post_result(channel, task_id, outcome_status, result_text)

    async def _persist_outcome(
        self,
        task_id: int,
        status: str,
        result_text: str,
    ) -> None:
        """Update the task row with its final state. Persistence is
        always-on; even a hard-cancelled task gets its outcome recorded."""
        try:
            await self.db.execute(
                "UPDATE tasks SET status = ?, result = ? WHERE id = ?",
                (status, result_text, task_id),
            )
        except Exception:
            log.exception("task #%d: failed to persist outcome", task_id)

    async def _post_result(
        self,
        channel: str,
        task_id: int,
        status: str,
        result_text: str,
    ) -> None:
        """Post the task's outcome to the channel with a [task #N] prefix.
        Each logical line of multi-line results gets the same prefix so IRC
        clients render them as a coherent block.

        Long logical lines (>400 chars; common for markdown bullets in task
        results) are word-wrapped by `bot.irc_send`; each wrap-continuation
        also carries the `[task #N]` prefix so every wire line stays
        identifiable as part of the same task block. `max_lines=20` per
        irc_send call gives generous headroom for one logical line to
        wrap into many sub-lines without truncation.

        Failure modes (network drop, channel parted) are logged but not
        retried — the result is in the DB; the channel post is best-effort.
        """
        try:
            # For done: just '[task #N] <text>'. For cancelled/failed: leading
            # marker word so the channel can tell the outcome at a glance.
            if status == "done":
                prefix = f"[task #{task_id}] "
            else:
                prefix = f"[task #{task_id}] {status}: "
            # Continuation prefix: visible task ID plus 3-space indent.
            # Used both for new logical lines (between-line continuations)
            # AND for wrap continuations of a single long logical line —
            # one consistent shape so readers don't have to distinguish
            # 'new bullet' from 'wrapped continuation' at the prefix layer.
            cont_prefix = f"[task #{task_id}]   "

            lines = [ln for ln in result_text.splitlines() if ln.strip()]
            if not lines:
                lines = [result_text or "(no result)"]

            # First logical line: prefix is the status-aware variant (e.g.
            # `[task #5] cancelled: ` for cancelled tasks); wrap
            # continuations of this line use the lighter `[task #5]   ` form.
            await self.bot.irc_send(
                channel, prefix + lines[0],
                continuation_prefix=cont_prefix,
                max_lines=20,
            )
            # Subsequent logical lines: use the lighter prefix for the
            # first chunk too — they are by definition continuations of
            # the task-result block.
            for ln in lines[1:]:
                await self.bot.irc_send(
                    channel, cont_prefix + ln,
                    continuation_prefix=cont_prefix,
                    max_lines=20,
                )
        except Exception:
            log.exception(
                "task #%d: failed to post result to %s (result stored in DB)",
                task_id, channel,
            )
