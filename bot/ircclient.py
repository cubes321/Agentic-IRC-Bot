"""Pydle subclass: routes events into the agent core, handles Quakenet AUTH,
tracks channel ops and account names, sends rate-limited responses."""

from __future__ import annotations

import asyncio
import logging
import re
import textwrap
from typing import Any

import httpx
import pydle

from .agent import AgentCore
from .auth import AuthManager
from .config import Config
from .db import Database
from .memory import MemoryWriter
from .policy import ChannelPolicy

log = logging.getLogger(__name__)


# IRC line length is 512 bytes including the server prefix the IRCd will add.
# 400 leaves comfortable headroom for any prefix.
MAX_LINE_LEN = 400
INTER_LINE_DELAY = 0.5
MAX_LINES_PER_REPLY = 5
# Pace between JOINs at startup. Quakenet's flood protection silently drops
# JOINs that come too fast in the first seconds after connect. 0.7s is
# conservative; tune lower if your network is permissive.
JOIN_INTERVAL_SEC = 0.7

Q_BOT_TARGET = "Q@CServe.quakenet.org"


class IRCBot(pydle.Client):
    """Slice-1 bot: connect, join, reply to mentions and DMs with tool-using
    agent. No initiative ticks, no tasks, no long-term memory yet."""

    def __init__(
        self,
        nickname: str,
        *,
        realname: str,
        cfg: Config,
        db: Database,
        auth: AuthManager,
        agent: AgentCore,
        http: httpx.AsyncClient,
        q_password: str | None,
        memory_writer: MemoryWriter | None = None,
        shutdown_event: asyncio.Event | None = None,
        task_runner: Any = None,           # bot.tasks.TaskRunner (untyped to avoid cycle)
        **kwargs: Any,
    ):
        super().__init__(nickname=nickname, realname=realname, **kwargs)
        self.cfg = cfg
        self.db = db
        self.auth = auth
        self.agent = agent
        self.http = http
        self.q_password = q_password
        self.memory_writer = memory_writer
        # TaskRunner is wired in main.py after bot construction (chicken-and-egg:
        # TaskRunner needs `bot` to post results; bot needs `task_runner` to
        # dispatch !task commands). Set via `bot.task_runner = ...` post-init.
        # None until then; command handlers gracefully refuse if so.
        self.task_runner = task_runner
        # Shared shutdown signal: set by !quit, signal handlers, or main loop
        # exit. Checked by on_message to refuse new engagements once set. Main
        # waits on this event to know when to begin tear-down.
        self.shutdown_event = shutdown_event or asyncio.Event()
        # Engagement tasks-in-flight. We add a task to the set when we kick off
        # _handle_engagement and remove it in the task's done callback. main
        # awaits this set during the grace period before disconnecting so a
        # mid-turn LLM call has a chance to post its reply.
        self._inflight_engagements: set[asyncio.Task] = set()
        # Custom QUIT message: set by !quit if the operator passes one. Used
        # by main during the disconnect step. None means "fall back to
        # cfg.shutdown.quit_message".
        self._quit_message: str | None = None

        # Build the join list as the UNION of [server].channels and the keys
        # of [channels.*] policy sections, preserving order (explicit list first,
        # then policy-only entries). Logged at connect time so users see the
        # provenance of each entry.
        explicit = list(cfg.server.channels)
        seen = set(explicit)
        policy_only = [ch for ch in cfg.channels.keys() if ch not in seen]
        self._configured_channels = explicit + policy_only
        self._channel_source = (
            {ch: "server.channels" for ch in explicit}
            | {ch: "policy" for ch in policy_only}
        )

        self._mention_re = re.compile(rf"\b{re.escape(nickname)}\b", re.IGNORECASE)
        self._ignore = {n.lower() for n in cfg.ignore.nicks}

    # ---- connection lifecycle ----

    async def on_connect(self) -> None:
        log.info("Connected as %s", self.nickname)
        await super().on_connect()

        qn = self.cfg.server.quakenet
        if qn.q_account and self.q_password:
            log.info("Sending Q AUTH for account %s", qn.q_account)
            await self.message(Q_BOT_TARGET, f"AUTH {qn.q_account} {self.q_password}")
            await asyncio.sleep(1.5)
            if qn.auto_request_x:
                log.info("Requesting user mode +x")
                await self.rawmsg("MODE", self.nickname, "+x")
                await asyncio.sleep(0.5)

        # Quakenet (and several other networks) drop JOINs sent without a gap
        # in the first seconds after connect. We pace channel joins, log each
        # attempt, and isolate failures so one bad channel doesn't abort the
        # rest of the list.
        if self._configured_channels:
            annotated = ", ".join(
                f"{ch} ({self._channel_source.get(ch, '?')})"
                for ch in self._configured_channels
            )
            log.info("Joining %d channel(s): %s",
                     len(self._configured_channels), annotated)
        else:
            log.warning(
                "No channels configured. Add channel names to [server].channels "
                "or define a [channels.\"#name\"] section."
            )
        joined: list[str] = []
        for ch in self._configured_channels:
            try:
                log.info("Joining %s", ch)
                await self.join(ch)
                joined.append(ch)
            except Exception:
                log.exception("Failed to join %s", ch)
            await asyncio.sleep(JOIN_INTERVAL_SEC)
        log.info("Join sequence complete: %d/%d succeeded (%s)",
                 len(joined), len(self._configured_channels),
                 ", ".join(joined) or "none")

    # ---- account / op tracking ----

    async def on_raw_330(self, message: Any) -> None:
        """RPL_WHOISACCOUNT: <nick> <target> <account> :is logged in as"""
        try:
            params = message.params
            if len(params) >= 3:
                target_nick = params[1]
                account = params[2]
                self.auth.remember_account(target_nick, account)
                log.debug("Cached account: %s -> %s", target_nick, account)
        except Exception:
            log.exception("on_raw_330 failed")

    async def on_account_change(self, user: str, account: str | None) -> None:
        """Pydle hook for IRCv3 account-notify."""
        self.auth.remember_account(user, account)

    async def on_join(self, channel: str, user: str) -> None:
        await super().on_join(channel, user)
        if user == self.nickname:
            log.info("Joined %s", channel)
            return

    async def on_part(self, channel: str, user: str, message: str | None = None) -> None:
        await super().on_part(channel, user, message)
        if user != self.nickname:
            self.auth.remove_op(channel, user)

    async def on_kick(self, channel: str, target: str, by: str, reason: str | None = None) -> None:
        await super().on_kick(channel, target, by, reason)
        self.auth.remove_op(channel, target)

    async def on_quit(self, user: str, message: str | None = None) -> None:
        await super().on_quit(user, message)
        self.auth.forget_user(user)

    async def on_nick_change(self, old: str, new: str) -> None:
        await super().on_nick_change(old, new)
        self.auth.rename(old, new)

    async def on_mode_change(self, channel: str, modes: list, by: str) -> None:
        """Re-sync the channel's op set whenever any channel mode changes.

        In pydle, `self.channels[channel]['modes']` is a dict keyed by *mode
        letter*. Parameter-taking modes (`o`, `v`, `h`, `b`, `e`, `I`, `k`)
        store a list/string of parameters; flag modes (`i`, `m`, `n`, `s`,
        `t`, `C`) store a bool. We only care about `'o'` — the list of
        nicks currently holding `+o` on this channel.
        """
        await super().on_mode_change(channel, modes, by)
        try:
            ch_state = self.channels.get(channel) or {}
            modes_dict = ch_state.get("modes") or {}
            op_value = modes_dict.get("o")
            if op_value is None:
                ops: set[str] = set()
            elif isinstance(op_value, str):
                ops = {op_value}
            elif isinstance(op_value, (list, set, tuple, frozenset)):
                ops = set(op_value)
            else:
                # Unexpected shape; leave the cache untouched rather than guessing.
                log.debug(
                    "on_mode_change: unexpected modes['o'] type %s for %s",
                    type(op_value).__name__, channel,
                )
                return
            self.auth.set_channel_ops(channel, ops)
            log.debug("ops in %s: %s", channel, sorted(ops) or "(none)")
        except Exception:
            log.exception("on_mode_change op resync failed for %s", channel)

    # ---- message handling ----

    async def on_message(self, target: str, source: str, message: str) -> None:
        await super().on_message(target, source, message)

        if source == self.nickname:
            return
        if source.lower() in self._ignore:
            return

        # Determine reply target and whether we should engage.
        is_dm = target == self.nickname
        reply_target = source if is_dm else target

        # Persist all public-channel messages for log_search, recent buffer,
        # and the memory extractor. We do this BEFORE the shutdown check so
        # the message log stays continuous up to the very last second; we'd
        # rather have an extra row than a gap in the transcript.
        if not is_dm:
            account = self._cached_account_or_none(source)
            await self.db.log_message(target, source, account, message)
            if self.memory_writer is not None:
                self.memory_writer.note_message(target)

        # For command dispatch, strip a leading bot-nick mention so commands
        # work whether typed bare ("!task X") or addressed to the bot
        # ("CubesBot: !task X", "CubesBot !task X", "CubesBot, !task X").
        # Without this, only the bare form matches — real users almost always
        # prefix commands with the bot's nick when addressing it.
        # Engagement detection further down still uses the ORIGINAL message
        # so mentions are correctly detected for non-command messages.
        stripped = message.strip()
        cmd_text = self._mention_re.sub("", stripped, count=1).strip(" :,") or stripped

        # Operator-only !quit command. Handled BEFORE the shutdown gate
        # (otherwise a stuck shutdown couldn't be re-triggered) but only by
        # operators (so a random user can't kill the bot).
        if cmd_text == "!quit" or cmd_text.startswith("!quit "):
            parting = cmd_text[5:].strip() or None
            asyncio.create_task(
                self._cmd_quit(reply_target, source, parting)
            )
            return

        # If we're shutting down, refuse new engagements silently. The
        # message log still got the line above. We don't reply with "I'm
        # shutting down" because that would itself be an LLM call we then
        # need to wait on.
        if self.shutdown_event.is_set():
            log.debug("Ignoring message from %s: shutdown in progress", source)
            return

        # Chat-style commands (handled BEFORE engagement so they don't burn a
        # mention or an LLM call). Public-channel only; DMs go to agent.
        if not is_dm and cmd_text == "!memory_stats":
            asyncio.create_task(self._cmd_memory_stats(target))
            return

        # Slice 2c: task commands. Channel-only; auth gated inside the handler.
        # All three are early-dispatched so they don't burn a reply turn or
        # require a mention — they're plain commands, not bot addressing.
        # Each command form accepts both "!cmd" (bare, prompts usage if args
        # are required) and "!cmd <args>" — that way "CubesBot !task" alone
        # gets a usage hint instead of falling through to engagement.
        if not is_dm and (cmd_text == "!tasks" or cmd_text.startswith("!tasks ")):
            asyncio.create_task(self._cmd_list_tasks(target))
            return
        if not is_dm and (cmd_text == "!cancel" or cmd_text.startswith("!cancel ")):
            asyncio.create_task(self._cmd_cancel_task(target, source, cmd_text))
            return
        if not is_dm and (cmd_text == "!task" or cmd_text.startswith("!task ")):
            asyncio.create_task(self._cmd_issue_task(target, source, cmd_text))
            return

        # Engagement: DMs always engage; channel messages need a mention.
        if not is_dm and not self._mention_re.search(message):
            return

        # Make sure we have an account cached if possible.
        await self._ensure_account_known(source)
        account = self._cached_account_or_none(source)

        channel_for_policy = reply_target if not is_dm else target  # for DMs, target == bot nick; treat as private quiet
        policy = self._policy_for(channel_for_policy if not is_dm else "")

        # Strip the bot's own nick from a mention to clean the trigger.
        trigger = self._mention_re.sub("", message).strip(" :,") if not is_dm else message
        if not trigger:
            trigger = message

        log.info("Engaging in %s for %s: %r", reply_target, source, trigger[:120])

        # Run the agent in a background task so a slow LLM call does not block
        # other events (other channels' messages, joins, etc.). Register it in
        # _inflight_engagements so shutdown can wait for it during the grace
        # period; auto-remove on completion via done_callback.
        task = asyncio.create_task(
            self._handle_engagement(reply_target, source, account, trigger, policy),
            name=f"engagement.{reply_target}.{source}",
        )
        self._inflight_engagements.add(task)
        task.add_done_callback(self._inflight_engagements.discard)

    async def on_ctcp_action(self, by: str, target: str, contents: str) -> None:
        """Pydle dispatches IRC actions (/me) here, NOT through on_message.

        Without this handler, actions are invisible to the bot:
          - Not logged to message_log (transcript has gaps, extractor misses)
          - Mention detection doesn't fire (no engagement when someone says
            '/me pings BotNick')
          - Initiative-tick context buffer is incomplete

        We mirror the on_message logic, with two twists:
          1. The action is stored and shown to the LLM in '* nick text' form
             so the action context is preserved (matters for the bot's
             response — 'waves back' makes sense only if it knew you waved).
          2. We do NOT strip the bot's nick from the trigger. In a regular
             message, 'BotNick: hello' has the nick as an address marker;
             in an action, 'pokes BotNick' has the nick as content.
        """
        # Self-echo guard. Pydle suppresses most echoes but be defensive.
        if by == self.nickname:
            return
        if by.lower() in self._ignore:
            return

        is_dm = target == self.nickname
        reply_target = by if is_dm else target

        # Log + extractor notification for public channels. We format as
        # '* by contents' so the message_log and recent-buffer renderers
        # show actions identifiably (matches IRC client convention).
        formatted = f"* {by} {contents}"
        if not is_dm:
            account = self._cached_account_or_none(by)
            await self.db.log_message(target, by, account, formatted)
            if self.memory_writer is not None:
                self.memory_writer.note_message(target)

        # Shutdown gate. Actions don't carry !quit (CTCP wrapper would have
        # been processed differently), so no operator-command branch here.
        if self.shutdown_event.is_set():
            log.debug("Ignoring action from %s: shutdown in progress", by)
            return

        # Engagement: DMs (rare for actions, but possible) always engage;
        # channel actions need a mention of the bot's nick in the action text.
        if not is_dm and not self._mention_re.search(contents):
            return

        await self._ensure_account_known(by)
        account = self._cached_account_or_none(by)

        channel_for_policy = reply_target if not is_dm else target
        policy = self._policy_for(channel_for_policy if not is_dm else "")

        # Pass the action through verbatim (no mention-strip): the bot's nick
        # in an action is content, not an address marker. The '* by ...'
        # prefix tells the LLM this was an action so it can respond in kind.
        trigger = formatted

        log.info("Engaging in %s for %s (action): %r", reply_target, by, contents[:120])

        task = asyncio.create_task(
            self._handle_engagement(reply_target, by, account, trigger, policy),
            name=f"engagement.{reply_target}.{by}.action",
        )
        self._inflight_engagements.add(task)
        task.add_done_callback(self._inflight_engagements.discard)

    async def _handle_engagement(
        self,
        reply_target: str,
        source: str,
        account: str | None,
        trigger: str,
        policy: ChannelPolicy,
    ) -> None:
        try:
            text = await self.agent.run_reply_turn(
                bot=self,
                channel=reply_target,
                policy=policy,
                actor_nick=source,
                actor_account=account,
                trigger_text=trigger,
                http=self.http,
            )
        except Exception:
            log.exception("agent run_reply_turn failed")
            await self.irc_send(reply_target, "(internal error in agent loop)")
            return

        if text:
            await self.irc_send(reply_target, text)

    # ---- chat commands ----

    async def _cmd_quit(
        self,
        reply_target: str,
        source: str,
        parting: str | None,
    ) -> None:
        """Operator-gated shutdown command. Acknowledges to the issuer, sets
        the shutdown_event so main begins tear-down, and records the parting
        message so main's QUIT step uses it.

        Authorisation reuses auth.is_operator (account-based). A channel-op
        on the local channel is NOT sufficient — operator status here is
        bot-wide, not channel-scoped. Locking shutdown to the smaller set
        prevents anyone who happens to be opped in #foo from killing the
        bot for everyone else."""
        await self._ensure_account_known(source)
        account = self._cached_account_or_none(source)
        if not self.auth.is_operator(account):
            log.warning(
                "!quit refused: %s (account=%r) is not an operator",
                source, account,
            )
            await self.irc_send(
                reply_target,
                "(refusing !quit: not an operator)",
            )
            return

        if self.shutdown_event.is_set():
            await self.irc_send(reply_target, "(already shutting down)")
            return

        # Record the parting message and trip the event. main will pick this
        # up on its next loop wake-up and start the tear-down sequence.
        if parting:
            self._quit_message = parting
        log.info(
            "!quit accepted from %s (account=%s); parting=%r",
            source, account, parting,
        )
        await self.irc_send(reply_target, "shutting down…")
        self.shutdown_event.set()

    async def await_inflight(self, timeout_sec: float) -> int:
        """Wait up to timeout_sec for all in-flight engagement tasks to finish.

        Returns the number of tasks still running when the wait ended (0 = all
        finished cleanly). Main uses this during the grace period so a mid-turn
        LLM call has a chance to post its reply before the disconnect.
        Tasks NOT cancelled here — main will let the asyncio loop close them
        out after the disconnect."""
        if not self._inflight_engagements:
            return 0
        pending = list(self._inflight_engagements)
        log.info("Waiting up to %.0fs for %d in-flight engagement(s)",
                 timeout_sec, len(pending))
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            still = sum(1 for t in pending if not t.done())
            log.warning(
                "Grace period expired with %d engagement(s) still running",
                still,
            )
            return still
        return 0

    @property
    def quit_message(self) -> str:
        """The QUIT line to use on disconnect. !quit override beats config default."""
        return self._quit_message or self.cfg.shutdown.quit_message

    # ---- task commands (slice 2c) ----

    async def _cmd_issue_task(self, channel: str, source: str, stripped: str) -> None:
        """`!task <goal>` — schedule a multi-step background task.

        Auth gate: ChannelPolicy.can_issue_tasks(actor). The actor's account
        is the source of truth (nicks can change); if the account isn't
        cached yet, we WHOIS first so the policy check sees real identity.
        """
        if self.task_runner is None:
            await self.irc_send(channel, "(tasks not available — TaskRunner not wired)")
            return

        goal = stripped[len("!task "):].strip()
        if not goal:
            await self.irc_send(channel, "Usage: !task <goal>  (e.g. '!task summarise https://example.com')")
            return
        if len(goal) > 500:
            await self.irc_send(channel, "(goal too long; max 500 chars)")
            return

        # Resolve identity for auth. ChannelPolicy.can_issue_tasks reads
        # actor.is_operator (account-based) and actor.is_op_in_channel (live
        # +o on this channel). We need both to be accurate, so WHOIS first
        # if the account isn't cached.
        await self._ensure_account_known(source)
        actor = self.auth.actor_for(source, channel)

        policy = self._policy_for(channel)
        if not policy.can_issue_tasks(actor):
            issuers = policy.cfg.task_issuers
            log.info(
                "!task refused: %s (account=%r, op=%s) lacks task_issuers='%s' in %s",
                source, actor.account, actor.is_op_in_channel, issuers, channel,
            )
            await self.irc_send(
                channel,
                f"(refusing !task: this channel restricts tasks to '{issuers}')",
            )
            return

        try:
            task_id = await self.task_runner.issue_task(
                channel=channel,
                owner_nick=source,
                owner_account=actor.account,
                goal=goal,
            )
        except Exception as e:
            log.exception("issue_task failed in %s", channel)
            await self.irc_send(channel, f"(failed to schedule task: {e})")
            return

        await self.irc_send(channel, f"[task #{task_id}] starting: {goal}")

    async def _cmd_cancel_task(self, channel: str, source: str, stripped: str) -> None:
        """`!cancel <id>` — cancel a running task. Auth = issuer or operator."""
        if self.task_runner is None:
            await self.irc_send(channel, "(tasks not available — TaskRunner not wired)")
            return

        rest = stripped[len("!cancel "):].strip()
        try:
            task_id = int(rest)
        except ValueError:
            await self.irc_send(channel, "Usage: !cancel <id>  (id is the number from '[task #N]')")
            return

        await self._ensure_account_known(source)
        account = self._cached_account_or_none(source)
        is_operator = self.auth.is_operator(account)

        msg = await self.task_runner.cancel_task(
            task_id=task_id,
            cancelled_by_nick=source,
            cancelled_by_account=account,
            is_operator=is_operator,
        )
        await self.irc_send(channel, msg)

    async def _cmd_list_tasks(self, channel: str) -> None:
        """`!tasks` — list recent tasks for this channel. Read-only, no auth."""
        if self.task_runner is None:
            await self.irc_send(channel, "(tasks not available — TaskRunner not wired)")
            return

        try:
            rows = await self.task_runner.list_tasks(channel)
        except Exception:
            log.exception("list_tasks failed for %s", channel)
            await self.irc_send(channel, "(failed to list tasks; see logs)")
            return

        if not rows:
            await self.irc_send(channel, f"No tasks in {channel} yet.")
            return

        # One-line-per-task render. Truncate goals so the line fits IRC width.
        for r in rows:
            goal_preview = (r["goal"] or "")[:60]
            if len(r["goal"] or "") > 60:
                goal_preview += "…"
            # created_at is ISO; trim to HH:MM:SS for readability in the channel.
            ts = (r["created_at"] or "")[11:19] or "?"
            await self.irc_send(
                channel,
                f"  #{r['id']} [{r['status']}] {ts} {r['owner_nick']}: {goal_preview}",
            )

    async def _cmd_memory_stats(self, channel: str) -> None:
        """Post a quick diagnostic about this channel's memory store. Reads
        from the DB (no LLM calls), so it's cheap and always-available."""
        try:
            stats = await self.db.memory_stats(channel)
        except Exception:
            log.exception("memory_stats query failed for %s", channel)
            await self.irc_send(channel, "(memory_stats query failed; see logs)")
            return

        total = stats["total"]
        if total == 0:
            await self.irc_send(channel, f"No memories stored for {channel} yet.")
            return

        kinds = stats["by_kind"]
        kinds_str = ", ".join(f"{r['kind']}={r['n']}" for r in kinds) or "none"

        top = stats["top_users"][:3]
        top_str = ", ".join(f"{r['who']}={r['n']}" for r in top) or "none"

        # Compress ISO timestamps to date for readability
        oldest = (stats["oldest"] or "?")[:10]
        newest = (stats["newest"] or "?")[:10]

        line1 = f"Memory in {channel}: {total} item(s); kinds: {kinds_str}"
        line2 = f"Oldest: {oldest}, newest: {newest}; top: {top_str}"
        await self.irc_send(channel, line1)
        await self.irc_send(channel, line2)

    # ---- helpers ----

    def _policy_for(self, channel: str) -> ChannelPolicy:
        cfg = self.cfg.channel(channel) if channel else self.cfg.channel("")
        return ChannelPolicy(channel or "(dm)", cfg, self.cfg)

    def _cached_account_or_none(self, nick: str) -> str | None:
        cached = self.auth.get_cached(nick)
        from .auth import _MISS
        if cached is _MISS:
            return None
        return cached  # type: ignore[return-value]

    async def _ensure_account_known(self, nick: str) -> None:
        from .auth import _MISS
        if self.auth.get_cached(nick) is not _MISS:
            return
        try:
            info = await self.whois(nick)
        except Exception:
            log.debug("whois %s failed", nick)
            self.auth.remember_account(nick, None)
            return
        account = None
        if isinstance(info, dict):
            account = info.get("account") or info.get("identified_as")
        self.auth.remember_account(nick, account)

    async def irc_send(
        self,
        target: str,
        text: str,
        *,
        continuation_prefix: str = "",
        max_lines: int | None = None,
    ) -> None:
        """Send `text` to `target` as one or more PRIVMSGs.

        Behaviour:
        - Splits `text` on newlines; empty / whitespace-only lines are dropped.
        - Each remaining line is sanitised (control chars stripped, ends
          trimmed) and then **word-wrapped** to fit MAX_LINE_LEN. Earlier
          revisions hard-truncated at the byte boundary with an ellipsis,
          which discarded real content for long task results; word-wrap
          preserves all the text at the cost of more PRIVMSGs.
        - `continuation_prefix` is prepended to every wrap-continuation
          chunk (NOT the first chunk of each logical line — the caller
          has already done any first-line prefixing). textwrap's
          `subsequent_indent` does this for us and accounts for the
          indent in the width budget, so each emitted wire-line is
          ≤ MAX_LINE_LEN total.
        - `max_lines` caps total wire-lines emitted per call (default
          MAX_LINES_PER_REPLY). Tasks pass a higher number; reply turns
          keep the existing cap to prevent runaway flooding.

        Failure mode: any exception from the underlying `self.message`
        call aborts the rest of the send for this invocation and is
        logged. The remaining text is dropped (acceptable; the caller
        usually has the full text stored elsewhere — DB row for tasks,
        log_message row for replies).
        """
        if not text:
            return
        cap = MAX_LINES_PER_REPLY if max_lines is None else max_lines
        sent = 0
        for raw in text.splitlines():
            if sent >= cap:
                break
            cleaned = "".join(c for c in raw if c.isprintable() or c == " ").strip()
            if not cleaned:
                continue
            # Word-wrap. `subsequent_indent` is the per-continuation prefix;
            # textwrap subtracts its length from the width when budgeting
            # the wrapped chunks, so each output line is ≤ MAX_LINE_LEN
            # total. `break_long_words=True` is a fallback for cases like
            # a single 500-char URL with no spaces (rare but defensible).
            # `break_on_hyphens=False` keeps hyphenated phrases like
            # "off-book" or "Netflix-style" intact on one line.
            chunks = textwrap.wrap(
                cleaned,
                width=MAX_LINE_LEN,
                subsequent_indent=continuation_prefix,
                break_long_words=True,
                break_on_hyphens=False,
            )
            if not chunks:
                # Defensive — wrap should never return [] for non-empty input
                # with break_long_words=True, but if continuation_prefix is
                # absurdly long the fallback keeps us functional.
                chunks = [cleaned[:MAX_LINE_LEN]]
            for chunk in chunks:
                if sent >= cap:
                    break
                # Safety net: if `continuation_prefix` alone is longer
                # than MAX_LINE_LEN (caller error), textwrap can still
                # emit an over-length chunk. Hard-truncate here as the
                # last line of defence.
                if len(chunk) > MAX_LINE_LEN:
                    chunk = chunk[:MAX_LINE_LEN - 1] + "…"
                try:
                    await self.message(target, chunk)
                except Exception:
                    log.exception("failed to send line to %s", target)
                    return
                sent += 1
                await asyncio.sleep(INTER_LINE_DELAY)
