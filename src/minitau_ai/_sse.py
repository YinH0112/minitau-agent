"""OpenAI /chat/completions SSE 解析：行协议 -> JSON chunk -> 原始事件。

纯函数层，不碰网络：feed() 吃一行 data 载荷，吐事件列表。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any

from minitau_agent.types import JSONValue
from minitau_ai._provider_events import (
    RawStreamEnd,
    RawStreamError,
    RawTextDelta,
    RawThinkingDelta,
    RawToolCall,
)

THINKING_FIELD_NAMES = ("reasoning_content", "reasoning", "thinking")

# 服务器推送回来的每一个 chunk 反序列化为 Python 字典后，结构通常是这样的：
# {
#   "id": "chatcmpl-123",
#   "object": "chat.completion.chunk",
#   "choices": [
#     {
#       "index": 0,
#       "delta": {
#         "content": "你"
#       },
#       "finish_reason": null
#     }
#   ]
# }


def parse_sse_line(line: str) -> str | None:
    """一行 SSE -> data 载荷。非 data 行/空行返回 None。"""
    line = line.strip()
    # 去除字符串首尾的所有空白字符（包括空格、制表符 \t、回车换行符 \r\n）
    if not line or not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()
    # 专门用于安全移除前缀。
    # 比如 "data: {"text": "hi"}" 会变成 " {"text": "hi"}"
    # 由strip吃掉前导空格


def _loads_object(value: str) -> dict[str, JSONValue] | None:
    try:
        loaded = json.loads(value)
    except JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _first_choice(chunk: Mapping[str, Any]) -> Mapping[str, Any] | None:
    # 防御性地从大语言模型（如 OpenAI API）返回的流式数据块（chunk）中，
    # 提取出第一条候选响应（choices[0]） 对应的字典对象
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    # 如果choice为list且不为空
    choice = choices[0]
    return choice if isinstance(choice, Mapping) else None


def _thinking_delta(delta: Mapping[str, Any]) -> tuple[str, str] | None:
    # 多厂商大模型思考字段适配器（Polyfill / Normalizer）：通过候选字段优先级轮询 + 安全类型过滤，
    # 从不同模型规范中提取出正在流式生成的思考过程
    # 各大中转站，它们在流式返回模型的“思考过程”时，字段命名五花八门。
    # 这个函数就是为了抹平各大模型的格式差异而设计的
    for field_name in THINKING_FIELD_NAMES:
        value = delta.get(field_name)
        if isinstance(value, str) and value:
            return field_name, value
    return None


def _tool_call_deltas(delta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [tc for tc in tool_calls if isinstance(tc, Mapping)]


@dataclass
class _ToolCallBuilder:
    """按 index 聚一个工具调用的分片。arguments 是分片 JSON，只能拼接。"""

    id: str = ""
    name: str = ""
    arguments_parts: list[str] = field(default_factory=list)
    # 为什么不直接写 = []？
    # 在 Python 中，列表是可变对象（Mutable Object）。
    # 如果写成 list[str] = []，所有由这个类生成的实例都会共享同一个列表内存地址
    # 这会导致工具 A 的参数拼到工具 B 里面去。
    # Python 的 dataclass 强制要求：可变类型的默认值必须使用 field(default_factory=list)。
    # 每次实例化时，它都会调用 list() 生成一个全新、独立的空列表

    # list / dict / set / bytearray 等可变对象 必须 field(default_factory=...)
    # str / int / float / bool / None	直接写：name: str = ""
    # tuple / frozenset（不可变）	直接写：tags: tuple[str, ...] = ()

    def add_delta(self, delta: Mapping[str, Any]) -> None:
        # 如果 chunk 中带来了 id 和 name，就存下来（一般只在第一块到达）
        call_id = delta.get("id")
        if isinstance(call_id, str):
            self.id = call_id
        function = delta.get("function")
        if not isinstance(function, Mapping):
        # 如果后续某些 chunk 根本没有 function 字段，直接 return 跳过，防止后面报 AttributeError
            return
        name = function.get("name")
        if isinstance(name, str):
            self.name = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            self.arguments_parts.append(arguments)

    def build(self, index: int) -> RawToolCall:
        arguments_text = "".join(self.arguments_parts)
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            raise ValueError(f"Tool call {self.name or index} has invalid JSON arguments")
        if not self.name:
            raise ValueError(f"Tool call {index} has no function name")
        return RawToolCall(
            index=index,
            id=self.id or f"tool-call-{index}",
            name=self.name,
            arguments=arguments,
        )


class ChatStreamParser:
    """OpenAI /chat/completions SSE chunk 流 -> 原始事件流。

    状态字段（5.4 的重试信封依赖）：
    - emitted_content：是否已吐过任何模型输出（决定断流后还能不能重试）
    - fatal：是否已产出终态错误（信封不得再调 finalize）

    这个类的设计采用了非常经典的 “推入-收尾（Feed-Finalize）” 模式：

    网络接收到每一个 SSE 文本块
    1. 循环调用 parser.feed(event) ──> 边吃数据边吐出实时事件（文字增量、思考增量）
       │
       ▼ 直到收到 [DONE] 或网络关闭
    2. 调用 parser.finalize() ──────> 收尾盘点，产出拼装好的工具调用和最终统计报告
    """

    def __init__(self) -> None:
        self.emitted_content = False
        # 如果网络请求发出去，才刚握手或者什么字都没吐出来网络就断了，此时可以放心重试
        self.fatal = False
        # 如果遇到严重的非法 JSON，标记为致命错误，阻止后续非法调用 finalize
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._tool_call_builders: dict[int, _ToolCallBuilder] = {}
        self._finish_reason: str | None = None
        self._usage: dict[str, JSONValue] | None = None
        self._received_done = False
        self._saw_choice = False

    def feed(self, event: str) -> tuple[list[Any], bool]:
        """吃一条 data 载荷，返回 (事件列表, 是否停止读流)。"""
        if event == "[DONE]":
            self._received_done = True
            return [], True

        chunk = _loads_object(event)
        if chunk is None:
            self.fatal = True
            return [RawStreamError(message="Provider returned invalid JSON chunk")], True

        provider_error = chunk.get("error")
        if provider_error is not None:
            self.fatal = True
            detail = provider_error.get("message") if isinstance(provider_error, Mapping) else None
            return [RawStreamError(message=str(detail or provider_error))], True

        # usage chunk（stream_options 开启时最后一块）顶层带 usage、choices 为空
        # 开启 stream_options 时，最后一个 chunk 会包含 Token 消耗统计（usage），
        # 此时 choices 往往是空的；
        # 国产模型（如 Moonshot / Kimi）：喜欢把 usage 塞在 choices[0] 里面；
        # 这里做了双重兼容与回退，无论模型遵循哪家规范，都能安全捕获到消耗的 Token 数量
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, Mapping):
            self._usage = dict(chunk_usage)

        choice = _first_choice(chunk)
        if choice is None:
            return [], False
        self._saw_choice = True

        # 某些端点（如 Moonshot）把 usage 挂在 choice 而不是 chunk 顶层
        choice_usage = choice.get("usage")
        if not isinstance(chunk_usage, Mapping) and isinstance(choice_usage, Mapping):
            self._usage = dict(choice_usage)

        # finish_reason：最后一个非空值赢（前序 chunk 全是 null）
        reason = choice.get("finish_reason")
        if isinstance(reason, str) and reason:
            self._finish_reason = reason

        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return [], False

        events: list[Any] = []
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.emitted_content = True
            self._content_parts.append(content)
            events.append(RawTextDelta(delta=content))

        thinking = _thinking_delta(delta)
        if thinking is not None:
            field_name, text = thinking
            self.emitted_content = True
            self._thinking_parts.append(text)
            events.append(RawThinkingDelta(field=field_name, delta=text))

        for tool_call_delta in _tool_call_deltas(delta):
            self.emitted_content = True
            index = tool_call_delta.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                self.fatal = True
                error = RawStreamError(message="Provider returned an invalid tool call index")
                return [error], True
            builder = self._tool_call_builders.setdefault(index, _ToolCallBuilder())
            builder.add_delta(tool_call_delta)

        return events, False

    def finalize(self) -> list[Any]:
        """流结束（[DONE] 或连接关闭）后收尾：产出工具调用与结束事件。"""
        if not self._saw_choice:
            return [RawStreamError(message="Provider stream contained no response choice")]
        if not (self._received_done or self._finish_reason):
            return [RawStreamError(message="Provider stream ended before completion")]
        events: list[Any] = []
        for index, builder in sorted(self._tool_call_builders.items()):
            try:
                events.append(builder.build(index))
            except ValueError as exc:
                return [RawStreamError(message=str(exc))]
        events.append(RawStreamEnd(finish_reason=self._finish_reason, usage=self._usage))
        return events

# 网络推过来：data: {"choices": [{"delta": {"content": "你好"}}]}
#    │
#    ├─► strip() 与 removeprefix("data:") 清洗出纯 JSON
#    │
#    ├─► ChatStreamParser.feed() 接收
#    │     ├─► _first_choice() 拿到 choice 和 delta
#    │     ├─► 识别出 content="你好"
#    │     ├─► 标记 self.emitted_content = True（已吐字，禁止盲目重试）
#    │     └─► 实时 return [RawTextDelta("你好")] 抛给上层打字机打印！
#    │
# 网络推过来：data: [DONE]
#    │
#    ├─► ChatStreamParser.feed() 识别到 [DONE]，返回 ([], True)，通知结束
#    │
#    └─► 上层调用 ChatStreamParser.finalize()
#          ├─► _ToolCallBuilder.build() 产出完整工具调用
#          └─► 产出包含完整全文、思考链、Token 使用量的最终 "end" 汇总包！
