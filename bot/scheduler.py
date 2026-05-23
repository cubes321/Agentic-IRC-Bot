"""Scheduler: long-lived background coroutines.

Owns:
  - One coroutine per `chatty` channel for initiative ticks
  - One global coroutine that polls the reminders table and fires due ones

Lifecycle: explicit `start()` / `stop()`. Lives for the bot's entire run, not
per-request. Cancellation on stop() is cooperative — each loop catches
asyncio.CancelledError, releases resources, and exits.

Concurrency: shares the chat semaphore with AgentCore + MemoryStore so all
chat completions contend for the same slot pool.

What ticks do:
  Build a small context (recent buffer + recalled memories), ask the LLM
  "should you say something? <silent> if no", post if non-silent. NO TOOLS —
  ticks are one-shot generations to keep cost predictable.

What ticks deliberately don't do:
  Run the full agent loop with tools. That's reply-turn territory; if the
  bot wants to look something up before chiming in, it can do so when actually
  addressed. Otherwise an idle channel can burn through tokens silently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from openai import AsyncOpenAI, APIError, APIConnectionError

from . import prompts
from .agent import _strip_thinking  # reuse the same defense
from .config import Config
from .db import Database, now_utc_iso
from .policy import ChannelPolicy

log = logging.getLogger(__name__)


# Sentinel the LLM emits to indicate "I have nothing to say" on a tick.
SILENT_TOKEN = "<silent>"


def _is_silent_signal(text: str) -> bool:
    """True if the model's tick output should be treated as silence.

    Tolerates:
      - case variants:        <SILENT>, <Silent>
      - HTML-escaped:         &lt;silent&gt;
      - bare word + brackets: silent, [silent], (silent), <silent/>, "silent."
      - empty / whitespace-only output

    Deliberately permissive: if the model's whole reply is "silent" in any
    plausible form, we'd rather treat as silence than post the literal tag
    to the channel. The cost of a false positive (one missed chime-in) is
    much smaller than the cost of a false negative (posting "<silent>" to
    a public channel)."""
    if not text:
        return True
    lower = text.lower().strip()
    if not lower:
        return True
    if "<silent>" in lower:
        return True
    if "&lt;silent&gt;" in lower:
        return True
    # If the entire stripped reply is some form of "silent" (with or without
    # angle brackets / surrounding punctuation), treat as silence.
    bare = lower.strip("[](){}<>:.,!?'\"/\\ \t\r\n")
    if bare == "silent":
        return True
    return False


class _NullCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False


def _maybe_sem(sem: asyncio.Semaphore | None):
    return sem if sem is not None else _NullCtx()


def _format_recent(rows: list) -> str:
    if not rows:
        return "(no recent messages)"
    return "\n".join(f"<{r['nick']}> {r['content']}" for r in rows)


def _format_memories(hits: list) -> str:
    if not hits:
        return "(no relevant memories)"
    return "\n".join(
        f"  - ({h.kind}, {h.user_account or 'channel'}): {h.content}"
        for h in hits
    )


class Scheduler:
    """Lifecycle: start() spawns tasks; stop() cancels them and awaits exit."""

    def __init__(
        self,
        cfg: Config,
        db: Database,
        chat_client: AsyncOpenAI,
        chat_model: str,
        chat_semaphore: asyncio.Semaphore | None,
        memory: Any,                # MemoryStore | None
        ircbot: Any,                # IRCBot — used for posting and reading channel state
    ):
        self.cfg = cfg
        self.db = db
        self.chat_client = chat_client
        self.chat_model = chat_model
        self.chat_sem = chat_semaphore
        self.memory = memory
        self.bot = ircbot
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    # ---- lifecycle ----

    async def start(self) -> None:
        """Spawn all background coroutines. Idempotent — calling twice is a no-op."""
        if self._tasks:
            return
        # Reminder firing — one global coroutine.
        self._tasks.append(asyncio.create_task(
            self._reminder_loop(), name="scheduler.reminders"
        ))
        # One initiative tick coroutine per chatty channel with a positive
        # tick_interval_sec. Built from the configured channel set.
        scheduled = 0
        for channel_name, ch_cfg in self.cfg.channels.items():
            if ch_cfg.mode != "chatty":
                continue
            if ch_cfg.tick_interval_sec <= 0:
                continue
            self._tasks.append(asyncio.create_task(
                self._tick_loop(channel_name), name=f"scheduler.tick.{channel_name}"
            ))
            scheduled += 1
        log.info(
            "Scheduler started: 1 reminder loop + %d initiative tick(s)",
            scheduled,
        )

    async def stop(self) -> None:
        """Cancel all loops and wait for them to exit. Safe to call any time."""
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        log.info("Scheduler stopped")

    # ---- shutdown-time channel goodbyes ----

    async def say_goodbyes(
        self,
        parting_message: str | None,
        timeout_sec: float,
    ) -> None:
        """Post a short in-character farewell in each channel with recent chat.

        Called from main()'s shutdown sequence AFTER stop() (so initiative
        ticks can't fire concurrently) and BEFORE client.quit() (so the
        connection is still open). Per-channel goodbye LLM calls run
        concurrently, capped by the shared chat_semaphore. The whole batch
        shares a single wall-clock budget — any channels that don't finish
        get cancelled.

        Eligibility: the channel must be in cfg.channels, the bot must be
        joined to it, mode must not be 'locked', and there must be a message
        in message_log newer than cfg.shutdown.goodbye_recent_sec.

        Per-channel failures (LLM error, send failure, model chose silence)
        are logged but never block other channels' goodbyes.
        """
        threshold = self.cfg.shutdown.goodbye_recent_sec
        if threshold <= 0:
            log.debug("Goodbye broadcast disabled (goodbye_recent_sec=0)")
            return

        # Cutoff in ISO form matches what recent_messages() expects.
        cutoff = datetime.now(timezone.utc).timestamp() - threshold
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()

        # Build the eligible-channel list by checking freshness per channel.
        # We do these sequentially because they're cheap DB calls; the LLM
        # work later is what runs concurrently.
        eligible: list[str] = []
        for channel_name, ch_cfg in self.cfg.channels.items():
            if ch_cfg.mode == "locked":
                continue
            try:
                in_chan = (
                    self.bot.in_channel(channel_name)
                    if hasattr(self.bot, "in_channel")
                    else True
                )
            except Exception:
                in_chan = True
            if not in_chan:
                continue
            try:
                recent = await self.db.recent_messages(
                    channel_name, n=5, since_iso=cutoff_iso,
                )
            except Exception:
                log.exception("goodbye: freshness check failed for %s", channel_name)
                continue
            if recent:
                eligible.append(channel_name)

        if not eligible:
            log.info("Goodbye: no channels with chat in the last %ds", threshold)
            return

        # NB: "considering" not "posting" — each channel's outcome is still
        # one of (posted | silent | LLM error | send error). The per-channel
        # log lines below show the actual outcome at INFO level, so the
        # caller can tell which channels actually got a goodbye line.
        log.info(
            "Goodbye: considering farewell in %d channel(s): %s (budget %.0fs)",
            len(eligible), ", ".join(eligible), timeout_sec,
        )

        tasks = [
            asyncio.create_task(
                self._say_goodbye_in(ch, parting_message),
                name=f"goodbye.{ch}",
            )
            for ch in eligible
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=max(1.0, timeout_sec),
            )
        except asyncio.TimeoutError:
            still = [t.get_name() for t in tasks if not t.done()]
            log.warning(
                "Goodbye batch timed out after %.0fs; cancelling: %s",
                timeout_sec, ", ".join(still),
            )
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Give cancelled tasks a moment to unwind cleanly.
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _say_goodbye_in(
        self,
        channel: str,
        parting_message: str | None,
    ) -> None:
        """One channel's goodbye flow: build prompt, call LLM, post if non-silent."""
        ch_cfg = self.cfg.channels.get(channel)
        if ch_cfg is None:
            return
        policy = ChannelPolicy(channel, ch_cfg, self.cfg)

        # Pull recent buffer fresh — same shape as the tick prompt builder
        # so the model sees familiar context, just with a different system
        # instruction.
        try:
            recent = await self.db.recent_messages(channel, n=15)
        except Exception:
            log.exception("goodbye: recent_messages failed for %s", channel)
            return
        if not recent:
            log.debug("goodbye: %s has no recent messages, skipping", channel)
            return

        # Optional parting message from `!quit <msg>` — passed to the prompt
        # so the model can weave context in if natural ("heading out for
        # dinner" -> "catch you all after dinner"). Empty/None just omits.
        reason_clause = (
            f' with parting message: "{parting_message}"'
            if parting_message else ""
        )

        prompt_text = prompts.GOODBYE_SYSTEM.format(
            persona=policy.persona,
            channel=channel,
            nick=self.cfg.server.nick,
            reason_clause=reason_clause,
            recent=_format_recent(recent),
        )
        messages = [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": "Say goodbye now."},
        ]

        log.debug("goodbye: firing LLM call for %s", channel)
        try:
            async with _maybe_sem(self.chat_sem):
                resp = await self.chat_client.chat.completions.create(
                    model=self.chat_model,
                    messages=messages,
                    temperature=0.6,
                    timeout=float(self.cfg.budgets.llm_call_timeout_sec),
                )
        except (APIError, APIConnectionError, asyncio.TimeoutError, asyncio.CancelledError) as e:
            log.warning("goodbye: LLM call failed for %s: %s", channel, e)
            return

        # Token usage attribution — distinct purpose so the on-exit summary
        # can show what graceful shutdown actually cost.
        try:
            usage = getattr(resp, "usage", None)
            if usage is not None:
                await self.db.log_usage(
                    model=self.chat_model,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    purpose="goodbye",
                    channel=channel,
                )
        except Exception:
            log.exception("goodbye: usage logging failed")

        raw = (resp.choices[0].message.content or "")
        text, removed = _strip_thinking(raw)
        if removed:
            log.debug("goodbye: stripped %d chars of <think> reasoning in %s", removed, channel)

        if _is_silent_signal(text):
            # Bumped from DEBUG to INFO: the parent "considering farewell in
            # N channel(s)" line is INFO, so its outcomes should be visible
            # at the same level. Otherwise the operator sees "I'll try 2
            # channels" with no follow-up at default log level and can't
            # tell silence from a bug.
            log.info("goodbye: %s -> silent (raw=%r)", channel, text[:80])
            return

        log.info("goodbye: %s posting %r", channel, text[:120])
        try:
            await self.bot.irc_send(channel, text)
        except Exception:
            log.exception("goodbye: failed to post to %s", channel)

    # ---- reminder firing ----

    # Per-channel cap on how many reminders may fire in a single poll cycle.
    # Excess get dropped (with a one-line notice posted to the channel) to
    # prevent the case where many reminders set across a long horizon all
    # come due in the same 10-second window and flood. Drop-with-notice
    # rather than delay-to-next-cycle: a 30-reminder backlog deferred 5 per
    # cycle is just a 1-minute sustained flood. (Security review H3.)
    MAX_REMINDERS_PER_CHANNEL_PER_CYCLE = 5
    # Retention: delete reminders that should have fired more than this many
    # days ago. Catches orphans (channels we've parted) and rows that
    # repeatedly failed to fire. Bounded table size as a side effect.
    REMINDER_RETENTION_DAYS = 7
    # Periodic prune interval; runs from inside the reminder poll loop so
    # we don't spawn a second long-lived coroutine for one DELETE per hour.
    REMINDER_PRUNE_INTERVAL_SEC = 3600.0

    async def _reminder_loop(self) -> None:
        """Every reminder_poll_sec, look up due reminders and post them. Uses
        the same chat semaphore for any LLM-driven framing — but for v1, we
        post the reminder text verbatim, no LLM involvement, so the loop is
        fast and cheap.

        Two H3 mitigations live inline here:
          1. Per-channel fire-time cap (MAX_REMINDERS_PER_CHANNEL_PER_CYCLE):
             groups due rows by channel; if more than N for one channel,
             fires the first N and drops the rest with a single warning
             line. Bounds the worst-case flood per channel per cycle.
          2. Retention prune: every REMINDER_PRUNE_INTERVAL_SEC, deletes
             rows whose fire_at is older than REMINDER_RETENTION_DAYS.
             Catches orphans and failed-fire rows that the per-row delete
             missed."""
        poll_sec = max(2, int(self.cfg.scheduler.reminder_poll_sec))
        log.info("Reminder loop: polling every %ds", poll_sec)
        last_prune = 0.0  # monotonic; 0 means "prune on first cycle"
        try:
            while not self._stopping.is_set():
                try:
                    rows = await self.db.due_reminders()
                except Exception:
                    log.exception("reminder poll failed")
                    rows = []

                # Group due rows by channel so the per-channel cap applies.
                by_channel: dict[str, list[Any]] = {}
                for row in rows:
                    by_channel.setdefault(row["channel"], []).append(row)
                for channel, ch_rows in by_channel.items():
                    await self._fire_channel_batch(channel, ch_rows)

                # Periodic housekeeping. Three independent maintenance
                # tasks share the same hourly trigger:
                #   - prune old reminders (orphans + failed-fire cleanup, H3)
                #   - prune old message_log rows (retention from
                #     [storage].message_log_retention_days, L2)
                #   - refresh per-channel op state (defensive against
                #     missed mode events around netsplits, M5)
                # All three are cheap enough that one hourly cycle
                # handles them with no dedicated coroutines.
                if time.monotonic() - last_prune > self.REMINDER_PRUNE_INTERVAL_SEC:
                    await self._prune_old_reminders()
                    await self._prune_old_messages()
                    await self._refresh_channel_op_state()
                    last_prune = time.monotonic()

                # Sleep but wake early if stop is signalled.
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=poll_sec)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            log.debug("reminder loop cancelled")
            raise

    async def _fire_channel_batch(self, channel: str, rows: list[Any]) -> None:
        """Fire up to MAX_REMINDERS_PER_CHANNEL_PER_CYCLE reminders for one
        channel in this cycle; drop the excess with a notice."""
        cap = self.MAX_REMINDERS_PER_CHANNEL_PER_CYCLE
        if len(rows) <= cap:
            for row in rows:
                await self._fire_reminder(row)
            return
        # Over cap: fire first N, drop the rest.
        for row in rows[:cap]:
            await self._fire_reminder(row)
        excess = rows[cap:]
        log.warning(
            "reminder fire-cap hit for %s: %d fired, %d dropped",
            channel, cap, len(excess),
        )
        try:
            await self.bot.irc_send(
                channel,
                f"(reminder flood control: {len(excess)} additional "
                f"reminder(s) dropped to avoid spamming the channel)",
            )
        except Exception:
            log.exception("failed to post fire-cap notice to %s", channel)
        # Delete the dropped rows so they don't re-trigger next cycle.
        for row in excess:
            try:
                await self.db.delete_reminder(row["id"])
            except Exception:
                log.exception(
                    "fire-cap excess delete failed for reminder #%d", row["id"],
                )

    async def _prune_old_reminders(self) -> None:
        """Delete reminders whose fire_at is older than REMINDER_RETENTION_DAYS.
        Catches orphans (parted channels), reminders that repeatedly failed
        to post (the per-row delete in _fire_reminder only runs on success),
        and ancient rows from before retention was implemented."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=self.REMINDER_RETENTION_DAYS,
        )
        try:
            # Direct sqlite execution so we can read cur.rowcount; the
            # db.execute wrapper doesn't expose it.
            async with self.db.conn.execute(
                "DELETE FROM reminders WHERE fire_at < ?",
                (cutoff.isoformat(),),
            ) as cur:
                deleted = cur.rowcount
            await self.db.conn.commit()
            if deleted > 0:
                log.info(
                    "Pruned %d expired reminder(s) older than %d days",
                    deleted, self.REMINDER_RETENTION_DAYS,
                )
        except Exception:
            log.exception("reminder retention prune failed")

    async def _refresh_channel_op_state(self) -> None:
        """Send NAMES for each joined channel and re-sync the op set.
        Defensive backup against pydle missing a MODE event during
        netsplits, reconnects, or other edge events that would otherwise
        leave `auth.is_op_in_channel()` returning stale data. Runs on
        the same hourly cadence as the retention prunes.

        Two-phase: trigger NAMES for every channel, sleep briefly to
        let pydle process replies, then re-read pydle's parsed channel
        state into our auth manager. Self-healing within one cycle if
        pydle's view was stale. (Security review M5, 2026-05-22.)"""
        bot_channels = getattr(self.bot, "channels", None) or {}
        channels = list(bot_channels)
        if not channels:
            return
        log.debug(
            "op-state refresh: requesting NAMES for %d channel(s)", len(channels),
        )
        for ch in channels:
            try:
                await self.bot.refresh_channel_state(ch)
            except Exception:
                log.debug("op-state refresh: NAMES failed for %s", ch)
        # Give pydle a beat to process the NAMES replies into
        # self.channels[*]['modes'] before we re-read them. 2s is
        # generous on a healthy connection (typical reply ~50ms) and
        # tolerable on a slow one.
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        if self._stopping.is_set():
            return
        for ch in channels:
            try:
                self.bot.resync_channel_ops(ch)
            except Exception:
                log.exception("op-state resync failed for %s", ch)
        log.debug("op-state refresh complete (%d channels)", len(channels))

    async def _prune_old_messages(self) -> None:
        """Delete message_log rows older than
        cfg.storage.message_log_retention_days. Channel transcripts grow
        without bound otherwise — both a privacy (IRC users don't expect
        durable transcripts) and a disk-usage concern. (Security review
        L2, 2026-05-22.)

        retention_days = 0 disables the prune entirely (caller's choice;
        the table just grows). Negative values are coerced to 0 here so
        a misconfiguration can't accidentally delete recent messages."""
        retention_days = max(0, int(self.cfg.storage.message_log_retention_days))
        if retention_days == 0:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        try:
            async with self.db.conn.execute(
                "DELETE FROM message_log WHERE ts < ?",
                (cutoff.isoformat(),),
            ) as cur:
                deleted = cur.rowcount
            await self.db.conn.commit()
            if deleted > 0:
                log.info(
                    "Pruned %d message_log row(s) older than %d days",
                    deleted, retention_days,
                )
        except Exception:
            log.exception("message_log retention prune failed")

    async def _fire_reminder(self, row: Any) -> None:
        """Post a single reminder and delete it. On failure, leave the row
        in place so the next poll retries."""
        rid = row["id"]
        channel = row["channel"]
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except json.JSONDecodeError:
            log.warning("reminder #%d has invalid payload, deleting", rid)
            await self.db.delete_reminder(rid)
            return

        target = payload.get("target_nick") or ""
        message = (payload.get("message") or "").strip()
        if not message:
            log.warning("reminder #%d has no message, deleting", rid)
            await self.db.delete_reminder(rid)
            return

        text = f"reminder for {target}: {message}" if target else f"reminder: {message}"
        try:
            await self.bot.irc_send(channel, text)
        except Exception:
            log.exception("failed to post reminder #%d to %s; will retry", rid, channel)
            return

        try:
            await self.db.delete_reminder(rid)
        except Exception:
            log.exception("posted reminder #%d but failed to delete row", rid)

    # ---- initiative ticks ----

    async def _tick_loop(self, channel: str) -> None:
        """Per-channel: sleep, decide to speak, repeat."""
        ch_cfg = self.cfg.channels.get(channel)
        if ch_cfg is None:
            log.warning("tick_loop: no config for %s, exiting", channel)
            return

        base = max(self.cfg.scheduler.min_tick_interval_sec, ch_cfg.tick_interval_sec)
        if base != ch_cfg.tick_interval_sec:
            log.warning(
                "tick_interval_sec for %s clamped from %d to %d (min_tick_interval_sec)",
                channel, ch_cfg.tick_interval_sec, base,
            )
        jitter_pct = max(0.0, min(0.95, self.cfg.scheduler.tick_jitter_pct))

        log.info(
            "Initiative tick for %s: every ~%ds (jitter ±%.0f%%)",
            channel, base, jitter_pct * 100,
        )

        try:
            # Stagger initial firings so all channels don't tick the same second.
            await self._sleep_with_jitter(base, jitter_pct, initial=True)
            while not self._stopping.is_set():
                try:
                    await self._do_tick(channel)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("tick failed for %s; continuing", channel)
                await self._sleep_with_jitter(base, jitter_pct, initial=False)
        except asyncio.CancelledError:
            log.debug("tick loop for %s cancelled", channel)
            raise

    async def _sleep_with_jitter(self, base: int, jitter_pct: float, initial: bool) -> None:
        if initial:
            # Random offset in [0, base) so channels don't sync up.
            sleep_for = random.uniform(0.5 * base, base)
        else:
            sleep_for = base * (1.0 + random.uniform(-jitter_pct, jitter_pct))
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=max(1.0, sleep_for))
        except asyncio.TimeoutError:
            pass

    async def _do_tick(self, channel: str) -> None:
        """Build a tick prompt, ask the LLM, post if not <silent>."""
        ch_cfg = self.cfg.channels.get(channel)
        if ch_cfg is None:
            return
        policy = ChannelPolicy(channel, ch_cfg, self.cfg)

        # Skip if the bot isn't actually in the channel right now (could have
        # been kicked, or join failed at startup).
        try:
            in_chan = self.bot.in_channel(channel) if hasattr(self.bot, "in_channel") else True
        except Exception:
            in_chan = True
        if not in_chan:
            log.debug("tick: skipping %s (not in channel)", channel)
            return

        # Recent buffer for context. We restrict the buffer to messages newer
        # than `tick_skip_if_idle_sec` so the model doesn't receive yesterday's
        # transcript mixed with one fresh "morning" and treat the whole thing
        # as live conversation.
        idle_threshold = self.cfg.scheduler.tick_skip_if_idle_sec
        since_iso: str | None = None
        if idle_threshold > 0:
            cutoff = datetime.now(timezone.utc).timestamp() - idle_threshold
            since_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        recent = await self.db.recent_messages(channel, n=20, since_iso=since_iso)
        if not recent:
            log.debug(
                "tick: %s has no messages within the freshness window (%ds), skipping",
                channel, idle_threshold,
            )
            return
        # If the post-filter buffer is too thin, the channel isn't really
        # "active" — just had a single fresh ping. Better to stay quiet than
        # to invent a conversation around one message.
        MIN_FRESH_MESSAGES = 3
        if len(recent) < MIN_FRESH_MESSAGES:
            log.debug(
                "tick: %s only %d fresh message(s), below threshold %d, skipping",
                channel, len(recent), MIN_FRESH_MESSAGES,
            )
            return

        # Tighter freshness check: even within the buffer window, if the newest
        # message is too old, the conversation has wrapped up. The buffer
        # contains a cluster of messages from earlier — that's history, not a
        # live conversation we can chime into. recent[-1] is newest because
        # recent_messages() returns oldest-first.
        last_age_threshold = self.cfg.scheduler.tick_skip_if_last_message_older_than_sec
        if last_age_threshold > 0 and recent:
            try:
                last_ts = datetime.fromisoformat(recent[-1]["ts"])
            except (ValueError, TypeError, KeyError):
                last_ts = None
            if last_ts is not None:
                # Make both sides timezone-aware to avoid naive/aware mixing
                # if a row was somehow stored without tz info.
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - last_ts).total_seconds()
                if age > last_age_threshold:
                    log.debug(
                        "tick: %s newest message is %.0fs old (> %ds), skipping",
                        channel, age, last_age_threshold,
                    )
                    return

        # Auto-pull a few relevant memories using the latest message as the
        # query. Cheap (one embedding call) and gives the bot something
        # concrete to surface if it chooses to speak.
        memories: list = []
        if self.memory is not None:
            try:
                latest = recent[-1]["content"]
                memories = await self.memory.recall(channel, latest, k=3)
            except Exception:
                log.exception("tick: memory recall failed for %s", channel)
                memories = []

        prompt_text = prompts.INITIATIVE_SYSTEM.format(
            persona=policy.persona,
            channel=channel,
            nick=self.cfg.server.nick,
            recent=_format_recent(recent),
            threads="(none)",         # open-thread queue lands in slice 2c with tasks
            memories=_format_memories(memories),
        )
        # Some local model chat templates (Qwen 2.5+, others) require at least
        # one user-role message to render. A system-only payload triggers
        # "No user query found in messages." from LM Studio. We include a
        # minimal synthetic user turn that asks the model to act on the
        # system prompt — preserving the design (system carries the context,
        # user carries the trigger) while being template-portable.
        messages = [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": "Decide now whether to chime in. Output the message, or <silent>."},
        ]

        log.debug("tick: firing for %s", channel)
        try:
            async with _maybe_sem(self.chat_sem):
                resp = await self.chat_client.chat.completions.create(
                    model=self.chat_model,
                    messages=messages,
                    temperature=0.7,    # a little randomness is fine for ticks
                    timeout=float(self.cfg.budgets.llm_call_timeout_sec),
                )
        except (APIError, APIConnectionError, asyncio.TimeoutError) as e:
            log.warning("tick: LLM call failed for %s: %s", channel, e)
            return

        # Token-usage logging for ticks (uses the same DB.log_usage path as
        # AgentCore so the on-exit summary attributes them correctly).
        try:
            usage = getattr(resp, "usage", None)
            if usage is not None:
                await self.db.log_usage(
                    model=self.chat_model,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    purpose="tick",
                    channel=channel,
                )
        except Exception:
            log.exception("tick: usage logging failed")

        raw = (resp.choices[0].message.content or "")
        text, removed = _strip_thinking(raw)
        if removed:
            log.debug("tick: stripped %d chars of <think> reasoning in %s", removed, channel)

        if _is_silent_signal(text):
            # Robust silence detection — handles case variants, HTML-escape,
            # bare-word "silent", and empty replies. Cost of being permissive
            # is one missed chime-in; cost of being strict is the literal tag
            # posted to the channel.
            log.debug("tick: %s -> silent (raw=%r)", channel, text[:80])
            return

        log.info("tick: %s speaking: %r", channel, text[:120])
        try:
            await self.bot.irc_send(channel, text)
        except Exception:
            log.exception("tick: failed to post message to %s", channel)
