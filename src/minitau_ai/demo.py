"""可反复回应的离线演示 Provider。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from minitau_agent.cancellation import CancellationToken
from minitau_agent.message import AgentMessage, AssistantMessage, TextContent, UserMessage
from minitau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
)
from minitau_agent.tools import AgentTool


class DemoProvider:
    async def aclose(self) -> None:
        # 演示实现没有 HTTP client，因此没有资源需要释放。
        return None

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        async def events() -> AsyncIterator[AssistantMessageEvent]:
            if signal is not None and signal.is_cancelled():
                return

            last_user = next(
                (
                    message.text
                    for message in reversed(messages)
                    if isinstance(message, UserMessage)
                ),
                "",
            )
            answer = AssistantMessage(
                model=model,
                content=[TextContent(text=f"Demo response to: {last_user}")],
            )

            yield AssistantStartEvent(partial=AssistantMessage(model=model))

            if signal is not None and signal.is_cancelled():
                return
            yield AssistantDoneEvent(reason="stop", message=answer)

        return events()