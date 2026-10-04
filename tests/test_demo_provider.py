"""P7.1：离线交互 Provider 每一轮都应能给出完整回应。"""

from __future__ import annotations

import pytest

from minitau_agent.message import UserMessage
from minitau_ai.factory import create_provider

pytest.importorskip("minitau_ai.demo", reason="等待 P7.1 DemoProvider 实现")


async def test_demo_provider_can_answer_multiple_turns():
    provider, model = create_provider("demo")
    try:
        first = [
            event
            async for event in provider.stream_response(
                model=model, system="s", messages=[UserMessage(content="一")], tools=[]
            )
        ]
        second = [
            event
            async for event in provider.stream_response(
                model=model, system="s", messages=[UserMessage(content="二")], tools=[]
            )
        ]
    finally:
        await provider.aclose()

    assert first[-1].type == "done"
    assert second[-1].type == "done"
    assert first[-1].message.text
    assert second[-1].message.text
