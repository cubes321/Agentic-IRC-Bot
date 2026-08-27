"""Unit tests for bot.tells.TellStore."""

from __future__ import annotations

import asyncio

import pytest

from bot.db import Database, now_utc_iso


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


from bot.tells import TELL_MAX_MESSAGE_LEN, TellStore, actor_key


def test_actor_key_prefers_account():
    assert actor_key("Alice", "alice_acct") == "alice_acct"


def test_actor_key_falls_back_to_lowercased_nick():
    assert actor_key("Alice", None) == "nick:alice"


def test_actor_key_namespaces_cannot_collide():
    # Account "alice" and nick "alice" must be different people.
    assert actor_key("bob", "alice") != actor_key("alice", None)


async def test_create_succeeds(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account=None, message="check the logs",
        bot_nick="TestBot",
    )
    assert r.ok is True
    assert r.tell_id is not None


async def test_create_stores_normalised_fields(store: TellStore):
    await store.create(
        channel="#test", from_nick="Alice", from_account="alice_acct",
        to_nick="DaVe", to_account="dave_acct", message="hi",
        bot_nick="TestBot",
    )
    row = await store.db.fetchone("SELECT * FROM tells")
    assert row["to_nick"] == "DaVe"          # preserved for display
    assert row["to_nick_lower"] == "dave"    # lowercased for matching
    assert row["from_key"] == "alice_acct"
    assert row["delivered_at"] is None


# NB: "#geeks" is deliberately NOT in this list. Channel prefixes are
# stripped BEFORE validation (spec step 1, the L5 fix), so "#geeks"
# becomes the valid nick "geeks" and is accepted — see
# test_create_strips_channel_prefix_then_validates below.
@pytest.mark.parametrize("bad", ["", "   ", "#", "!!!", "9lives", "a" * 40])
async def test_create_rejects_bad_target_nicks(store: TellStore, bad: str):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick=bad, to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is False


@pytest.mark.parametrize("good", ["dave", "Dave_", "d-a-v-e", "[dave]", "da|ve"])
async def test_create_accepts_valid_nicks(store: TellStore, good: str):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick=good, to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is True


async def test_create_strips_channel_prefix_then_validates(store: TellStore):
    # "#dave" strips to "dave", which is a valid nick, so this succeeds
    # with the stripped value. Guards the L5 failure mode where the model
    # passes a channel name as the target.
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="#dave", to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is True
    row = await store.db.fetchone("SELECT to_nick_lower FROM tells")
    assert row["to_nick_lower"] == "dave"


async def test_create_refuses_the_bot_itself(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="TestBot", to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is False
    assert "myself" in r.message.lower()


async def test_create_allows_self_tell(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="alice", to_account=None, message="note to self",
        bot_nick="TestBot",
    )
    assert r.ok is True


async def test_create_rejects_empty_message(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account=None, message="   ", bot_nick="TestBot",
    )
    assert r.ok is False


async def test_create_truncates_long_message(store: TellStore):
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account=None, message="x" * 900, bot_nick="TestBot",
    )
    assert r.ok is True
    row = await store.db.fetchone("SELECT message FROM tells")
    assert len(row["message"]) == TELL_MAX_MESSAGE_LEN
    assert "truncat" in r.message.lower()


async def test_create_refused_when_allow_tell_false(tmp_path):
    # Built inline rather than via a conftest helper: `tests/` has no
    # __init__.py, so `from tests.conftest import ...` would not resolve.
    from bot.config import AICfg, ChannelCfg, Config, ServerCfg
    from bot.db import Database

    database = Database(tmp_path / "off.sqlite")
    await database.open()
    try:
        off = Config(
            server=ServerCfg(host="irc.example.org", nick="TestBot"),
            ai=AICfg(chat_model="test-model"),
            channels={"#off": ChannelCfg(allow_tell=False)},
        )
        s = TellStore(db=database, cfg=off)
        r = await s.create(
            channel="#off", from_nick="alice", from_account=None,
            to_nick="dave", to_account=None, message="hi", bot_nick="TestBot",
        )
        assert r.ok is False
        assert "disabled" in r.message.lower()
    finally:
        await database.close()


from bot.tells import (
    TELL_MAX_PENDING_PER_PAIR,
    TELL_MAX_PENDING_PER_RECIPIENT,
    TELL_RATE_LIMIT,
)


async def _leave(store, to="dave", frm="alice", account=None, channel="#test"):
    return await store.create(
        channel=channel, from_nick=frm, from_account=account,
        to_nick=to, to_account=None, message="hi", bot_nick="TestBot",
    )


async def test_hourly_rate_limit_allows_up_to_the_cap(store):
    # Each goes to a different recipient so only the hourly cap can bite.
    for i in range(TELL_RATE_LIMIT):
        r = await _leave(store, to=f"dave{i}")
        assert r.ok is True, f"create #{i + 1} should have been allowed"


async def test_hourly_rate_limit_refuses_past_the_cap(store):
    for i in range(TELL_RATE_LIMIT):
        await _leave(store, to=f"dave{i}")
    r = await _leave(store, to="someone-else")
    assert r.ok is False
    assert "rate limit" in r.message.lower()


async def test_rate_limit_is_per_actor(store):
    for i in range(TELL_RATE_LIMIT):
        await _leave(store, to=f"dave{i}", frm="alice")
    # A different person still has their own budget.
    r = await _leave(store, to="dave", frm="bob")
    assert r.ok is True


