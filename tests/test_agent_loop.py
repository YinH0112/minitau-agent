"""run_agent_loop 核心行为的测试。"""

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from minitau_agent.loop import run_agent_loop
from minitau_agent.message import (
    AgentMessage,
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from minitau_agent.tools import AgentTool, AgentToolResult
from minitau_agent.types import JSONValue
from minitau_ai.fake import FakeProvider
from pi_event_helpers import (
    assistant_done,
    assistant_error,
    assistant_start,
    text_delta,
    tool_call_end,
)


def echo_tool() -> AgentTool:
    async def execute(arguments: Mapping[str, JSONValue]) -> AgentToolResult:
        return AgentToolResult(content=f"echo:{arguments.get('text', '')}")

    return AgentTool(
        name="echo",
        description="Echo the text argument.",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        execute_fn=execute,
    )


ECHO_CALL = ToolCall(id="c1", name="echo", arguments={"text": "hi"})


class _Cancelled:
    def is_cancelled(self) -> bool:
        return True


async def test_single_turn_event_sequence():
    """最简单的一轮：用户提问、助手流式回答、循环收尾。"""
    provider = FakeProvider(
        [
            [
                assistant_start(),
                text_delta("你"),
                text_delta("好"),
                assistant_done(AssistantMessage(content="你好")),
            ]
        ]
    )
    messages: list[AgentMessage] = []

    events = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[],
            prompts=[UserMessage(content="hi")],
        )
    ]

    assert [e.type for e in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_update",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    assert len(messages) == 2
    assert isinstance(messages[0], UserMessage)
    assert isinstance(messages[1], AssistantMessage)
    assert messages[1].text == "你好"
    assert provider.calls[0][2] == [messages[0]]


async def test_tool_call_round_trip():
    """带工具调用的一轮：toolUse → 执行 → ToolResult 入史 → 第二次 provider 调用。"""
    provider = FakeProvider(
        [
            [
                assistant_start(),
                tool_call_end(ECHO_CALL),
                assistant_done(AssistantMessage(content=[ECHO_CALL])),
            ],
            [assistant_start(), assistant_done(AssistantMessage(content="done"))],
        ]
    )
    messages: list[AgentMessage] = []

    events = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[echo_tool()],
            prompts=[UserMessage(content="run")],
        )
    ]

    assert [type(m).__name__ for m in messages] == [
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
        "AssistantMessage",
    ]
    tool_result = messages[2]
    assert tool_result.tool_call_id == "c1"
    assert tool_result.content == "echo:hi"
    assert tool_result.is_error is False

    types = [e.type for e in events]
    assert "tool_execution_start" in types
    assert "tool_execution_end" in types

    turn_ends = [e for e in events if e.type == "turn_end"]
    assert len(turn_ends) == 2
    assert len(turn_ends[0].tool_results) == 1
    assert len(turn_ends[1].tool_results) == 0

    assert len(provider.calls[0][2]) == 1
    assert len(provider.calls[1][2]) == 3


async def test_outer_task_cancellation_cancels_and_awaits_running_tool():
    """外层 loop task 取消时，工具子任务也要收到取消并完成清理。"""
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()
    call = ToolCall(id="cancel-tool-1", name="blocking", arguments={})

    async def blocking_execute(
        _arguments: Mapping[str, JSONValue],
    ) -> AgentToolResult:
        tool_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            tool_cancelled.set()
        return AgentToolResult(content="unreachable")

    blocking_tool = AgentTool(
        name="blocking",
        description="Waits until its task is cancelled.",
        parameters={"type": "object", "properties": {}},
        execute_fn=blocking_execute,
    )
    provider = FakeProvider(
        [
            [
                assistant_start(),
                tool_call_end(call),
                assistant_done(AssistantMessage(content=[call])),
            ]
        ]
    )
    messages: list[AgentMessage] = []

    async def consume_loop() -> None:
        async for _event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[blocking_tool],
            prompts=[UserMessage(content="run the blocking tool")],
        ):
            pass

    task = asyncio.create_task(consume_loop())
    await tool_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tool_cancelled.is_set()


async def test_provider_context_filters_empty_error_turns():
    """空的失败轮次留在 history 里，但不进下一次 provider 请求。"""
    stale = AssistantMessage(stop_reason="error", error_message="old failure")
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="ok"))]])
    messages: list[AgentMessage] = [UserMessage(content="hi"), stale]

    _ = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[],
        )
    ]

    assert provider.calls[0][2] == [messages[0]]
    assert stale in messages


