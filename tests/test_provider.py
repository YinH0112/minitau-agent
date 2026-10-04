"""ModelProvider 协议契约的测试。"""

import pytest

from minitau_agent.message import AssistantMessage, TextContent
from minitau_agent.provider import ModelProvider
from minitau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantStartEvent,
)
from minitau_ai.fake import FakeProvider


class _StreamFake:
    """最小实现：只为了验证协议的调用语义。"""

    def stream_response(self, *, model, system, messages, tools, signal=None):
        async def _stream():
            partial = AssistantMessage(model=model, content=[TextContent(text="hi")])
            yield AssistantStartEvent(partial=partial)
            yield AssistantDoneEvent(reason="stop", message=partial)

        return _stream()


async def test_stream_response_yields_iterator_without_await():
    """协议契约：调用即得异步迭代器，直接 async for，绝不先 await。"""
    provider = _StreamFake()
    source = provider.stream_response(
        model="fake-model", system="sys", messages=[], tools=[]
    )
    events = [event async for event in source]
    assert isinstance(events[0], AssistantStartEvent)
    assert isinstance(events[1], AssistantDoneEvent)
    assert events[1].message.text == "hi"


def test_stream_response_arguments_are_keyword_only():
    """五个参数全部关键字传参——位置传参直接 TypeError。"""
    provider = _StreamFake()
    with pytest.raises(TypeError):
        provider.stream_response("fake-model")


def test_fake_provider_satisfies_model_provider_protocol():
    """FakeProvider 结构化满足 ModelProvider 协议（mypy 静态验证的锚点）。"""
    provider: ModelProvider = FakeProvider([])
    assert callable(provider.stream_response)
