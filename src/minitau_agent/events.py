"""
provider 词汇表 → agent 词汇表
Agent 循环向外界广播的事件
**上层 Agent 业务粒度事件**，
在底层流之上，封装了 Agent 的会话生命周期、推理回合、消息、工具执行
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from minitau_agent.message import AgentMessage, ToolResultMessage, WireModel
from minitau_agent.provider_events import AssistantMessageEvent
from minitau_agent.tools import AgentToolResult
from minitau_agent.types import JSONValue


#生命周期事件
class AgentStartEvent(WireModel):
    type: Literal["agent_start"] = "agent_start"
# Agent 任务会话正式启动
class AgentEndEvent(WireModel):
    type: Literal["agent_end"] = "agent_end"
    messages: list[AgentMessage] = Field(default_factory=list)

class TurnStartEvent(WireModel):
    type: Literal["turn_start"] = "turn_start"
# `turn_start` → 模型生成消息（流式）→ 调用工具 → 工具返回结果 → `turn_end`
class TurnEndEvent(WireModel):
    type: Literal["turn_end"] = "turn_end"
    message: AgentMessage
    tool_results: list[ToolResultMessage] = Field(default_factory=list)

# 消息事件
class MessageStartEvent(WireModel):
    type: Literal["message_start"] = "message_start"
    message: AgentMessage
# 一条 Agent 消息开始生成
class MessageUpdateEvent(WireModel):
    type: Literal["message_update"] = "message_update"
    message: AgentMessage
    assistant_message_event: AssistantMessageEvent
# `assistant_message_event`：
# **直接嵌入一条底层模型流式事件**（可以是 `text_delta` / `toolcall_delta` / `text_end`…
class MessageEndEvent(WireModel):
    type: Literal["message_end"] = "message_end"
    message: AgentMessage

# 工具执行阶段
# 区分两个概念：
# 1. ToolCall（模型侧）：模型输出一段 JSON，想要调用工具；
# 属于 `provider_events` 里的 `toolcall_start / delta / end`。只是 “打算调用”。
# 2. ToolExecution（执行侧）：后端收到工具调用请求，
# 真正开始运行函数、等待返回结果；就是下面这一组事件。
class ToolExecutionStartEvent(WireModel):
    type: Literal["tool_execution_start"] = "tool_execution_start"
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue] = Field(default_factory=dict)

class ToolExecutionUpdateEvent(WireModel):
    type: Literal["tool_execution_update"] = "tool_execution_update"
    tool_call_id: str
    tool_name: str
    args: dict[str, JSONValue] = Field(default_factory=dict)
    partial_result: AgentToolResult

class ToolExecutionEndEvent(WireModel):
    type: Literal["tool_execution_end"] = "tool_execution_end"
    tool_call_id: str
    tool_name: str
    result: AgentToolResult
    is_error: bool

# 上下文压缩阶段
type CompactionReason = Literal["auto", "manual"]

class CompactionStartEvent(WireModel):
    type: Literal["compaction_start"] = "compaction_start"
    reason: CompactionReason

class CompactionEndEvent(WireModel):
    type: Literal["compaction_end"] = "compaction_end"
    reason: CompactionReason
    aborted: bool = False
    skipped: bool = False # skipped=True 表示根本没有可压缩的历史
    error_message: str | None = None

type AgentEvent = Annotated[
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
    | CompactionEndEvent
    | CompactionStartEvent,
    Field(discriminator="type"),
]