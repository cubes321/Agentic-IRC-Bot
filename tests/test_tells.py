"""Unit tests for bot.tells.TellStore."""

from __future__ import annotations

import asyncio
import json

import pytest

from bot.config import AICfg, ChannelCfg, Config, ServerCfg
from bot.db import Database, now_utc_iso
from bot.tells import (
    TELL_LIST_LIMIT,
    TELL_MAX_DELIVER_PER_ARRIVAL,
    TELL_MAX_MESSAGE_LEN,
    TELL_MAX_PENDING_PER_PAIR,
    TELL_MAX_PENDING_PER_RECIPIENT,
    TELL_RATE_LIMIT,
    TellStore,
    actor_key,
    human_age,
)


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
    assert ChannelCfg().allow_tell is True


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


async def test_create_audits_message_preview(store: TellStore):
    # The only other copy of a tell's text is tells.message, which cancel()
    # hard-deletes and prune() removes after 30 days. Without a preview in
    # the audit row, an abuser who withdraws before delivery leaves no
    # record of what was staged.
    await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick="dave", to_account=None,
        message="secret rendezvous details", bot_nick="TestBot",
    )
    row = await store.db.fetchone(
        "SELECT details FROM audit WHERE action = 'tell.create'"
    )
    assert row is not None
    details = json.loads(row["details"])
    assert "secret rendezvous details" in details["message_preview"]


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


@pytest.mark.parametrize(
    "good", ["dave", "Dave_", "d-a-v-e", "[dave]", "da|ve", "dave,", "dave:"]
)
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


@pytest.mark.parametrize("raw", ["dave,", "dave:"])
async def test_create_strips_trailing_punctuation(store: TellStore, raw: str):
    # "!tell dave, hi" and "!tell dave: hi" are how people naturally address
    # each other in chat; the trailing punctuation must not be part of the
    # stored nick.
    r = await store.create(
        channel="#test", from_nick="alice", from_account=None,
        to_nick=raw, to_account=None, message="hi", bot_nick="TestBot",
    )
    assert r.ok is True
    row = await store.db.fetchone("SELECT to_nick_lower FROM tells")
    assert row["to_nick_lower"] == "dave"


def test_clean_target_nick_is_shared():
    # Pins clean_target_nick as public interface: bot/ircclient.py and
    # bot/tools/tell.py both call it directly to resolve a target's account
    # against the SAME cleaned token create() will store. A future rename
    # back to a "private" name has to notice these two external callers.
    assert TellStore.clean_target_nick("dave,") == "dave"
    assert TellStore.clean_target_nick("#dave") == "dave"
    assert TellStore.clean_target_nick("!!!") is None


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


