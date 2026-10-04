"""FakeProvider 契约行为的直接测试。"""

from minitau_agent.message import AgentMessage, AssistantMessage, UserMessage
from minitau_ai.fake import FakeProvider
from pi_event_helpers import assistant_done, assistant_start, text_delta


async def test_replays_scripted_events_in_order():
    """脚本流按序回放，事件类型序列与剧本一致。"""
    provider = FakeProvider(
        [[assistant_start(), text_delta("你好"), assistant_done(AssistantMessage(content="你好"))]]
    )
    events = [
        event
        async for event in provider.stream_response(
            model="m", system="s", messages=[], tools=[]
        )
    ]
    assert [e.type for e in events] == ["start", "text_delta", "done"]


async def test_calls_snapshot_freezes_messages_at_call_time():
    """calls 记录的是调用时刻的列表结构，事后 append 不影响快照。"""
    provider = FakeProvider([[assistant_start()]])
    messages: list[AgentMessage] = []

    source = provider.stream_response(model="m", system="s", messages=messages, tools=[])
    assert provider.calls[0][2] == []

    messages.append(UserMessage(content="hi"))
    _ = [event async for event in source]
    assert provider.calls[0][2] == []
    assert len(provider.calls) == 1


async def test_streams_are_popped_in_order():
    """多次调用依次弹出多段剧本。"""
    provider = FakeProvider(
        [
            [assistant_start(), assistant_done(AssistantMessage(content="one"))],
            [assistant_start(), assistant_done(AssistantMessage(content="two"))],
        ]
    )
    first = [
        e async for e in provider.stream_response(model="m", system="s", messages=[], tools=[])
    ]
    second = [
        e async for e in provider.stream_response(model="m", system="s", messages=[], tools=[])
    ]
    assert first[-1].message.text == "one"
    assert second[-1].message.text == "two"


async def test_exhausted_streams_yield_nothing():
    """剧本耗尽后调用返回空流，而不是报错。"""
    provider = FakeProvider([])
    events = [
        e async for e in provider.stream_response(model="m", system="s", messages=[], tools=[])
    ]
    assert events == []


class _Flippable:
    def __init__(self) -> None:
        self.cancelled = False

    def is_cancelled(self) -> bool:
        return self.cancelled


async def test_cancelled_signal_stops_mid_stream():
    """取消检查发生在每个事件 yield 之前，可中途停下。"""
    signal = _Flippable()
    provider = FakeProvider(
        [[assistant_start(), text_delta("你"), assistant_done(AssistantMessage(content="你"))]]
    )
    events: list = []
    async for event in provider.stream_response(
        model="m", system="s", messages=[], tools=[], signal=signal
    ):
        events.append(event)
        signal.cancelled = True
    assert [e.type for e in events] == ["start"]
