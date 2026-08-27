"""Unit tests for bot.tells.TellStore."""

from __future__ import annotations

import pytest

from bot.db import Database


async def test_tells_table_exists(db: Database):
    row = await db.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tells'"
    )
    assert row is not None, "tells table was not created by SCHEMA"


async def test_tells_table_columns(db: Database):
    rows = await db.fetchall("PRAGMA table_info(tells)")
    names = {r["name"] for r in rows}
    assert names == {
        "id", "channel", "from_nick", "from_account", "from_key",
        "to_nick", "to_nick_lower", "to_account", "message",
        "created_at", "expires_at", "delivered_at",
    }


def test_allow_tell_defaults_true():
    from bot.config import ChannelCfg
    assert ChannelCfg().allow_tell is True
