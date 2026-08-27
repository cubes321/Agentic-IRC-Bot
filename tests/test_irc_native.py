"""Unit tests for bot.tools.irc_native — the private_msg tool."""

from __future__ import annotations

import pytest

from bot.db import Database
from bot.tools import ToolContext
from bot.tools import irc_native
from bot.tools.irc_native import _private_msg


class FakeIRCBot:
    """Stand-in for IRCBot. Mirrors the real irc_send contract: returns the
    count of wire lines sent, and returns 0 rather than raising when the
    send fails (IRCBot.irc_send catches its own exceptions)."""

    def __init__(self, fail: bool = False, raises: bool = False):
        self.nickname = "TestBot"
        # Shape pydle uses: bot.channels[ch]['users'].
        self.channels = {"#test": {"users": {"alice", "dave"}}}
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


@pytest.fixture(autouse=True)
def clean_dm_rate_limit():
    """The DM rate limiter is module-level state; give each test a fresh
    budget so ordering can't make a test fail spuriously."""
    irc_native._dm_history.clear()
    yield
    irc_native._dm_history.clear()


def make_ctx(db: Database, cfg, bot: FakeIRCBot) -> ToolContext:
    return ToolContext(
        bot=bot,
        channel="#test",
        actor_nick="alice",
        actor_account="alice_acct",
        cfg=cfg,
        db=db,
        http=None,
        llm_chat=None,
        llm_vision=None,
    )


async def audit_rows(db: Database) -> list:
    return await db.fetchall("SELECT * FROM audit WHERE action = 'private_msg'")


async def test_private_msg_sends_and_audits(db, cfg):
    bot = FakeIRCBot()
    r = await _private_msg(make_ctx(db, cfg, bot), {
        "target_nick": "dave", "message": "check the logs",
    })
    assert r["sent"] is True
    assert bot.sent == [("dave", "check the logs")]
    assert len(await audit_rows(db)) == 1


async def test_private_msg_reports_error_when_send_returns_zero(db, cfg):
    """The regression: irc_send returns 0 on a wire failure instead of
    raising, so the `except` never runs and the tool told the model the
    DM was delivered when nothing reached the wire."""
    r = await _private_msg(make_ctx(db, cfg, FakeIRCBot(fail=True)), {
        "target_nick": "dave", "message": "check the logs",
    })
    assert "error" in r, "a DM that never reached the wire must not report success"
    assert r.get("sent") is not True


async def test_private_msg_does_not_audit_a_dm_that_never_sent(db, cfg):
    """The audit table is a forensic record. A row for a DM that never
    left the bot is worse than no row at all."""
    await _private_msg(make_ctx(db, cfg, FakeIRCBot(fail=True)), {
        "target_nick": "dave", "message": "check the logs",
    })
    assert await audit_rows(db) == []


async def test_private_msg_reports_error_when_send_raises(db, cfg):
    """Belt-and-braces: irc_send swallows its own exceptions today, but if
    that ever changes the tool must still report failure."""
    r = await _private_msg(make_ctx(db, cfg, FakeIRCBot(raises=True)), {
        "target_nick": "dave", "message": "check the logs",
    })
    assert "error" in r
    assert await audit_rows(db) == []
