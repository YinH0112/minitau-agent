"""P2 Step 4: AgentHarness 挂钩 SessionStorage 的记录行为测试。"""

from __future__ import annotations

from collections.abc import Mapping

from minitau_agent.harness import AgentHarness, AgentHarnessConfig
from minitau_agent.message import AssistantMessage, ToolCall
from minitau_agent.session.memory import SessionState
from minitau_agent.session.storage import JsonlSessionStorage, SessionStorage
from minitau_agent.tools import AgentTool, AgentToolResult
from minitau_agent.types import JSONValue
from minitau_ai.fake import FakeProvider
from pi_event_helpers import (
    assistant_done,
    assistant_error,
    assistant_start,
    tool_call_end,
)

ECHO_CALL = ToolCall(id="c1", name="echo", arguments={"text": "hi"})


def make_config(
    provider: FakeProvider,
    storage: SessionStorage | None = None,
) -> AgentHarnessConfig:
    return AgentHarnessConfig(
        provider=provider,
        model="fake",
        system="test system",
        tools=[],
        storage=storage,
    )


def make_provider(text: str) -> FakeProvider:
    return FakeProvider([[assistant_start(), assistant_done(AssistantMessage(content=text))]])


async def drain(iterator) -> None:
    async for _ in iterator:
        pass


class TestHarnessRecording:
    """harness 侧：观察事件流，把落定的消息写进 storage。"""

    async def test_without_storage_runs_unchanged(self, tmp_path) -> None:
        harness = AgentHarness(make_config(make_provider("hello")))

        await drain(harness.prompt("hi"))

        assert [type(m).__name__ for m in harness.messages] == ["UserMessage", "AssistantMessage"]

    async def test_prompt_records_user_and_assistant_entries(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        harness = AgentHarness(make_config(make_provider("hello"), storage))

        await drain(harness.prompt("hi"))

        entries = await storage.read_all()
        assert len(entries) == 2
        recorded_messages = [entry.message for entry in entries]  # type: ignore[union-attr]
        assert recorded_messages == list(harness.messages)

    async def test_tool_round_trip_records_tool_results(self, tmp_path) -> None:
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
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")

        async def execute(arguments: Mapping[str, JSONValue]) -> AgentToolResult:
            return AgentToolResult(content="echo:hi")

        tool = AgentTool(
            name="echo",
            description="Echo tool",
            parameters={"type": "object", "properties": {}},
            execute_fn=execute,
        )
        config = AgentHarnessConfig(
            provider=provider, model="fake", system="s", tools=[tool], storage=storage
        )
        harness = AgentHarness(config)

        await drain(harness.prompt("run"))

        entries = await storage.read_all()
        assert [type(entry.message).__name__ for entry in entries] == [  # type: ignore[union-attr]
            "UserMessage",
            "AssistantMessage",
            "ToolResultMessage",
            "AssistantMessage",
        ]

    async def test_entries_chain_via_parent_id(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        harness = AgentHarness(make_config(make_provider("hello"), storage))

        await drain(harness.prompt("hi"))

        first, second = await storage.read_all()
        assert first.parent_id is None
        assert second.parent_id == first.id

    async def test_error_placeholder_assistant_is_still_recorded(self, tmp_path) -> None:
        """错误占位 assistant 落盘：磁盘历史比模型上下文更完整。"""
        provider = FakeProvider([[assistant_start(), assistant_error("boom")]])
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        harness = AgentHarness(make_config(provider, storage))

        await drain(harness.prompt("hi"))

        entries = await storage.read_all()
        assert len(entries) == 2
        error_entry = entries[1]
        assert isinstance(error_entry.message, AssistantMessage)  # type: ignore[union-attr]
        assert error_entry.message.stop_reason == "error"  # type: ignore[union-attr]


class TestHarnessResume:
    """会话重启：两个 harness 进程先后写同一文件，链条不断。"""

    async def test_resumed_harness_continues_chain(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        first = AgentHarness(make_config(make_provider("第一答"), storage))
        await drain(first.prompt("第一问"))

        saved = await storage.read_all()
        state = SessionState.from_entries(saved)

        second = AgentHarness(
            make_config(make_provider("第二答"), storage),
            messages=state.messages,
            last_entry_id=saved[-1].id,
            entry_ids=state.context_entry_ids,
        )
        await drain(second.prompt("第二问"))

        entries = await storage.read_all()
        assert len(entries) == 4
        assert [type(entry.message).__name__ for entry in entries] == [  # type: ignore[union-attr]
            "UserMessage",
            "AssistantMessage",
            "UserMessage",
            "AssistantMessage",
        ]
        assert entries[2].parent_id == entries[1].id
        assert entries[3].parent_id == entries[2].id

        resumed = AgentHarness(
            make_config(make_provider("第三答"), storage),
            messages=SessionState.from_entries(entries).messages,
        )
        assert len(resumed.messages) == 4
