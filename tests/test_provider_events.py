"""provider_events 数据协议的测试。"""




import pytest
from pydantic import TypeAdapter

from minitau_agent.message import AssistantMessage, TextContent, ToolCall
from minitau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)

adapter = TypeAdapter(AssistantMessageEvent)

def partial(text: str = "") -> AssistantMessage:
    """构造一个只含单个 text 块的累积快照。"""
    return AssistantMessage(content=[TextContent(text=text)] if text else [])

ECHO_CALL = ToolCall(id="call_1", name="echo", arguments={"text": "你好"})

ALL_EVENTS = [
    AssistantStartEvent(partial=partial()),
    TextStartEvent(content_index=0, partial=partial()),
    TextDeltaEvent(content_index=0, delta="你", partial=partial("你")),
    TextEndEvent(content_index=0, content="你好", partial=partial("你好")),
    ToolCallStartEvent(content_index=1, partial=partial("你好")),
    ToolCallDeltaEvent(content_index=1, delta='{"text":', partial=partial("你好")),
    ToolCallEndEvent(content_index=1, tool_call=ECHO_CALL, partial=partial("你好")),
    AssistantDoneEvent(reason="toolUse", message=partial("你好")),
    AssistantErrorEvent(
        reason="error",
        error=AssistantMessage(stop_reason="error", error_message="provider 超时"),
    ),
]

@pytest.mark.parametrize("event", ALL_EVENTS, ids=lambda e: e.type)
def test_event_round_trips_through_union(event):
    """每种事件都能通过判别联合无损往返。"""
    parsed = adapter.validate_json(event.model_dump_json())
    assert type(parsed) is type(event)
    assert parsed == event

def test_wire_format_mixes_snake_discriminator_and_camel_fields():
    """判别值保持 snake_case，字段名转驼峰——锁死混合线格式。"""
    event = TextDeltaEvent(content_index=0, delta="hi", partial=partial("hi"))
    payload = event.model_dump_json()
    assert '"type":"text_delta"' in payload
    assert '"contentIndex":0' in payload

def test_tool_call_discriminators_are_single_word():
    """tau 源码用的是 toolcall（一个词），不是 tool_call。"""
    payload = ToolCallStartEvent(content_index=0, partial=partial()).model_dump_json()
    assert '"type":"toolcall_start"' in payload

def test_error_event_carries_assistant_message():
    """错误事件携带的也是一条 AssistantMessage（stop_reason + error_message）。"""
    event = AssistantErrorEvent(
        reason="aborted",
        error=AssistantMessage(stop_reason="aborted", error_message="用户取消"),
    )
    parsed = adapter.validate_json(event.model_dump_json())
    assert parsed.error.stop_reason == "aborted"
    assert parsed.error.error_message == "用户取消"