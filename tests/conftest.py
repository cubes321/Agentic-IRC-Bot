"""Shared fixtures. Each test gets its own throwaway SQLite file, so tests
never interfere with each other or with the real bot.sqlite."""

from __future__ import annotations

import pytest

from bot.config import AICfg, ChannelCfg, Config, ServerCfg
from bot.db import Database


def make_cfg(**channels: ChannelCfg) -> Config:
    """Minimal valid Config. `server.host`, `server.nick` and `ai.chat_model`
    are the only fields without defaults."""
    return Config(
        server=ServerCfg(host="irc.example.org", nick="TestBot"),
        ai=AICfg(chat_model="test-model"),
        channels=dict(channels),
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.sqlite")
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def cfg() -> Config:
    return make_cfg(**{"#test": ChannelCfg()})
