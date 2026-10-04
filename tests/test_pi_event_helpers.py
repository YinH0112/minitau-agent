"""pi_event_helpers 构造函数的测试。"""

from minitau_agent.message import AssistantMessage, ToolCall
from pi_event_helpers import (
    assistant_done,
    assistant_error,
    assistant_start,
    text_delta,
    tool_call_end,
)


def test_assistant_start_uses_fake_model():
    event = assistant_start()
    assert event.partial.model == "fake"
    assert event.partial.content == []


def test_text_delta_accepts_plain_string_partial():
    event = text_delta("你好")
    assert event.delta == "你好"
    assert event.partial.content[0].text == "你好"


def test_tool_call_end_carries_finalized_call():
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    event = tool_call_end(call)
    assert event.tool_call == call
    assert event.partial.tool_calls == (call,)


def test_assistant_done_infers_tool_use_reason():
    call = ToolCall(id="c1", name="echo", arguments={})
    event = assistant_done(AssistantMessage(content=[call]))
    assert event.reason == "toolUse"
    assert event.message.stop_reason == "toolUse"


def test_assistant_done_normalizes_provider_finish_reasons():
    """不同 provider 的结束原因词汇全部归一到 Pi 的三值。"""
    for raw in ("tool_calls", "tool_use", "toolUse"):
        assert assistant_done(AssistantMessage(), finish_reason=raw).reason == "toolUse"
    for raw in ("length", "max_tokens", "MAX_TOKENS", "incomplete"):
        assert assistant_done(AssistantMessage(), finish_reason=raw).reason == "length"


def test_assistant_done_defaults_to_stop():
    assert assistant_done(AssistantMessage(content="hi")).reason == "stop"


def test_assistant_done_accepts_dict():
    event = assistant_done({"content": "hi"})
    assert event.message.text == "hi"


def test_assistant_error_builds_error_message():
    event = assistant_error("boom")
    assert event.reason == "error"
    assert event.error.stop_reason == "error"
    assert event.error.error_message == "boom"
    assert event.error.content == []
