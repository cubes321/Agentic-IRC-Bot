"""Per-channel policy. Wraps ChannelCfg with runtime helpers."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ChannelCfg, Config


@dataclass
class ActorContext:
    """The user (or bot itself) performing an action, for authorisation checks."""
    nick: str
    account: str | None
    is_operator: bool
    is_op_in_channel: bool


class ChannelPolicy:
    def __init__(self, channel: str, cfg: ChannelCfg, global_cfg: Config):
        self.channel = channel
        self.cfg = cfg
        self.global_cfg = global_cfg

    @property
    def mode(self) -> str:
        return self.cfg.mode

    @property
    def persona(self) -> str:
        return self.cfg.persona

    @property
    def tick_interval_sec(self) -> int:
        return self.cfg.tick_interval_sec

    @property
    def auto_summarise_links(self) -> bool:
        return self.cfg.auto_summarise_links

    @property
    def auto_welcome(self) -> bool:
        return self.cfg.auto_welcome

    @property
    def auto_recall(self) -> bool:
        return self.cfg.auto_recall

    @property
    def auto_recall_k(self) -> int:
        return self.cfg.auto_recall_k

    def can_issue_tasks(self, actor: ActorContext) -> bool:
        if self.mode == "locked":
            return False
        rule = self.cfg.task_issuers
        if rule == "all":
            return True
        if rule == "ops":
            return actor.is_operator or actor.is_op_in_channel
        if rule == "operator":
            return actor.is_operator
        return False

    def allows_action(self, kind: str) -> bool:
        return kind in self.cfg.allow_actions

    def step_cap_for(self, mode: str) -> int:
        b = self.global_cfg.budgets
        return b.task_step_cap if mode == "task" else b.reply_step_cap

    def wall_cap_for(self, mode: str) -> int:
        b = self.global_cfg.budgets
        return b.task_wall_sec if mode == "task" else b.reply_wall_sec
