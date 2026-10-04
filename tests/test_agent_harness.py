"""AgentHarness 状态管理的测试。"""

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from minitau_agent.cancellation import CancellationToken
from minitau_agent.harness import AgentHarness, AgentHarnessConfig
from minitau_agent.message import (
    AgentMessage,
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from minitau_agent.provider_events import AssistantMessageEvent
from minitau_agent.tools import AgentTool, AgentToolResult
from minitau_agent.types import JSONValue
from minitau_ai.fake import FakeProvider
from pi_event_helpers import assistant_done, assistant_start, text_delta, tool_call_end


def make_harness(provider: FakeProvider, tools: list[AgentTool] | None = None) -> AgentHarness:
    return AgentHarness(
        AgentHarnessConfig(
            provider=provider,
            model="fake",
            system="test system",
            tools=tools or [],
        )
    )


def steering_tool(harness: AgentHarness) -> AgentTool:
    """执行时往 harness 塞一条 steering 消息——模拟用户运行中插话。"""

    async def execute(arguments: Mapping[str, JSONValue]) -> AgentToolResult:
        harness.steer("stop early")
        return AgentToolResult(content="steered")

    return AgentTool(
        name="steerer",
        description="Queue a steering message mid-run.",
        parameters={"type": "object", "properties": {}},
        execute_fn=execute,
    )


def follow_up_tool(harness: AgentHarness) -> AgentTool:
    async def execute(arguments: Mapping[str, JSONValue]) -> AgentToolResult:
        harness.follow_up("and then?")
        return AgentToolResult(content="queued")

    return AgentTool(
        name="queuer",
        description="Queue a follow-up message mid-run.",
        parameters={"type": "object", "properties": {}},
        execute_fn=execute,
    )


async def test_prompt_runs_loop_and_accumulates_history():
    """prompt 跑完一轮：两条消息入史，harness 持有它们。"""
    provider = FakeProvider(
        [[assistant_start(), assistant_done(AssistantMessage(content="hello"))]]
    )
    harness = make_harness(provider)

    events = [event async for event in harness.prompt("hi")]

    assert harness.is_running is False
    messages = harness.messages
    assert len(messages) == 2
    assert isinstance(messages[0], UserMessage)
    assert isinstance(messages[1], AssistantMessage)
    assert messages[1].text == "hello"
    assert events[-1].type == "agent_end"
    assert provider.calls[0][2] == [messages[0]]


async def test_continue_resumes_with_accumulated_context():
    """continue_ 续跑：第二次 provider 调用带着前两条历史——多轮记忆的证据。"""
    provider = FakeProvider(
        [
            [assistant_start(), assistant_done(AssistantMessage(content="hello"))],
            [assistant_start(), assistant_done(AssistantMessage(content="how are you"))],
        ]
    )
    harness = make_harness(provider)

    _ = [event async for event in harness.prompt("hi")]
    _ = [event async for event in harness.continue_()]

    messages = harness.messages
    assert [type(m).__name__ for m in messages] == [
        "UserMessage",
        "AssistantMessage",
        "AssistantMessage",
    ]
    assert messages[2].text == "how are you"
    assert provider.calls[1][2] == list(messages[:2])


async def test_prompt_while_running_raises():
    """重入保护：运行中再 prompt/continue_ 直接 RuntimeError，但 steer 合法。"""
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="hi"))]])
    harness = make_harness(provider)

    events = harness.prompt("go")
    assert harness.is_running is False  # 只创建流，还没有开始运行

    first = await anext(events)
    assert first.type == "agent_start"
    assert harness.is_running is True

    with pytest.raises(RuntimeError):
        harness.prompt("again")
    with pytest.raises(RuntimeError):
        harness.continue_()

    _ = [event async for event in events]
    assert harness.is_running is False


async def test_unstarted_stream_does_not_reserve_harness_or_change_history():
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="ok"))]])
    harness = make_harness(provider)

    abandoned = harness.prompt("ignored")
    assert harness.is_running is False
    assert harness.messages == ()

    _ = [event async for event in harness.prompt("active")]
    assert harness.is_running is False
    assert [message.text for message in harness.messages if isinstance(message, UserMessage)] == [
        "active"
    ]
    await abandoned.aclose()


