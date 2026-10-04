"""Provider 层原始流事件：openai chunk 解析的产物，5.3 再翻译成规范事件。

与 minitau_agent/provider_events.py 的区别：
- 那边是 canonical 事件（带 partial 快照，agent loop 消费）
- 这边是 raw 事件（最小载荷，只描述"线上发生了什么"）
"""

from __future__ import annotations

from dataclasses import dataclass

from minitau_agent.types import JSONValue


@dataclass(frozen=True, slots=True)
class RawTextDelta:
    delta: str


@dataclass(frozen=True, slots=True)
class RawThinkingDelta:
    """推理模型的思考增量。各家字段名不一，field 记录原始字段名。"""

    field: str  # "reasoning_content"（DeepSeek）/ "reasoning"（OpenRouter）/ "thinking"
    delta: str


@dataclass(frozen=True, slots=True)
class RawToolCall:
    index: int
    id: str
    name: str
    arguments: dict[str, JSONValue]



@dataclass(frozen=True, slots=True)
class RawStreamEnd:
    """流正常终结（[DONE] 或连接关闭后的收尾）。"""

    finish_reason: str | None
    usage: dict[str, JSONValue] | None = None


@dataclass(frozen=True, slots=True)
class RawStreamError:
    """解析层致命错误（坏 JSON chunk 等）。产此事件即流终止。"""

    message: str
    aborted: bool = False
