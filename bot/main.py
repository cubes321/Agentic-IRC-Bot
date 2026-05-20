"""Entry point: load config, build collaborators, connect, run until interrupted.

Usage:
    python -m bot.main config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from datetime import datetime, timezone

from .agent import AgentCore
from .auth import AuthManager
from .config import Config, load_config, load_q_password
from .db import Database
from .ircclient import IRCBot
from .memory import MemoryStore, MemoryWriter
from .scheduler import Scheduler
# Importing tools triggers self-registration into the registry.
from . import tools  # noqa: F401


def _resolve_api_key(cfg: Config) -> str:
    """Order: config -> env var -> legacy file fallback."""
    if cfg.ai.api_key and cfg.ai.api_key not in ("", "lm-studio-replace-me"):
        return cfg.ai.api_key
    env = os.environ.get("OPENAI_API_KEY")
    if env:
        return env
    legacy = Path("e:/ai/openai_api_key.txt")
    if legacy.exists():
        return legacy.read_text().strip()
    # LM Studio accepts any non-empty string; allow a literal placeholder.
    return cfg.ai.api_key or "not-needed"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _fmt_pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "  -  "


async def _print_usage_summary(db: Database, session_started_iso: str) -> None:
    """Log a token-usage summary covering this session and all-time."""
    log = logging.getLogger("bot.usage")
    try:
        session = await db.usage_summary(since_iso=session_started_iso)
        lifetime = await db.usage_summary(since_iso=None)
    except Exception:
        log.exception("could not compute usage summary")
        return

    if lifetime["totals"]["n"] == 0:
        log.info("=== Token usage: no calls recorded ===")
        return

    s_t = session["totals"]
    l_t = lifetime["totals"]

    lines: list[str] = []
    lines.append("=== Token usage summary ===")
    lines.append(
        f"This session: {_fmt_int(s_t['p'])} prompt + "
        f"{_fmt_int(s_t['c'])} completion = "
        f"{_fmt_int(s_t['t'])} tokens / {_fmt_int(s_t['n'])} calls"
    )
    lines.append(
        f"All time:     {_fmt_int(l_t['p'])} prompt + "
        f"{_fmt_int(l_t['c'])} completion = "
        f"{_fmt_int(l_t['t'])} tokens / {_fmt_int(l_t['n'])} calls"
    )
    if session["by_purpose"]:
        lines.append("By purpose (this session):")
        s_total = max(s_t["t"], 1)
        for r in session["by_purpose"]:
            t = (r["p"] or 0) + (r["c"] or 0)
            lines.append(
                f"  {r['purpose']:<14}: "
                f"{_fmt_int(r['p']):>10} prompt + {_fmt_int(r['c']):>9} completion "
                f"= {_fmt_int(t):>11} ({_fmt_pct(t, s_total):>6}) "
                f"over {_fmt_int(r['n'])} calls"
            )
    if session["by_model"]:
        lines.append("By model (this session):")
        for r in session["by_model"]:
            t = (r["p"] or 0) + (r["c"] or 0)
            lines.append(
                f"  {r['model']:<40}: {_fmt_int(t):>11} tokens "
                f"over {_fmt_int(r['n'])} calls"
            )

    for line in lines:
        log.info(line)


def _build_capabilities(cfg: Config) -> set[str]:
    caps: set[str] = set()
    if cfg.ai.vision_model:
        caps.add("vision")
    if cfg.ai.embed_model:
        caps.add("memory")
    if cfg.tools.brave_api_key:
        caps.add("brave_key")
    if cfg.tools.tavily_api_key:
        caps.add("tavily_key")
    return caps


async def run(cfg: Config, shutdown_event: asyncio.Event) -> None:
    log = logging.getLogger("bot.main")

    # Mark the session start time. Used to scope the "this session" portion
    # of the on-exit usage summary.
    session_started_iso = datetime.now(timezone.utc).isoformat()

    # Database
    db = Database(cfg.storage.db_path)
    await db.open()
    log.info("Database ready at %s", cfg.storage.db_path)

    # HTTP pool shared across tool calls
    http = httpx.AsyncClient(timeout=15.0)

    # OpenAI clients (LM Studio uses the same OpenAI-compatible API)
    api_key = _resolve_api_key(cfg)
    chat_client = AsyncOpenAI(base_url=cfg.ai.base_url, api_key=api_key)
    vision_client = (
        AsyncOpenAI(base_url=cfg.ai.effective_vision_base_url, api_key=api_key)
        if cfg.ai.vision_base_url and cfg.ai.vision_base_url != cfg.ai.base_url
        else chat_client
    )

    # Concurrency cap on outbound chat completions, shared by AgentCore,
    # MemoryStore (extractor), and Scheduler (initiative ticks). Embeddings
    # don't share — they run on a separate model.
    chat_sem = asyncio.Semaphore(max(1, cfg.budgets.max_concurrent_chat_calls))
    log.info(
        "Chat concurrency cap: %d concurrent call(s)",
        cfg.budgets.max_concurrent_chat_calls,
    )

    # Long-term memory (optional; only if embed_model is configured)
    memory: MemoryStore | None = None
    memory_writer: MemoryWriter | None = None
    if cfg.ai.embed_model:
        memory = MemoryStore(
            db=db,
            chat_client=chat_client,
            chat_model=cfg.ai.chat_model,
            embed_client=chat_client,  # same LM Studio endpoint
            embed_model=cfg.ai.embed_model,
            chat_semaphore=chat_sem,
        )
        memory_writer = MemoryWriter(memory, batch_size=cfg.memory.extractor_batch_size)
        log.info(
            "Long-term memory enabled (embed=%r, extractor batch=%d)",
            cfg.ai.embed_model, cfg.memory.extractor_batch_size,
        )
    else:
        log.info("Long-term memory disabled (no [ai].embed_model configured)")

    # Auth + agent
    auth = AuthManager(db=db, operator_accounts=set(cfg.operator.accounts))
    capabilities = _build_capabilities(cfg)
    agent = AgentCore(
        chat_client=chat_client,
        vision_client=vision_client,
        db=db,
        cfg=cfg,
        capabilities=capabilities,
        memory=memory,
        chat_semaphore=chat_sem,
    )

    # Q password (optional)
    q_password = load_q_password(cfg)
    if q_password:
        log.info("Loaded Q password (%d chars)", len(q_password))

    client = IRCBot(
        nickname=cfg.server.nick,
        realname=cfg.server.realname,
        cfg=cfg,
        db=db,
        auth=auth,
        agent=agent,
        http=http,
        q_password=q_password,
        memory_writer=memory_writer,
        shutdown_event=shutdown_event,
    )

    # Scheduler runs the long-lived background loops (initiative ticks,
    # reminder firing). Constructed here, started AFTER the IRC connection
    # is up, stopped during shutdown.
    scheduler = Scheduler(
        cfg=cfg,
        db=db,
        chat_client=chat_client,
        chat_model=cfg.ai.chat_model,
        chat_semaphore=chat_sem,
        memory=memory,
        ircbot=client,
    )

    try:
        log.info("Connecting to %s:%d (tls=%s)", cfg.server.host, cfg.server.port, cfg.server.tls)
        await client.connect(
            hostname=cfg.server.host,
            port=cfg.server.port,
            tls=cfg.server.tls,
            tls_verify=False,  # many IRCds use self-signed; revisit per-server
        )
        # Start the scheduler now that we're connected and joining channels.
        # Even if not all joins land immediately, the tick coroutine for each
        # channel checks `bot.in_channel(...)` before firing, so an early
        # tick on a still-joining channel is a no-op.
        await scheduler.start()
        # Stay alive until shutdown is requested OR the connection drops.
        # The latter doubles as a watchdog: if pydle's reader task notices
        # the socket is gone, client.connected flips False and we tear down.
        log.info(
            "Running. Trigger shutdown via !quit (operator), SIGINT/SIGTERM, "
            "or Ctrl+C. Grace period for in-flight turns: %ds.",
            cfg.shutdown.grace_sec,
        )
        while client.connected and not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass  # retry the connected-check
        if shutdown_event.is_set():
            log.info("Shutdown requested; entering graceful tear-down")
        else:
            log.warning("Connection lost; entering tear-down")
    except asyncio.CancelledError:
        # Reached if something else cancels run(); fall through to finally.
        # We set the event so the bot's on_message stops accepting work.
        log.info("Run task cancelled")
        shutdown_event.set()
    finally:
        # Mark the shutdown signal as set, in case we got here via an
        # unexpected path (e.g. connection drop). on_message uses this to
        # refuse new engagements during tear-down.
        shutdown_event.set()

        # Grace period: let in-flight reply turns finish posting before we
        # cut the connection. New ones are already refused by on_message.
        try:
            still = await client.await_inflight(float(cfg.shutdown.grace_sec))
            if still:
                log.warning("%d engagement(s) did not finish in grace period", still)
        except Exception:
            log.exception("await_inflight failed")

        # Stop the scheduler now so its loops can't try to send IRC
        # messages while we're sending goodbyes / QUIT. We do this BEFORE
        # the goodbye broadcast so a tick can't fire concurrently and
        # post a "should I chime in?" message alongside the farewell.
        try:
            await scheduler.stop()
        except Exception:
            log.exception("error stopping scheduler")

        # Per-channel goodbyes. Posts a short in-character farewell in each
        # channel with recent chat, capped by goodbye_timeout_sec across the
        # whole batch. Skipped silently if disabled (goodbye_recent_sec=0).
        # The connection is still open at this point so irc_send works; the
        # scheduler is stopped so no other LLM-driven posts compete.
        try:
            await scheduler.say_goodbyes(
                parting_message=getattr(client, "_quit_message", None),
                timeout_sec=float(cfg.shutdown.goodbye_timeout_sec),
            )
        except Exception:
            log.exception("error during goodbye broadcast")

        # Send a proper IRC QUIT with our parting message rather than
        # letting the server time us out. quit() sends QUIT then disconnects;
        # falls back to plain disconnect on older pydle versions.
        try:
            if client.connected:
                msg = client.quit_message
                log.info("Sending QUIT: %r", msg)
                if hasattr(client, "quit"):
                    await client.quit(msg)
                else:
                    await client.disconnect(expected=True)
        except Exception:
            log.exception("error during QUIT/disconnect")

        await http.aclose()
        # Print the usage summary BEFORE closing the database (it queries it).
        try:
            await _print_usage_summary(db, session_started_iso)
        except Exception:
            log.exception("usage summary failed")
        await db.close()
        log.info("Shutdown complete")


def _setup_logging(verbose: bool, log_path: str | None) -> None:
    """Console gets INFO+ (or DEBUG+ with -v); file always gets DEBUG+ for
    forensics. Third-party libraries that are chatty at DEBUG are pinned to
    INFO on the console only, but kept at DEBUG in the file."""
    console_level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Clear default handlers so re-runs in the same process don't double-log.
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_path:
        try:
            fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            root.addHandler(fh)
            logging.getLogger("bot.main").info("Debug log -> %s", log_path)
        except OSError as e:
            logging.getLogger("bot.main").warning("Could not open log file %r: %s", log_path, e)

    # Quiet down chatty libraries on the console; the file still captures DEBUG.
    for noisy in ("pydle", "httpx", "httpcore", "openai._base_client"):
        if not verbose:
            # Add a console-specific filter so DEBUG still flows to the file.
            logger = logging.getLogger(noisy)
            logger.setLevel(logging.DEBUG)  # let DEBUG reach the file handler
        # Suppress on console regardless: a custom filter on the console handler.
    if not verbose:
        class _NoisyConsoleFilter(logging.Filter):
            _noisy = ("pydle", "httpx", "httpcore", "openai._base_client", "openai._client")
            def filter(self, record: logging.LogRecord) -> bool:
                if record.levelno >= logging.WARNING:
                    return True
                return not any(record.name.startswith(p) for p in self._noisy)
        console.addFilter(_NoisyConsoleFilter())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agentic IRC bot")
    parser.add_argument("config", help="Path to TOML config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args(argv)

    # Need to load the config before we can resolve log_path; do a minimal
    # logging setup first so config-load errors are still visible.
    _setup_logging(args.verbose, log_path=None)
    cfg = load_config(args.config)
    # Re-init logging with the configured file path.
    _setup_logging(args.verbose, log_path=cfg.storage.log_path)

    # Cooperative signal handling: SIGINT/SIGTERM/KeyboardInterrupt set the
    # shutdown_event, which run() observes and tears down gracefully. We
    # DO NOT cancel the task — cancellation interrupts mid-await, which can
    # corrupt the disconnect sequence and lose the QUIT message. A flag is
    # cooperative; cancellation is not.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown_event = asyncio.Event()
    task = loop.create_task(run(cfg, shutdown_event))

    def _request_shutdown(*_: object) -> None:
        # Thread/signal-safe: Event.set() is safe to call from a signal handler.
        if not shutdown_event.is_set():
            logging.getLogger("bot.main").info(
                "Signal received; requesting graceful shutdown"
            )
            shutdown_event.set()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, _request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)

    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        # Windows path (no add_signal_handler). A second Ctrl+C while we're
        # already shutting down falls through to a hard exit — useful escape
        # hatch if something hangs.
        _request_shutdown()
        try:
            loop.run_until_complete(task)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