async def test_two_unstarted_streams_cannot_run_concurrently():
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="ok"))]])
    harness = make_harness(provider)

    first = harness.prompt("first")
    second = harness.prompt("second")
    await anext(first)
    with pytest.raises(RuntimeError, match="already running"):
        await anext(second)

    _ = [event async for event in first]
    assert harness.is_running is False
    assert [message.text for message in harness.messages if isinstance(message, UserMessage)] == [
        "first"
    ]


async def test_steer_before_continue_injects_into_next_turn():
    """运行间 steer：消息在下一轮 provider 调用前入史。"""
    provider = FakeProvider(
        [
            [assistant_start(), assistant_done(AssistantMessage(content="one"))],
            [assistant_start(), assistant_done(AssistantMessage(content="two"))],
        ]
    )
    harness = make_harness(provider)

    _ = [event async for event in harness.prompt("go")]
    queued = harness.steer("change plans")
    assert queued.count == 1

    _ = [event async for event in harness.continue_()]

    messages = harness.messages
    steer_msg = messages[2]
    assert isinstance(steer_msg, UserMessage)
    assert steer_msg.text == "change plans"
    assert provider.calls[1][2] == list(messages[:3])


async def test_steer_mid_run_reaches_next_turn():
    """运行中 steer（由工具触发）：轮末拉取，下一轮注入。"""
    call = ToolCall(id="c1", name="steerer", arguments={})
    provider = FakeProvider(
        [
            [
                assistant_start(),
                tool_call_end(call),
                assistant_done(AssistantMessage(content=[call])),
            ],
            [assistant_start(), assistant_done(AssistantMessage(content="done"))],
        ]
    )
    harness = make_harness(provider)
    harness.config.tools.append(steering_tool(harness))

    _ = [event async for event in harness.prompt("go")]

    messages = harness.messages
    assert [type(m).__name__ for m in messages] == [
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
        "UserMessage",
        "AssistantMessage",
    ]
    assert messages[3].text == "stop early"
    assert provider.calls[1][2] == list(messages[:4])


async def test_follow_up_mid_run_drives_next_cycle():
    """follow_up（由工具触发）：内层循环自然退出后，外层拉取续跑。"""
    call = ToolCall(id="c1", name="queuer", arguments={})
    provider = FakeProvider(
        [
            [
                assistant_start(),
                tool_call_end(call),
                assistant_done(AssistantMessage(content=[call])),
            ],
            [assistant_start(), assistant_done(AssistantMessage(content="continued"))],
            [assistant_start(), assistant_done(AssistantMessage(content="final"))],
        ]
    )
    harness = make_harness(provider)
    harness.config.tools.append(follow_up_tool(harness))

    _ = [event async for event in harness.prompt("go")]

    messages = harness.messages
    assert [type(m).__name__ for m in messages] == [
        "UserMessage",
        "AssistantMessage",
        "ToolResultMessage",
        "AssistantMessage",
        "UserMessage",
        "AssistantMessage",
    ]
    assert messages[4].text == "and then?"
    assert len(provider.calls) == 3


async def test_continue_heals_dangling_tool_calls():
    """中断留下的 ToolCall：continue_ 入口补一条 error ToolResult。"""
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="ok"))]])
    harness = make_harness(provider)
    dangling = ToolCall(id="c7", name="echo", arguments={})
    harness.append_message(UserMessage(content="go"))
    harness.append_message(AssistantMessage(content=[dangling]))

    events = harness.continue_()
    assert harness.is_running is False
    assert not any(isinstance(message, ToolResultMessage) for message in harness.messages)

    first = await anext(events)
    assert first.type == "agent_start"
    assert harness.is_running is True
    healed = [m for m in harness.messages if isinstance(m, ToolResultMessage)]
    assert len(healed) == 1
    assert healed[0].tool_call_id == "c7"
    assert healed[0].is_error is True
    assert "interrupted" in healed[0].content

    _ = [event async for event in events]
    assert provider.calls[0][2] == list(harness.messages[:3])


