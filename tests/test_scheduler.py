"""Unit tests for bot.scheduler.Scheduler reminder firing."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from bot.config import Config
from bot.db import Database
from bot.scheduler import Scheduler


class FakeBot:
    """Stand-in for IRCBot. Records what would go on the wire.

    Mirrors the real irc_send contract deliberately: it returns the count
    of wire lines sent and, on a wire failure, returns 0 rather than
    raising. Writing this double to raise instead is what let the
    original _fire_reminder bug hide — the production `except` branch
    can never run, because irc_send catches its own exceptions."""

    def __init__(self, fail: bool = False, raises: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail
        self.raises = raises

    async def irc_send(self, target: str, text: str, **kwargs) -> int:
        if self.raises:
            raise RuntimeError("wire is down")
        if self.fail:
            return 0          # what the real irc_send does on failure
        self.sent.append((target, text))
        return 1


def make_scheduler(cfg: Config, db: Database, bot: FakeBot) -> Scheduler:
    """A Scheduler wired for reminder firing only. _fire_reminder touches
    nothing but self.db and self.bot, so the LLM plumbing stays None."""
    return Scheduler(
        cfg=cfg,
        db=db,
        chat_client=None,
        chat_model="test-model",
        chat_semaphore=None,
        memory=None,
        ircbot=bot,
    )


async def add_due_reminder(
    db: Database,
    channel: str = "#test",
    message: str = "water the plants",
    target: str = "",
) -> int:
    """Insert a reminder that is already due and return its id."""
    fire_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    payload = json.dumps({"target_nick": target, "message": message})
    return await db.insert_returning_id(
        "INSERT INTO reminders (channel, fire_at, payload) VALUES (?, ?, ?)",
        (channel, fire_at, payload),
    )


async def reminder_exists(db: Database, rid: int) -> bool:
    return await db.fetchone("SELECT id FROM reminders WHERE id = ?", (rid,)) is not None


async def due_row(db: Database, rid: int):
    """The row exactly as _reminder_loop hands it to _fire_reminder."""
    rows = await db.due_reminders()
    return next(r for r in rows if r["id"] == rid)


async def test_fire_reminder_posts_and_deletes(db, cfg):
    rid = await add_due_reminder(db)
    bot = FakeBot()
    await make_scheduler(cfg, db, bot)._fire_reminder(await due_row(db, rid))
    assert bot.sent == [("#test", "reminder: water the plants")]
    assert not await reminder_exists(db, rid)


async def test_fire_reminder_keeps_row_when_send_returns_zero(db, cfg):
    """The regression: irc_send returns 0 on a wire failure instead of
    raising, so the `except` retry branch never runs and the row was
    deleted for a reminder that was never posted."""
    rid = await add_due_reminder(db)
    await make_scheduler(cfg, db, FakeBot(fail=True))._fire_reminder(
        await due_row(db, rid)
    )
    assert await reminder_exists(db, rid), \
        "a reminder that never reached the wire must survive for the next poll"


async def test_fire_reminder_retries_on_the_next_cycle(db, cfg):
    """A row left pending by a failed send is picked up and posted next time."""
    rid = await add_due_reminder(db)
    await make_scheduler(cfg, db, FakeBot(fail=True))._fire_reminder(
        await due_row(db, rid)
    )
    bot = FakeBot()
    await make_scheduler(cfg, db, bot)._fire_reminder(await due_row(db, rid))
    assert bot.sent == [("#test", "reminder: water the plants")]
    assert not await reminder_exists(db, rid)


async def test_fire_reminder_keeps_row_when_send_raises(db, cfg):
    """Belt-and-braces: irc_send swallows its own exceptions today, but if
    that ever changes the row must still be left pending."""
    rid = await add_due_reminder(db)
    await make_scheduler(cfg, db, FakeBot(raises=True))._fire_reminder(
        await due_row(db, rid)
    )
    assert await reminder_exists(db, rid)
