"""OpenAI 兼容 /chat/completions 的 Provider 适配器。

三层流水线：信封(HTTP+重试) -> ChatStreamParser -> canonicalize_provider_stream。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from json import dumps

import httpx

from minitau_agent.async_iterators import closing_async_iterator
from minitau_agent.message import AgentMessage, AssistantMessage, ToolResultMessage, UserMessage
from minitau_agent.provider import CancellationToken
from minitau_agent.provider_events import AssistantMessageEvent
from minitau_agent.tools import AgentTool
from minitau_agent.types import JSONValue
from minitau_ai._provider_events import RawStreamError
from minitau_ai._sse import ChatStreamParser, parse_sse_line
from minitau_ai.env import OpenAICompatibleConfig
from minitau_ai.http import create_async_client
from minitau_ai.http_errors import provider_http_error_message
from minitau_ai.retry import is_transient_status, retry_delay_seconds, wait_for_retry
from minitau_ai.stream import canonicalize_provider_stream


async def _wait_for_cancel(signal: CancellationToken) -> None:
    while not signal.is_cancelled():
        await asyncio.sleep(0.05)


async def _cancelled_stream(
    source: AsyncIterator[object], signal: CancellationToken
) -> AsyncIterator[object]:
    """Interrupt even while the HTTP transport is waiting for the next byte."""
    watch = asyncio.create_task(_wait_for_cancel(signal))
    next_event: asyncio.Task[object] | None = None

    async def next_from_source() -> object:
        return await anext(source)

    try:
        while True:
            if signal.is_cancelled():
                yield RawStreamError(message="Request cancelled", aborted=True)
                return
            next_event = asyncio.create_task(next_from_source())
            done, _ = await asyncio.wait({next_event, watch}, return_when=asyncio.FIRST_COMPLETED)
            if watch in done or signal.is_cancelled():
                next_event.cancel()
                await asyncio.gather(next_event, return_exceptions=True)
                yield RawStreamError(message="Request cancelled", aborted=True)
                return
            try:
                yield next_event.result()
            except StopAsyncIteration:
                return
            next_event = None
    finally:
        if next_event is not None and not next_event.done():
            next_event.cancel()
            await asyncio.gather(next_event, return_exceptions=True)
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()


# 第一部分：协议转换函数（内部领域模型 ➔ OpenAI 线上格式）
# 大模型只认 OpenAI 规定的 JSON 格式，而 minitau 内部有一套强类型的 Python 消息对象。
def _system_message(system: str) -> dict[str, JSONValue]:
    """构建 OpenAI 规范的系统提示词（System Prompt）字典。"""
    return {"role": "system", "content": system}

def _tool_call_to_openai(tool_call: object) -> dict[str, JSONValue]:
    """在 Python 内部，工具参数是以字典形式存储的（如 {"city": "Beijing"}）；
    但 OpenAI API 规定，发往后端的 arguments 必须是一个序列化后的 JSON 字符串，
    所以这里用 dumps(tool_call.arguments) 做了一次转义"""
    from minitau_agent.message import ToolCall

    assert isinstance(tool_call, ToolCall)
    # minitau 的 arguments 是 dict；OpenAI 线上格式要求 JSON 字符串——重新序列化
    return {
        "id": tool_call.id,
        "type": "function",
        "function": {"name": tool_call.name, "arguments": dumps(tool_call.arguments)},
    }
# json.dumps：Python 对象 → JSON 字符串
# json.loads：JSON 字符串 → Python 对象

def _assistant_to_openai(message: AssistantMessage) -> dict[str, JSONValue]:
    """将历史对话中 AI 助手曾经发过的消息转换为 OpenAI 格式"""
    item: dict[str, JSONValue] = {"role": "assistant", "content": message.text}
    if message.tool_calls:
        item["tool_calls"] = [_tool_call_to_openai(tc) for tc in message.tool_calls]
    return item

def _messages_to_openai_chat(messages: list[AgentMessage]) -> list[dict[str, JSONValue]]:
    """minitau 消息模型 -> OpenAI wire 格式。无图片支持（minitau 明确不做）。
    遍历上下文历史消息：