async def test_pending_per_pair_cap(store):
    # TELL_RATE_LIMIT and TELL_MAX_PENDING_PER_PAIR are both 5, but create()
    # checks the pair cap BEFORE the hourly one, so the 6th is refused for
    # the pair reason. Asserting on the message is what pins that ordering.
    for i in range(TELL_MAX_PENDING_PER_PAIR):
        r = await _leave(store, to="dave")
        assert r.ok is True, f"pair create #{i + 1} should have been allowed"
    r = await _leave(store, to="dave")
    assert r.ok is False
    assert "already" in r.message.lower()


async def test_pending_per_pair_cap_is_per_recipient(store, monkeypatch):
    # Lift the hourly cap so only the pair cap can bite. Patching the module
    # constant beats reaching into store._rate_history: it touches no
    # privates and reads as what it is.
    monkeypatch.setattr("bot.tells.TELL_RATE_LIMIT", 100)
    for _ in range(TELL_MAX_PENDING_PER_PAIR):
        await _leave(store, to="dave")
    r = await _leave(store, to="erin")   # different recipient, fresh pair
    assert r.ok is True


async def test_pending_per_recipient_cap_across_senders(store):
    # Fill dave's inbox from enough distinct senders to exceed the
    # per-recipient cap without any one of them hitting the pair cap.
    # Each sender has their own hourly budget, so no patching is needed.
    senders = [f"sender{i}" for i in range(4)]
    made = 0
    for s in senders:
        for _ in range(TELL_MAX_PENDING_PER_PAIR):
            r = await _leave(store, to="dave", frm=s)
            if r.ok:
                made += 1
            else:
                assert "too many" in r.message.lower()
                assert made == TELL_MAX_PENDING_PER_RECIPIENT
                return
    pytest.fail(f"per-recipient cap never fired (made {made})")


async def test_delivered_tells_do_not_count_against_caps(store, monkeypatch):
    monkeypatch.setattr("bot.tells.TELL_RATE_LIMIT", 100)
    for _ in range(TELL_MAX_PENDING_PER_PAIR):
        await _leave(store, to="dave")
    # Mark them all delivered; the pair budget should free up.
    await store.db.execute(
        "UPDATE tells SET delivered_at = ? WHERE delivered_at IS NULL",
        ("2026-08-27T00:00:00+00:00",),
    )
    r = await _leave(store, to="dave")
    assert r.ok is True


async def test_concurrent_create_cannot_exceed_pair_cap(store, monkeypatch):
    """TOCTOU guard. create() counts pending rows and then inserts, with
    awaits in between; ircclient dispatches command handlers via
    create_task, so two !tell calls really can interleave there. Without
    _create_lock several of these gathered calls all see the same
    below-cap count and all insert."""
    monkeypatch.setattr("bot.tells.TELL_RATE_LIMIT", 100)
    for _ in range(TELL_MAX_PENDING_PER_PAIR - 1):
        await _leave(store, to="dave")

    results = await asyncio.gather(*[_leave(store, to="dave") for _ in range(5)])

    assert sum(1 for r in results if r.ok) == 1, "exactly one should win the last slot"
    rows = await store.db.fetchall(
        "SELECT id FROM tells "
        "WHERE to_nick_lower = 'dave' AND delivered_at IS NULL"
    )
    assert len(rows) == TELL_MAX_PENDING_PER_PAIR


async def test_pending_for_matches_exact_nick(store):
    await _leave(store, to="dave")
    rows = await store.pending_for("#test", "dave", None)
    assert len(rows) == 1


async def test_pending_for_matches_nick_case_insensitively(store):
    await _leave(store, to="DaVe")
    rows = await store.pending_for("#test", "dAvE", None)
    assert len(rows) == 1


async def test_pending_for_matches_on_account_when_nick_changed(store):
    # The whole point of the hybrid: dave was known as "dave" with account
    # "dave_acct" when the tell was left, and comes back as "dave_".
    await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account="dave_acct", message="hi",
        bot_nick="TestBot",
    )
    rows = await store.pending_for("#test", "dave_", "dave_acct")
    assert len(rows) == 1


async def test_pending_for_ignores_unrelated_people(store):
    await _leave(store, to="dave")
    assert await store.pending_for("#test", "erin", "erin_acct") == []


async def test_pending_for_does_not_match_null_account(store):
    # A tell with to_account NULL must not match everyone who arrives
    # without an account. Guards a NULL-comparison mistake in the WHERE.
    await _leave(store, to="dave")
    assert await store.pending_for("#test", "erin", None) == []


async def test_pending_for_is_channel_scoped(store):
    await _leave(store, to="dave", channel="#test")
    assert await store.pending_for("#other", "dave", None) == []


async def test_pending_for_skips_expired(store):
    await _leave(store, to="dave")
    await store.db.execute(
        "UPDATE tells SET expires_at = ?", ("2020-01-01T00:00:00+00:00",)
    )
    assert await store.pending_for("#test", "dave", None) == []


async def test_pending_for_skips_delivered(store):
    await _leave(store, to="dave")
    await store.db.execute("UPDATE tells SET delivered_at = ?", (now_utc_iso(),))
    assert await store.pending_for("#test", "dave", None) == []


async def test_pending_for_is_oldest_first(store):
    for i in range(3):
        await store.create(
            channel="#test", from_nick="alice", from_account=None,
            to_nick="dave", to_account=None, message=f"msg{i}",
            bot_nick="TestBot",
        )
    rows = await store.pending_for("#test", "dave", None)
    assert [r["message"] for r in rows] == ["msg0", "msg1", "msg2"]
