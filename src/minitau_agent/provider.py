from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from minitau_agent.cancellation import CancellationToken as CancellationToken
from minitau_agent.message import AgentMessage
from minitau_agent.provider_events import AssistantMessageEvent
from minitau_agent.tools import AgentTool


class ModelProvider(Protocol):
    """ "Provider-neutral Pi-compatible model stream interface."""
    async def aclose(self) -> None:
        """Release resources owned by the provider."""
        ...

    def stream_response(
            self,
            *,
            model: str,
            system: str,
            messages: list[AgentMessage],
            tools: list[AgentTool],
            signal: CancellationToken | None = None,
            ) -> AsyncIterator[AssistantMessageEvent]:
        """ Stream one model response as assistant message events."""
        ...