UserMessage ➔ 转成 {"role": "user", "content": ...}
AssistantMessage ➔ 转成 {"role": "assistant", ...}
ToolResultMessage（工具执行结果）➔
转成 OpenAI 规范的 {"role": "tool", "tool_call_id": ..., "name": ..., "content": ...}
    """
    converted: list[dict[str, JSONValue]] = []
    for message in messages:
        if isinstance(message, UserMessage):
            converted.append({"role": "user", "content": message.content})
        elif isinstance(message, AssistantMessage):
            converted.append(_assistant_to_openai(message))
        elif isinstance(message, ToolResultMessage):
            converted.append({
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "name": message.tool_name,
                "content": message.content or "(no tool output)",
            })
    return converted

def _tool_to_openai(tool: AgentTool) -> dict[str, JSONValue]:
    """
    把 Agent本地注册的可用工具声明，转换为大模型能看懂的 JSON Schema 格式（Function Calling 规范）
    """
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        },
    }


def _build_chat_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
) -> dict[str, JSONValue]:
    """组装最终发给 HTTP POST 请求的整个 JSON Payload 字典"""
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "messages": [
            _system_message(system),
            *_messages_to_openai_chat(messages),
        ],
        # 请求最后一块带 usage 的 chunk（5.2 解析器已支持其空 choices 形态）
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = [_tool_to_openai(tool) for tool in tools]
    return payload


# 第二部分：OpenAICompatibleProvider 类的生命周期管理
class OpenAICompatibleProvider:
    """OpenAI 兼容端点的 Provider 适配器。"""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client          # 测试注入 MockTransport 的口子
        self._owns_client = client is None

    async def aclose(self) -> None:
        """关闭自建的 HTTP 客户端（注入的不归我们管）。"""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # 懒创建：走 5.1 的代理归一化工厂
            self._client = create_async_client(timeout=self._config.timeout_seconds)
        return self._client

# 第三部分：对外统一流式门面
    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        # 一次模型请求最多包含三层流：HTTP 原始事件、可中断包装、统一消息事件。
        # 在这里建立它们的归属关系，避免上层提前关闭时只停下事件转换而留下 HTTP 响应。
        raw_source = self._stream_chat_completions(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            signal=signal,
        )
        raw = _cancelled_stream(raw_source, signal) if signal is not None else raw_source
        canonical = canonicalize_provider_stream(
            raw,
            provider="openai-compatible",
            model=model,
        )

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            # 多个 async with 按相反顺序退出：先关闭 canonical，再关闭 raw。
            # raw 若是 _cancelled_stream，它自己的 finally 还会关闭 raw_source；
            # 最底层 _stream 的 HTTP async with 因而能够退出。
            async with closing_async_iterator(raw), closing_async_iterator(canonical):
                async for event in canonical:
                    yield event

        return iterator()
    def _stream_chat_completions(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None,
    ) -> AsyncIterator[object]:
        """
        URL 路由与 Payload 封装
        去掉用户配置的 base_url 末尾可能带有的斜杠 /，然后拼接 /chat/completions
        随后将组装好的 payload 和 url 移交给底层的 _stream
        """
        payload = _build_chat_payload(model=model, system=system, messages=messages, tools=tools)
        return self._stream(
            model=model,
            url=f"{self._config.base_url.rstrip('/')}/chat/completions",
            payload=payload,
            signal=signal,
        )

    def _stream(
        self,
        *,
        model: str,
        url: str,
        payload: dict[str, JSONValue],
        signal: CancellationToken | None,
    ) -> AsyncIterator[object]:
        """共享的流式 POST + 重试信封。5.1 的全部分类/退避决策在这里落地。"""

        async def iterator() -> AsyncIterator[object]:
            client = self._get_client()
            headers = {"Authorization": f"Bearer {self._config.api_key}"}
            attempt = 0 # 当前重试次数计数器
            while True:
                # 为什么每次重试都要新建一个 parser？
                # 因为如果上一次尝试在读流中途中断了，前一个 parser 的内部已经残留了半截碎片
                # 比如拼了一半的 JSON、已经收到的几个字
                # 必须彻底扔掉被污染的旧 parser，换一个崭新、干净的 parser 重新拼装
                parser = ChatStreamParser()
                try:
                    async with client.stream(
                        "POST", url, json=payload, headers=headers
                    ) as response:
                        if response.status_code >= 400:
                            body_text = (await response.aread()).decode(errors="replace")
                            if (
                                attempt < self._config.max_retries
                                and is_transient_status(response.status_code)
                            ):
                                delay = retry_delay_seconds(
                                    attempt,
                                    max_delay_seconds=self._config.max_retry_delay_seconds,
                                )
                                attempt += 1
                                if not await wait_for_retry(delay, signal=signal):
                                    return
                                continue
                            # 不可重试（或次数耗尽）：错误消息带 provider 的 detail
                            yield RawStreamError(
                                message=provider_http_error_message(
                                    provider_name="openai-compatible",
                                    status_code=response.status_code,
                                    body=body_text,
                                    model=model,
                                )
                            )
                            return

                        async for line in response.aiter_lines():
                            if signal is not None and signal.is_cancelled():
                                return
                            event = parse_sse_line(line)
                            if event is None:
                                continue
                            events, stop = parser.feed(event)
                            for parsed in events:
                                yield parsed
                            if stop:
                                break

                        if parser.fatal:
                            return
                        for parsed in parser.finalize():
                            yield parsed
                        return
                except httpx.HTTPError as exc:
                    # 已吐内容不可重试（5.1 的核心守卫）：吐过的字收不回
                    if not parser.emitted_content and attempt < self._config.max_retries:
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield RawStreamError(message=str(exc))
                    return

        return iterator()