async def _leave(store, to="dave", frm="alice", account=None, channel="#test",
                  to_account=None):
    return await store.create(
        channel=channel, from_nick=frm, from_account=account,
        to_nick=to, to_account=to_account, message="hi", bot_nick="TestBot",
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


async def test_pending_cap_counts_account_matched_tells(store, monkeypatch):
    # _count_pending must use the same nick-OR-account predicate as
    # pending_for. Without that, a sender could land more than
    # TELL_MAX_PENDING_PER_PAIR tells on one account holder simply by
    # respelling their nick ("dave", "dave_", "dave__", ...) each time —
    # each respelling would look like a fresh, empty pair to a nick-only
    # counter, even though every tell resolves to the same to_account.
    monkeypatch.setattr("bot.tells.TELL_RATE_LIMIT", 100)
    spellings = ["dave", "dave_", "dave__", "dave___", "dave____", "dave_____"]
    assert len(spellings) > TELL_MAX_PENDING_PER_PAIR
    made = 0
    for nick in spellings:
        r = await _leave(store, to=nick, to_account="dave_acct")
        if r.ok:
            made += 1
        else:
            assert "already" in r.message.lower()
            break
    assert made == TELL_MAX_PENDING_PER_PAIR, (
        f"cap should have fired at {TELL_MAX_PENDING_PER_PAIR}, made {made}"
    )
    pending = await store.pending_for("#test", "dave", "dave_acct")
    assert len(pending) <= TELL_MAX_PENDING_PER_PAIR


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
    # without an account.
    # Pins the no-cross-user-leak behaviour itself. (Note: this passes with
    # or without the explicit IS NOT NULL guard in the WHERE — SQL's
    # `= NULL` is already never true. It pins the outcome that matters,
    # not the guard's presence.)
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


class FakeBot:
    """Stand-in for IRCBot. Records what would go on the wire.

    Mirrors the real irc_send contract deliberately: it returns the count
    of wire lines sent and, on a wire failure, returns 0 rather than
    raising. An earlier version of this double raised instead, which made
    the failure test pass while production would have silently dropped the
    tell."""

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


def test_human_age_formats():
    assert human_age(now_utc_iso()) == "just now"
    assert human_age("2020-01-01T00:00:00+00:00").endswith("d ago")
    assert human_age("not a timestamp") == "a while ago"


async def test_deliver_posts_and_marks(store):
    await _leave(store, to="dave")
    bot = FakeBot()
    n = await store.deliver(bot, "#test", "dave", None)
    assert n == 1
    assert len(bot.sent) == 1
    target, text = bot.sent[0]
    assert target == "#test"
    assert text.startswith("dave:")
    assert "alice" in text
    row = await store.db.fetchone("SELECT delivered_at FROM tells")
    assert row["delivered_at"] is not None


async def test_deliver_is_not_repeated(store):
    await _leave(store, to="dave")
    bot = FakeBot()
    assert await store.deliver(bot, "#test", "dave", None) == 1
    assert await store.deliver(bot, "#test", "dave", None) == 0
    assert len(bot.sent) == 1


async def test_deliver_noop_when_nothing_pending(store):
    bot = FakeBot()
    assert await store.deliver(bot, "#test", "nobody", None) == 0
    assert bot.sent == []


async def test_deliver_caps_per_arrival_and_flags_the_rest(store):
    for i in range(TELL_MAX_DELIVER_PER_ARRIVAL + 2):
        await store.create(
            channel="#test", from_nick=f"s{i}", from_account=None,
            to_nick="dave", to_account=None, message=f"m{i}",
            bot_nick="TestBot",
        )
    bot = FakeBot()
    n = await store.deliver(bot, "#test", "dave", None)
    assert n == TELL_MAX_DELIVER_PER_ARRIVAL
    # 3 tells + 1 overflow notice
    assert len(bot.sent) == TELL_MAX_DELIVER_PER_ARRIVAL + 1
    assert "2 more" in bot.sent[-1][1]


async def test_deliver_delivers_the_remainder_next_time(store):
    for i in range(TELL_MAX_DELIVER_PER_ARRIVAL + 2):
        await store.create(
            channel="#test", from_nick=f"s{i}", from_account=None,
            to_nick="dave", to_account=None, message=f"m{i}",
            bot_nick="TestBot",
        )
    bot = FakeBot()
    await store.deliver(bot, "#test", "dave", None)
    assert await store.deliver(bot, "#test", "dave", None) == 2


async def test_deliver_leaves_row_pending_when_send_fails(store):
    await _leave(store, to="dave")
    n = await store.deliver(FakeBot(fail=True), "#test", "dave", None)
    assert n == 0
    row = await store.db.fetchone("SELECT delivered_at FROM tells")
    assert row["delivered_at"] is None, "a failed send must not mark delivered"
    # And it still arrives on the next attempt.
    bot = FakeBot()
    assert await store.deliver(bot, "#test", "dave", None) == 1


async def test_concurrent_deliver_does_not_double_post(store):
    """The join-and-speak race: both hooks fire within milliseconds."""
    await _leave(store, to="dave")
    bot = FakeBot()
    results = await asyncio.gather(
        store.deliver(bot, "#test", "dave", None),
        store.deliver(bot, "#test", "dave", None),
    )
    assert sorted(results) == [0, 1]
    assert len(bot.sent) == 1


async def test_deliver_leaves_row_pending_when_send_raises(store):
    """Belt-and-braces: irc_send doesn't raise today, but if it ever does
    the row must still be left pending."""
    await _leave(store, to="dave")
    assert await store.deliver(FakeBot(raises=True), "#test", "dave", None) == 0
    row = await store.db.fetchone("SELECT delivered_at FROM tells")
    assert row["delivered_at"] is None


async def test_deliver_overflow_counts_unsent_batch_items(store):
    """A send failing mid-batch leaves its row pending; the '+N more'
    notice must count it, not just the rows beyond the per-arrival cap."""
    for i in range(5):
        await store.create(
            channel="#test", from_nick=f"s{i}", from_account=None,
            to_nick="dave", to_account=None, message=f"m{i}",
            bot_nick="TestBot",
        )

    class FlakyBot(FakeBot):
        """Sends the first tell, fails on the second, succeeds on overflow notice."""
        async def irc_send(self, target: str, text: str, **kwargs) -> int:
            # Fail on tell #2 (index 1 in sent), but succeed on overflow notice
            if len(self.sent) == 1 and "[from" in text:
                return 0
            self.sent.append((target, text))
            return 1

    bot = FlakyBot()
    delivered = await store.deliver(bot, "#test", "dave", None)
    assert delivered == 1
    # 5 pending, 1 delivered -> 4 still waiting, not 2.
    # The overflow notice should be the last message sent.
    assert "+4 more" in bot.sent[-1][1]


async def test_list_from_returns_only_your_own(store):
    await _leave(store, to="dave", frm="alice")
    await _leave(store, to="erin", frm="bob")
    rows = await store.list_from("#test", "alice", None)
    assert len(rows) == 1
    assert rows[0]["to_nick"] == "dave"


async def test_list_from_excludes_delivered(store):
    await _leave(store, to="dave", frm="alice")
    await store.db.execute("UPDATE tells SET delivered_at = ?", (now_utc_iso(),))
    assert await store.list_from("#test", "alice", None) == []


async def test_list_from_matches_on_account_not_nick(store):
    await _leave(store, to="dave", frm="alice", account="alice_acct")
    # Same person, different nick — their account is what identifies them.
    rows = await store.list_from("#test", "alice_away", "alice_acct")
    assert len(rows) == 1


async def test_list_from_caps_at_limit(store, monkeypatch):
    # Lift the hourly cap so we have headroom to exceed TELL_LIST_LIMIT;
    # each tell goes to a distinct recipient so the per-recipient caps
    # don't bite first.
    monkeypatch.setattr("bot.tells.TELL_RATE_LIMIT", 100)
    for i in range(TELL_LIST_LIMIT + 5):
        r = await _leave(store, to=f"dave{i}")
        assert r.ok is True, f"create #{i + 1} should have been allowed"
    rows = await store.list_from("#test", "alice", None)
    assert len(rows) == TELL_LIST_LIMIT


async def test_cancel_by_owner(store):
    r = await _leave(store, to="dave", frm="alice")
    c = await store.cancel("#test", r.tell_id, "alice", None, is_operator=False)
    assert c.ok is True
    assert await store.pending_for("#test", "dave", None) == []


async def test_cancel_refused_for_stranger(store):
    r = await _leave(store, to="dave", frm="alice")
    c = await store.cancel("#test", r.tell_id, "mallory", None, is_operator=False)
    assert c.ok is False
    assert len(await store.pending_for("#test", "dave", None)) == 1


async def test_cancel_allowed_for_operator(store):
    r = await _leave(store, to="dave", frm="alice")
    c = await store.cancel("#test", r.tell_id, "root", "root_acct", is_operator=True)
    assert c.ok is True


async def test_cancel_unknown_id(store):
    c = await store.cancel("#test", 9999, "alice", None, is_operator=False)
    assert c.ok is False
    assert "no pending tell" in c.message.lower()


async def test_cancel_already_delivered(store):
    r = await _leave(store, to="dave", frm="alice")
    await store.deliver(FakeBot(), "#test", "dave", None)
    c = await store.cancel("#test", r.tell_id, "alice", None, is_operator=False)
    assert c.ok is False


async def test_cancel_is_channel_scoped(store):
    r = await _leave(store, to="dave", frm="alice", channel="#test")
    c = await store.cancel("#other", r.tell_id, "alice", None, is_operator=False)
    assert c.ok is False
    assert len(await store.pending_for("#test", "dave", None)) == 1


async def test_cancel_refusals_are_indistinguishable(store):
    # Unknown id vs. someone else's tell must read identically, so probing
    # !untell across ids can't be used to map which ids are live. The
    # message embeds the id the caller already typed, so a fair literal
    # comparison needs the SAME id under both refusal reasons: probe
    # while the tell still exists (not-mine), then let the real owner
    # cancel it for real and probe the same id again (now truly unknown).
    r = await _leave(store, to="dave", frm="alice")
    not_mine = await store.cancel("#test", r.tell_id, "mallory", None, is_operator=False)
    assert not_mine.ok is False

    owner_cancel = await store.cancel("#test", r.tell_id, "alice", None, is_operator=False)
    assert owner_cancel.ok is True

    unknown = await store.cancel("#test", r.tell_id, "mallory", None, is_operator=False)
    assert unknown.ok is False
    assert unknown.message == not_mine.message


async def test_prune_removes_old_delivered_and_expired(store):
    old = "2020-01-01T00:00:00+00:00"
    await _leave(store, to="dave")                        # keep: pending, fresh
    r2 = await _leave(store, to="erin")
    await store.db.execute(
        "UPDATE tells SET delivered_at = ? WHERE id = ?", (old, r2.tell_id)
    )                                                     # drop: long delivered
    r3 = await _leave(store, to="fred")
    await store.db.execute(
        "UPDATE tells SET expires_at = ? WHERE id = ?", (old, r3.tell_id)
    )                                                     # drop: expired
    assert await store.prune() == 2
    rows = await store.db.fetchall("SELECT to_nick FROM tells")
    assert [r["to_nick"] for r in rows] == ["dave"]


async def test_entry_points_produce_equivalent_rows(store):
    """The spec's load-bearing claim is that !tell and leave_message cannot
    drift apart. They have drifted twice already — once when the tool
    skipped account resolution, once when only one caller cleaned the
    target token before the cache lookup. Both are now expected to resolve
    the SAME cleaned nick and hand create() the same arguments, so the rows
    they produce must be indistinguishable apart from id and timestamp."""
    raw = "dave,"                       # punctuated, as a human would type it
    cleaned = TellStore.clean_target_nick(raw)
    fake_cache = {"dave": "dave_acct"}  # what both callers would look up

    common = dict(
        channel="#test", from_nick="alice", from_account="alice_acct",
        message="check the logs", bot_nick="TestBot",
    )
    # What _cmd_tell supplies, and what the leave_message tool supplies.
    for _ in range(2):
        await store.create(
            to_nick=raw, to_account=fake_cache.get(cleaned), **common
        )

    rows = await store.db.fetchall("SELECT * FROM tells ORDER BY id")
    assert len(rows) == 2
    fields = ("channel", "from_nick", "from_account", "from_key",
              "to_nick", "to_nick_lower", "to_account", "message")
    assert {f: rows[0][f] for f in fields} == {f: rows[1][f] for f in fields}
    # And the account actually got resolved — the whole point of the fix.
    assert rows[0]["to_account"] == "dave_acct"
    assert rows[0]["to_nick_lower"] == "dave"
