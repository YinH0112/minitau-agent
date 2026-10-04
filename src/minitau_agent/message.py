from __future__ import annotations

from time import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from minitau_agent.types import JSONValue


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])

def current_timestamp_ms() -> int:
    """实现一个毫秒级的时间戳"""
    return int(round(time() * 1000)) # round四舍五入

class WireModel(BaseModel):
    """ 统一规则"""

    model_config = ConfigDict(
        extra="forbid",  # 拒绝外来字段
        validate_by_name=True,  # 允许按变量名校验
        validate_by_alias=True,  # 允许按别名校验
        serialize_by_alias=True,  # 序列化时强制使用别名
        alias_generator=_to_camel, # # 自动生成驼峰别名
    )

class TextContent(WireModel):
    type: Literal["text"] = "text"
    text: str

class ToolCall(WireModel):
    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, JSONValue] = Field(default_factory=dict)

class UserMessage(WireModel):
    role: Literal["user"] = "user"
    content: str
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @property
    def text(self) -> str:
        return self.content

class AssistantMessage(WireModel):
    role: Literal["assistant"] = "assistant"
    content: list[TextContent | ToolCall] = Field(default_factory=list)
    provider: str = "unknown"
    model: str = "unknown"
    stop_reason: Literal[
        "stop",
        "length",
        "toolUse",
        "error",
        "aborted",
    ] = "stop"
    error_message: str | None = None
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_convenient_content(cls, value: object) -> object:
        """
        Accept a plain string as a Python construction convenience.

        The stored model and serialized protocol are always block based.
        """
        if not isinstance(value, dict):
            # 如果用户传入的不是字典（例如传了一个已经构造好的模型实例，或者非法的字符串），
            # 验证器不做任何处理，直接返回原值，让 Pydantic 后续的验证流程去抛出合适的类型错误
            return value
        data = dict(value)
        content = data.get("content") # 获取 content 字段
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        return "".join(
            block.text
            for block in self.content
            if isinstance(block, TextContent)
        ) # 提取所有文本块并拼接成一个字符串

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(
            block
            for block in self.content
            if isinstance(block, ToolCall)
        ) # 提取工具调用的模块

class ToolResultMessage(WireModel):
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @property
    def text(self) -> str:
        return self.content

type AgentMessage = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]