async def test_healing_skips_calls_that_already_have_results():
    """已有 ToolResult 的调用不再重复补全。"""
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="ok"))]])
    harness = make_harness(provider)
    call = ToolCall(id="c7", name="echo", arguments={})
    harness.append_message(UserMessage(content="go"))
    harness.append_message(AssistantMessage(content=[call]))
    harness.append_message(
        ToolResultMessage(tool_call_id="c7", tool_name="echo", content="done", is_error=False)
    )

    events = harness.continue_()
    results = [m for m in harness.messages if isinstance(m, ToolResultMessage)]
    assert len(results) == 1
    _ = [event async for event in events]


async def test_cancel_mid_stream_stops_and_resets_running():
    """运行中 cancel：流被截断，防御分支产出错误消息，状态复位。"""
    provider = FakeProvider(
        [[assistant_start(), text_delta("hi"), assistant_done(AssistantMessage(content="hi"))]]
    )
    harness = make_harness(provider)

    events = harness.prompt("go")
    _ = await events.__anext__()  # agent_start——此刻 _current_signal 已创建
    harness.cancel()

    rest = [event async for event in events]

    assert harness.is_running is False
    failure = harness.messages[-1]
    assert isinstance(failure, AssistantMessage)
    assert failure.stop_reason == "aborted"
    assert failure.error_message == "Operation aborted"
    assert rest[-1].type == "agent_end"


async def test_run_state_resets_when_cancelled_persistence_fails(monkeypatch):
    """取消收尾时持久化失败，也必须释放 Harness 的运行状态。"""
    provider = FakeProvider(
        [[assistant_start(), assistant_done(AssistantMessage(content="done"))]]
    )
    harness = make_harness(provider)

    async def fail_persistence() -> None:
        raise OSError("simulated session storage failure")

    events = harness.prompt("start")

    # 消费第一个事件，让本轮运行真正开始。
    first_event = await events.__anext__()
    assert first_event.type == "agent_start"
    assert harness.is_running is True

    # 只模拟取消后的保存失败，不影响 _run 开始时的初始保存。
    monkeypatch.setattr(
        harness,
        "_persist_unrecorded_messages",
        fail_persistence,
    )

    harness.cancel()

    # 保存异常需要传给调用方，不能被静默吞掉。
    with pytest.raises(OSError, match="simulated session storage failure"):
        async for _event in events:
            pass

    # 但异常不能让 Harness 永久处于 running 状态。
    assert harness.is_running is False


async def test_task_cancellation_resets_harness_and_allows_retry():
    """外层 task.cancel() 必须传播取消，并释放状态以允许下一轮运行。"""

    class BlockingOnceProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__(
                [
                    [
                        assistant_start(),
                        assistant_done(AssistantMessage(content="retry succeeded")),
                    ]
                ]
            )
            self.started = asyncio.Event()
            self._block_first_call = True

        def stream_response(
            self,
            *,
            model: str,
            system: str,
            messages: list[AgentMessage],
            tools: list[AgentTool],
            signal: CancellationToken | None = None,
        ) -> AsyncIterator[AssistantMessageEvent]:
            if self._block_first_call:
                self._block_first_call = False

                async def blocked_stream() -> AsyncIterator[AssistantMessageEvent]:
                    self.started.set()
                    # 永远等待，直到消费此流的 task 被外部取消。
                    await asyncio.Event().wait()
                    yield assistant_start()

                return blocked_stream()

            return super().stream_response(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                signal=signal,
            )

    provider = BlockingOnceProvider()
    harness = make_harness(provider)

    async def consume_first_run() -> None:
        async for _event in harness.prompt("first run"):
            pass

    task = asyncio.create_task(consume_first_run())
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.is_running is False

    retry_events = [event async for event in harness.prompt("retry")]

    assert retry_events[-1].type == "agent_end"
    assert isinstance(harness.messages[-1], AssistantMessage)
    assert harness.messages[-1].text == "retry succeeded"


