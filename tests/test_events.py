"""AgentEvent 数据协议的测试。"""



import pytest
from pydantic import TypeAdapter

from minitau_agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from minitau_agent.message import (
    AssistantMessage,
    TextContent,
    ToolResultMessage,
    UserMessage,
)
from minitau_agent.provider_events import TextDeltaEvent
from minitau_agent.tools import AgentToolResult

adapter = TypeAdapter(AgentEvent)

assistant = AssistantMessage(content=[TextContent(text="你好")])
tool_result = ToolResultMessage(tool_call_id="c1", tool_name="echo", content="ok")

ALL_EVENTS = [
    AgentStartEvent(),
    AgentEndEvent(messages=[UserMessage(content="hi"), assistant]),
    TurnStartEvent(),
    TurnEndEvent(message=assistant, tool_results=[tool_result]),
    MessageStartEvent(message=UserMessage(content="hi")),
    MessageUpdateEvent(
        message=assistant,
        assistant_message_event=TextDeltaEvent(
            content_index=0, delta="你", partial=assistant
        ),
    ),
    MessageEndEvent(message=assistant),
    ToolExecutionStartEvent(tool_call_id="c1", tool_name="echo", args={"text": "hi"}),
    ToolExecutionUpdateEvent(
        tool_call_id="c1", tool_name="echo", args={}, partial_result=AgentToolResult(content="ok")
    ),
    ToolExecutionEndEvent(
        tool_call_id="c1", tool_name="echo", result=AgentToolResult(content="ok"), is_error=False
    ),
]

@pytest.mark.parametrize("event", ALL_EVENTS, ids=lambda e: e.type)
def test_event_round_trips_through_union(event):
    """全部十种事件都能通过判别联合无损往返。"""
    parsed = adapter.validate_json(event.model_dump_json())
    assert type(parsed) is type(event)
    assert parsed == event

def test_wire_format_mixes_snake_discriminator_and_camel_fields():
    """判别值 snake_case、字段名 camelCase——锁死混合线格式。"""
    event = ToolExecutionStartEvent(tool_call_id="c1", tool_name="echo", args={})
    payload = event.model_dump_json()
    assert '"type":"tool_execution_start"' in payload
    assert '"toolCallId":"c1"' in payload

def test_message_update_nests_provider_event_union():
    """AgentEvent 联合内部嵌套 provider 事件联合，序列化不丢信息。"""
    inner = TextDeltaEvent(content_index=0, delta="你", partial=assistant)
    event = MessageUpdateEvent(message=assistant, assistant_message_event=inner)
    parsed = adapter.validate_json(event.model_dump_json())
    assert parsed.assistant_message_event.delta == "你"