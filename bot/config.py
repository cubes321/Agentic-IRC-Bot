"""Config loading and validation. TOML in, pydantic models out."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


Mode = Literal["chatty", "quiet", "worker", "locked"]
TaskIssuers = Literal["all", "ops", "operator"]
ActionKind = Literal["me", "topic", "msg"]


class QuakenetCfg(BaseModel):
    q_account: str = ""
    q_password_file: str = ""
    auto_request_x: bool = True


class ServerCfg(BaseModel):
    host: str
    port: int = 6667
    tls: bool = False
    nick: str
    realname: str = "Agentic IRC Bot"
    channels: list[str] = Field(default_factory=list)
    quakenet: QuakenetCfg = Field(default_factory=QuakenetCfg)


class AICfg(BaseModel):
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    chat_model: str
    embed_model: str = ""
    vision_model: str = ""
    vision_base_url: str = ""

    @property
    def effective_vision_base_url(self) -> str:
        return self.vision_base_url or self.base_url


class ToolsCfg(BaseModel):
    search_provider: Literal["brave", "duckduckgo", "tavily"] = "duckduckgo"
    search_fallback: Literal["brave", "duckduckgo", "tavily", ""] = ""
    brave_api_key: str = ""
    tavily_api_key: str = ""


class OperatorCfg(BaseModel):
    accounts: list[str] = Field(default_factory=list)


class BudgetsCfg(BaseModel):
    reply_step_cap: int = 6
    reply_wall_sec: int = 90        # per-turn budget; bumped for local models
    task_step_cap: int = 30
    task_wall_sec: int = 1800
    llm_call_timeout_sec: int = 90  # per-call HTTP timeout; decoupled from per-turn budget
    # Cap concurrent chat-completion calls against the chat model. LM Studio
    # accepts multiple concurrent calls but slows down per-call when it does.
    # 2 is a reasonable default: one user-facing reply can run while one
    # background task (initiative tick / extractor) waits its turn.
    max_concurrent_chat_calls: int = 2


class MemoryCfg(BaseModel):
    extractor_batch_size: int = 20  # fire extractor every N public messages per channel


class SchedulerCfg(BaseModel):
    """Scheduler timing knobs. Owns initiative ticks + reminder firing."""
    # How often to poll the reminders table for due rows.
    reminder_poll_sec: int = 10
    # Random jitter applied to per-channel tick intervals, as a fraction.
    # 0.20 means each tick fires at tick_interval_sec * (1.0 ± 0.20). Avoids
    # multiple channels' ticks lining up on the same second.
    tick_jitter_pct: float = 0.20
    # Minimum allowed tick_interval_sec across all channels. Prevents a
    # mistyped `tick_interval_sec = 5` from generating ~17K LLM calls/day.
    # Channels that want no ticks should set tick_interval_sec = 0 explicitly.
    min_tick_interval_sec: int = 60
    # Buffer-window cutoff: messages older than this are excluded from the
    # tick prompt entirely. If nothing remains after filtering, the tick is
    # skipped. Prevents the bot from chiming in on a conversation that ended
    # hours ago because the recent-buffer looks superficially active.
    # 0 disables the filter (always include all recent messages).
    tick_skip_if_idle_sec: int = 900   # 15 minutes
    # Tighter freshness gate: skip the tick if the *newest* message in the
    # buffer is older than this. Even within the buffer-window, a channel
    # whose last message is 12 minutes old is no longer a live conversation
    # — chiming in there reads as the bot talking to itself. With the
    # default tick interval of 90s, 300s = "give up after about 3 missed
    # ticks worth of silence". Set to 0 to disable.
    tick_skip_if_last_message_older_than_sec: int = 300   # 5 minutes


class StorageCfg(BaseModel):
    db_path: str = "bot.sqlite"
    log_path: str = "bot.log"       # debug log file (INFO+ to console, DEBUG+ to file)


class ShutdownCfg(BaseModel):
    """Graceful-shutdown knobs. Used by !quit, SIGINT, SIGTERM, KeyboardInterrupt."""
    # Goodbye line sent as the IRC QUIT message. Channels see this in the
    # part notice, so make it informative ("scheduled restart", "out of
    # memory", "shutting down for the night"). Operators can override per
    # invocation via `!quit <message>`.
    quit_message: str = "shutting down"
    # Max seconds to wait for in-flight reply turns to finish before forcing
    # the disconnect. Set high enough to cover a slow LLM turn (default 30s
    # comfortably covers a 90s wall budget IF the turn already started its
    # final summarisation). Set to 0 to skip waiting (rude — interrupts mid-turn).
    grace_sec: int = 30
    # In-channel goodbye message: before disconnecting, the bot can post a
    # short in-character farewell in each channel that's had recent activity.
    # Costs one chat-completion call per eligible channel (parallel, capped
    # by max_concurrent_chat_calls). Set goodbye_recent_sec=0 to disable
    # the feature entirely (saves the LLM calls; channels still see the
    # IRC QUIT message). Default 900s mirrors the tick-freshness window —
    # channels that wouldn't get an initiative tick don't get a goodbye.
    goodbye_recent_sec: int = 900
    # Combined wall-clock budget for all goodbye LLM calls. Channels that
    # don't complete within this window get skipped (their goodbye is
    # cancelled). Keeps shutdown from hanging on a slow LLM. Set 0 to give
    # them an unbounded amount of time (not recommended).
    goodbye_timeout_sec: int = 30


class IgnoreCfg(BaseModel):
    nicks: list[str] = Field(default_factory=lambda: ["Q", "ChanServ", "NickServ"])


class ChannelCfg(BaseModel):
    mode: Mode = "quiet"
    persona: str = "a helpful, terse IRC chatbot."
    task_issuers: TaskIssuers = "ops"
    allow_actions: list[ActionKind] = Field(default_factory=list)
    tick_interval_sec: int = 0
    auto_summarise_links: bool = False
    auto_welcome: bool = False
    # Auto-recall: when true, every reply turn embeds the user's message,
    # pulls top-K relevant memories, and prepends them to the system context
    # before the agent loop runs. The model never has to "decide" to call
    # recall — it always sees relevant memories. Costs one embedding call
    # per reply turn (~100ms on CPU). Default off; opt-in per channel.
    auto_recall: bool = False
    auto_recall_k: int = 3

    @field_validator("tick_interval_sec")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("tick_interval_sec must be >= 0")
        return v


class Config(BaseModel):
    server: ServerCfg
    ai: AICfg
    tools: ToolsCfg = Field(default_factory=ToolsCfg)
    operator: OperatorCfg = Field(default_factory=OperatorCfg)
    budgets: BudgetsCfg = Field(default_factory=BudgetsCfg)
    memory: MemoryCfg = Field(default_factory=MemoryCfg)
    scheduler: SchedulerCfg = Field(default_factory=SchedulerCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    shutdown: ShutdownCfg = Field(default_factory=ShutdownCfg)
    ignore: IgnoreCfg = Field(default_factory=IgnoreCfg)
    channels: dict[str, ChannelCfg] = Field(default_factory=dict)

    def channel(self, name: str) -> ChannelCfg:
        """Return per-channel config, falling back to a quiet default."""
        return self.channels.get(name) or ChannelCfg()


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)


def load_q_password(cfg: Config) -> str | None:
    """Read the Q password from disk if configured. Held in memory only."""
    qn = cfg.server.quakenet
    if not qn.q_account or not qn.q_password_file:
        return None
    return Path(qn.q_password_file).read_text().strip()
