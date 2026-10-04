"""Append-only session entry models
append-only（只追加）是这个模块的核心设计约束。它决定了后面一切：条目一旦写入就不可修改，
所以需要唯一 id；因为不可修改，想"改"就得追加新条目并用 parent_id 指向前驱
"""

from __future__ import annotations

from time import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from minitau_agent.message import AgentMessage

# 还是会寂寞

def new_entry_id() -> str:
    """ Return a unique session entry id
    生成条目的唯一标识
    """
    return uuid4().hex

def current_timestamp() -> float:
    """ Return the current Unix timestamp"""
    return time()

class BaseSessionEntry(BaseModel):
    """ Common fields shared by all append-only session entries"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_entry_id)
    # 用 default_factory 保证每次实例化都重新调用
    parent_id: str | None = None
    timestamp: float = Field(default_factory=current_timestamp)

# 对话消息（UserMessage / AssistantMessage / ToolResultMessage）
class MessageEntry(BaseSessionEntry):
    """" A transcript message entry. """
    type: Literal["message"] = "message"
    message: AgentMessage

class CompactionEntry(BaseSessionEntry):
    """ A context summary that replaces older message entries during replay
    不删任何东西，只声明："id 为 a、b、c 的那几条，从此被我这段摘要替代。"
    重放时执行这个声明。磁盘上历史完整保留，喂给模型的却是压缩后的版本
    """
    type: Literal["compaction"] = "compaction"
    summary: str
    replaces_entry_ids: list[str] = Field(default_factory=list)

# 会话元信息（cwd、title，都可选）
class SessionInfoEntry(BaseSessionEntry):
    """ Basic session metadata entry"""
    type: Literal["session_info"] = "session_info"
    cwd: str | None = None
    title: str | None = None


class ModelChangeEntry(BaseSessionEntry):
    """当前分支生效的模型配置；凭据始终从环境读取，不进入会话日志。"""

    type: Literal["model_change"] = "model_change"
    provider_name: str = Field(min_length=1)
    model: str = Field(min_length=1)
    context_window_tokens: int | None = Field(default=None, gt=0)

class LeafEntry(BaseSessionEntry):
    """记录当前选择的分支节点；它不是发给模型的消息。"""

    type: Literal["leaf"] = "leaf"
    entry_id: str | None = None

# 联合类型 SessionEntry
type SessionEntry = Annotated[
    MessageEntry | SessionInfoEntry | ModelChangeEntry | CompactionEntry | LeafEntry,
    Field(discriminator="type"),
    # 解析这个联合类型时，读输入里的 type 字段，用它的值去查表决定该实例化哪个类
]
