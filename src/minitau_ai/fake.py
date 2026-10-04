"""Deterministic Pi-compatible model provider for tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from minitau_agent.message import AgentMessage
from minitau_agent.provider import CancellationToken
from minitau_agent.provider_events import AssistantMessageEvent
from minitau_agent.tools import AgentTool


class FakeProvider:
    """A provider that replays predefined assistant event streams."""

    def __init__(self, streams: Iterable[Iterable[AssistantMessageEvent]]) -> None:
        self._streams = [list(stream) for stream in streams]
        self.calls: list[tuple[str, str, list[AgentMessage], list[AgentTool]]] = []

    async def aclose(self) -> None:
        return None
    # Iterable[Iterable[...]]：接受任意可迭代对象（列表、元组、生成器等），
    # 不强制要求具体类型，提高灵活性
    # self.calls 的类型注解：精确记录了四元组
    def stream_response(self,
                        *,
                        model: str,
                        system: str,
                        messages: list[AgentMessage],
                        tools: list[AgentTool],
                        signal: CancellationToken | None = None,
                        ) -> AsyncIterator[AssistantMessageEvent]:
        self.calls.append((model, system, list(messages), list(tools)))
        # 因为传入的列表可能在外部被修改，记录快照确保事后断言的准确性
        stream = self._streams.pop(0) if self._streams else []

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            for event in stream:
                if signal is not None and signal.is_cancelled():
                    return
                yield event

        return iterator()
