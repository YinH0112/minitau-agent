"""
Provider-neutral assistant stream events.
把模型返回的流式片段，统一转换成这套自定义事件
**底层模型 Token 粒度事件**，
管文本增量、工具调用 JSON 增量（`text_delta`、`toolcall_delta`），是大模型厂商返回的原始流
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from minitau_agent.message import AssistantMessage, ToolCall, WireModel


class AssistantStartEvent(WireModel):
    type: Literal["start"] = "start"
    partial: AssistantMessage

class TextStartEvent(WireModel):
    type: Literal["text_start"] = "text_start"
    content_index: int
    partial: AssistantMessage

class TextDeltaEvent(WireModel):
    type: Literal["text_delta"] = "text_delta"
    content_index: int
    delta: str
    partial: AssistantMessage

class TextEndEvent(WireModel):
    type: Literal["text_end"] = "text_end"
    content_index: int
    content: str
    partial: AssistantMessage

# 1.type: Literal["text_end"]
#在复杂的流式通信中，客户端会收到各种类型的事件
# （如 text_delta 增量、tool_call 工具调用、error 错误等）。
# 这个字段用于快速路由，让解析器一眼就能认出“这是一条文本生成结束的信号”，从而触发对应的完结逻辑

# 2.content_index: int
#content_index 是什么？
# AssistantMessage.content 是有序块列表（text 和 toolCall 混排）。
# 模型流式输出时逐块产出：先说话（text 块，index 0），再调工具（toolCall 块，index 1），
# 可能再说一句（text 块，index 2）。
# content_index 标记当前 delta 属于第几块——多块交错时仅靠顺序无法对号入座。

# 3.Start/Delta/End 三连的分工

# 4.`partial`：到此刻为止完整快照

# 5.未来添加thinking三连


class ToolCallStartEvent(WireModel):
    type: Literal["toolcall_start"] = "toolcall_start"
    content_index: int
    partial: AssistantMessage

class ToolCallDeltaEvent(WireModel):
    type: Literal["toolcall_delta"] = "toolcall_delta"
    content_index: int
    delta: str
    partial: AssistantMessage

class ToolCallEndEvent(WireModel):
    type: Literal["toolcall_end"] = "toolcall_end"
    content_index: int
    tool_call: ToolCall
    partial: AssistantMessage

DoneReason = Literal["stop", "length", "toolUse"] # 结束事件
ErrorReason = Literal["aborted", "error"] # 错误事件

class AssistantDoneEvent(WireModel):
    type: Literal["done"] = "done"
    reason: DoneReason
    message: AssistantMessage
# - `reason`：结束原因：
#   - `stop`：模型自然停止，回复完成
#   - `length`：达到最大 token 长度被截断
#   - `toolUse`：助手生成完工具调用，需要暂停、调用外部工具
# - `message`：**最终完整版助手消息**（不再是 partial），整个流到此结束

class AssistantErrorEvent(WireModel):
    type: Literal["error"] = "error"
    reason: ErrorReason
    error: AssistantMessage

type AssistantMessageEvent = Annotated[
    AssistantStartEvent
    | TextStartEvent
    | TextDeltaEvent
    | TextEndEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | AssistantDoneEvent
    | AssistantErrorEvent,
    Field(discriminator="type"),
]
# 联合类型 所有流式事件的总入口类型。
# discriminator="type" 是 Pydantic 的判别器（多态解析）：
# 当你收到一条 JSON：
# {"type":"text_delta", "content_index": 0, "delta":"你好", "partial": {...}}
# Pydantic 会自动读取 `type` 字段，自动把 json 解析成对应的 `TextDeltaEvent` 对象