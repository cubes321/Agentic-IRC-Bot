"""Quakenet-aware authorisation.

Identity comes from the user's services account, not their nick. We populate
the nick -> account map from:
  - WHOIS responses (RPL_WHOISACCOUNT, numeric 330) — pulled on demand, cached
  - account-notify IRCv3 messages — pushed when supported (pydle handles it)

Operators are identified by account name from config; channel ops by live `+o`
on the channel.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .db import Database
from .policy import ActorContext

log = logging.getLogger(__name__)

CACHE_TTL_SEC = 300


@dataclass
class AuthManager:
    db: Database
    operator_accounts: set[str]
    # nick -> (account or None, fetched_at_monotonic)
    _account_cache: dict[str, tuple[str | None, float]] = field(default_factory=dict)
    # channel -> set of nicks with +o
    _channel_ops: dict[str, set[str]] = field(default_factory=dict)

    def is_operator(self, account: str | None) -> bool:
        return bool(account) and account in self.operator_accounts

    def is_op_in_channel(self, channel: str, nick: str) -> bool:
        return nick in self._channel_ops.get(channel, set())

    # ---- cache management ----

    def remember_account(self, nick: str, account: str | None) -> None:
        self._account_cache[nick] = (account, time.monotonic())

    def get_cached(self, nick: str) -> str | None | object:
        """Returns the account (str or None) if cached & fresh; sentinel _MISS otherwise."""
        entry = self._account_cache.get(nick)
        if entry is None:
            return _MISS
        account, fetched = entry
        if time.monotonic() - fetched > CACHE_TTL_SEC:
            self._account_cache.pop(nick, None)
            return _MISS
        return account

    def clear_account(self, nick: str) -> None:
        self._account_cache.pop(nick, None)

    def rename(self, old_nick: str, new_nick: str) -> None:
        if old_nick in self._account_cache:
            self._account_cache[new_nick] = self._account_cache.pop(old_nick)
        for ops in self._channel_ops.values():
            if old_nick in ops:
                ops.discard(old_nick)
                ops.add(new_nick)

    def forget_user(self, nick: str) -> None:
        self._account_cache.pop(nick, None)
        for ops in self._channel_ops.values():
            ops.discard(nick)

    # ---- channel-op tracking ----

    def set_channel_ops(self, channel: str, op_nicks: set[str]) -> None:
        self._channel_ops[channel] = set(op_nicks)

    def add_op(self, channel: str, nick: str) -> None:
        self._channel_ops.setdefault(channel, set()).add(nick)

    def remove_op(self, channel: str, nick: str) -> None:
        self._channel_ops.get(channel, set()).discard(nick)

    # ---- actor builder ----

    def actor_for(self, nick: str, channel: str) -> ActorContext:
        cached = self.get_cached(nick)
        account: str | None = None if cached is _MISS else cached  # type: ignore[assignment]
        return ActorContext(
            nick=nick,
            account=account,
            is_operator=self.is_operator(account),
            is_op_in_channel=self.is_op_in_channel(channel, nick),
        )


_MISS = object()