async def test_closing_harness_event_stream_closes_provider_stream():
    """调用方提前关闭事件流时，上游 Provider 生成器也应完成清理。"""

    class CloseTrackingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.started = asyncio.Event()
            self.closed = asyncio.Event()

        def stream_response(
            self,
            *,
            model: str,
            system: str,
            messages: list[AgentMessage],
            tools: list[AgentTool],
            signal: CancellationToken | None = None,
        ) -> AsyncIterator[AssistantMessageEvent]:
            async def source() -> AsyncIterator[AssistantMessageEvent]:
                self.started.set()
                try:
                    yield assistant_start()
                    # 保持流打开，直到调用方提前关闭外层事件流。
                    await asyncio.Event().wait()
                finally:
                    self.closed.set()

            return source()

    provider = CloseTrackingProvider()
    harness = make_harness(provider)
    events = harness.prompt("start a streamed response")

    # 持续消费事件，直到 Provider 已经开始输出。
    while not provider.started.is_set():
        await events.__anext__()

    await events.aclose()

    assert harness.is_running is False
    assert provider.closed.is_set()


async def test_initial_messages_restore_conversation():
    """带历史构造：续跑时 provider 收到既有历史——Phase 2 会话恢复的钩子。"""
    provider = FakeProvider(
        [[assistant_start(), assistant_done(AssistantMessage(content="resumed"))]]
    )
    prior: list[AgentMessage] = [
        UserMessage(content="earlier"),
        AssistantMessage(content="before"),
    ]
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, model="fake", system="s"),
        messages=prior,
    )

    _ = [event async for event in harness.continue_()]

    assert len(provider.calls) == 1
    assert provider.calls[0][2] == prior
    prior.append(UserMessage(content="late"))  # 外部改动不泄漏进 harness
    assert len(harness.messages) == 3  # prior 两条 + resumed 一条


def test_queued_messages_and_clear_queues():
    """队列 API：入队、快照计数、清空。"""
    provider = FakeProvider([])
    harness = make_harness(provider)

    assert harness.has_queued_messages() is False
    harness.steer("one")
    harness.follow_up("two")
    queued = harness.steer("three")

    assert queued.count == 3
    assert len(queued.steering) == 2
    assert len(queued.follow_up) == 1
    assert harness.has_queued_messages() is True

    snapshot = harness.clear_queues()
    assert snapshot.count == 3
    assert harness.has_queued_messages() is False


def test_message_mutation_api():
    """append_message / replace_messages：直接操作历史的口子。"""
    provider = FakeProvider([])
    harness = make_harness(provider)

    msg = UserMessage(content="direct")
    harness.append_message(msg)
    assert harness.messages == (msg,)

    harness.replace_messages([UserMessage(content="clean slate")])
    assert harness.messages == (UserMessage(content="clean slate"),)


async def test_healing_syncs_entry_ids_for_compaction():
    """回归：自愈补占位结果时必须同步 _entry_ids，否则压缩闸门 zip(strict=True) 会炸。

    年久 Bug：_append_interrupted_tool_results 只 append 了 _messages 漏了
    _entry_ids，两个平行数组从此不等长。本测试锁住这个不变量。
    """
    provider = FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content="ok"))]])
    harness = make_harness(provider)
    dangling = ToolCall(id="c9", name="echo", arguments={})
    harness.append_message(UserMessage(content="go"))
    harness.append_message(AssistantMessage(content=[dangling]))

    events = harness.continue_()  # 首次消费时才执行自愈
    _ = [event async for event in events]

    # 核心不变量：自愈补了一条 ToolResultMessage，_entry_ids 必须同步 append None
    assert len(harness._messages) == len(harness._entry_ids)

    # 点睛：把阈值压到 1，任何非空上下文都超阈值，执行流才会走到 zip(strict=True)；
    # 若 _entry_ids 漏同步（4 条消息 vs 3 个 id），此处当场抛 ValueError。
    harness.config.auto_compact_token_threshold = 1
    assert harness._compaction_plan() is None