async def test_error_event_ends_loop_with_error_message():
    """provider 报错：错误被建模为消息入史，循环立即收尾。"""
    provider = FakeProvider([[assistant_start(), assistant_error("provider exploded")]])
    messages: list[AgentMessage] = []

    events = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[],
            prompts=[UserMessage(content="go")],
        )
    ]

    assert len(provider.calls) == 1
    failure = messages[-1]
    assert isinstance(failure, AssistantMessage)
    assert failure.stop_reason == "error"
    assert failure.error_message == "provider exploded"
    assert events[-1].type == "agent_end"
    turn_end = next(e for e in events if e.type == "turn_end")
    assert turn_end.message is failure


async def test_steering_messages_reach_provider_context():
    """steering 消息在下一轮开始前入史，随上下文一起送给 provider。"""
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="ok"))]])
    steer = UserMessage(content="change of plans")
    polled = False

    def get_steering() -> Sequence[AgentMessage]:
        nonlocal polled
        if not polled:
            polled = True
            return [steer]
        return ()

    messages: list[AgentMessage] = []

    _ = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[],
            prompts=[UserMessage(content="go")],
            get_steering_messages=get_steering,
        )
    ]

    assert messages[1] == steer
    assert provider.calls[0][2] == messages[:2]


async def test_follow_up_messages_continue_conversation():
    """一轮自然结束后，follow-up 消息驱动第二轮 provider 调用。"""
    provider = FakeProvider(
        [
            [assistant_start(), assistant_done(AssistantMessage(content="one"))],
            [assistant_start(), assistant_done(AssistantMessage(content="two"))],
        ]
    )
    queue: list[AgentMessage] = [UserMessage(content="and then?")]

    def get_follow_ups() -> Sequence[AgentMessage]:
        if queue:
            return [queue.pop(0)]
        return ()

    messages: list[AgentMessage] = []

    _ = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[],
            prompts=[UserMessage(content="go")],
            get_follow_up_messages=get_follow_ups,
        )
    ]

    assert len(provider.calls) == 2
    assert [m.text for m in messages] == ["go", "one", "and then?", "two"]


async def test_max_turns_stops_with_error_message():
    """超过 max_turns：循环以一条错误 AssistantMessage 收尾。"""
    provider = FakeProvider(
        [
            [
                assistant_start(),
                tool_call_end(ECHO_CALL),
                assistant_done(AssistantMessage(content=[ECHO_CALL])),
            ],
            [assistant_start(), assistant_done(AssistantMessage(content="never"))],
        ]
    )
    messages: list[AgentMessage] = []

    events = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[echo_tool()],
            prompts=[UserMessage(content="go")],
            max_turns=1,
        )
    ]

    assert len(provider.calls) == 1
    failure = messages[-1]
    assert isinstance(failure, AssistantMessage)
    assert failure.stop_reason == "error"
    assert "max_turns" in (failure.error_message or "")
    assert events[-1].type == "agent_end"


async def test_cancelled_signal_yields_error_message():
    """迭代前已取消：流为空，防御分支产出错误消息。"""
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="ok"))]])
    messages: list[AgentMessage] = []

    _ = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[],
            prompts=[UserMessage(content="go")],
            signal=_Cancelled(),
        )
    ]

    failure = messages[-1]
    assert isinstance(failure, AssistantMessage)
    assert failure.stop_reason == "aborted"
    assert "aborted" in (failure.error_message or "")


async def test_unknown_tool_degrades_to_error_result():
    """调用了不存在的工具：降级为 is_error=True 的 ToolResult，循环不中断。"""
    call = ToolCall(id="c9", name="missing", arguments={})
    provider = FakeProvider(
        [
            [
                assistant_start(),
                tool_call_end(call),
                assistant_done(AssistantMessage(content=[call])),
            ],
            [assistant_start(), assistant_done(AssistantMessage(content="ok"))],
        ]
    )
    messages: list[AgentMessage] = []

    _ = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="m",
            system="s",
            messages=messages,
            tools=[],  # 不注册任何工具——missing 必然 not found
            prompts=[UserMessage(content="go")],
        )
    ]

    result = messages[2]
    assert isinstance(result, ToolResultMessage)
    assert result.is_error is True
    assert "not found" in result.content
    # 工具错误不中断循环：第二次 provider 调用照常发生
    assert len(provider.calls) == 2
