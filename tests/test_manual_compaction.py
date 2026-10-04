"""P6.3：手动压缩复用自动压缩的计划、摘要和持久化链路。"""

from __future__ import annotations

import pytest

from minitau_agent.compaction import select_compaction_rows
from minitau_agent.harness import AgentHarness, AgentHarnessConfig
from minitau_agent.message import AssistantMessage, UserMessage
from minitau_agent.provider_events import AssistantErrorEvent
from minitau_agent.session.entries import CompactionEntry
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.session import CodingSession, CodingSessionConfig
from pi_event_helpers import assistant_done, assistant_start

if not hasattr(AgentHarness, "compact") or not hasattr(CodingSession, "compact"):
    pytest.skip("等待 P6.3 手动压缩接口实现", allow_module_level=True)


def reply(text: str):
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


async def test_empty_history_is_explicit_no_op_and_does_not_call_provider(tmp_path):
    provider = FakeProvider([])
    config = CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake")
    session = await CodingSession.new(config, session_id="empty", title="空会话")

    events = [event async for event in session.compact()]

    assert len(events) == 1
    assert events[0].type == "compaction_end"
    assert events[0].reason == "manual"
    assert events[0].skipped is True
    assert provider.calls == []
    assert session.messages == ()


def test_one_non_summary_message_can_be_compacted():
    plan = select_compaction_rows(
        [("entry-1", UserMessage(content="一个目标"))], keep_recent_tokens=0
    )

    assert plan is not None
    assert plan.replace_entry_ids == ["entry-1"]


async def test_manual_compaction_persists_and_replays_summary(tmp_path):
    provider = FakeProvider([reply("原回答"), reply("Goal: 完成任务")])
    config = CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake")
    session = await CodingSession.new(config, session_id="saved", title="任务")
    _ = [event async for event in session.prompt("原问题")]

    events = [event async for event in session.compact()]

    assert [event.type for event in events] == ["compaction_start", "compaction_end"]
    assert events[-1].reason == "manual"
    assert events[-1].aborted is False
    assert events[-1].skipped is False
    assert [message.text for message in session.messages] == [
        "Previous conversation summary:\nGoal: 完成任务"
    ]

    path = tmp_path / ".minitau" / "sessions" / "saved.jsonl"
    entries = await JsonlSessionStorage(path).read_all()
    assert [entry.type for entry in entries] == [
        "session_info", "model_change", "message", "message", "compaction"
    ]
    assert isinstance(entries[-1], CompactionEntry)
    assert entries[-1].replaces_entry_ids == [entries[2].id, entries[3].id]

    resumed_config = CodingSessionConfig(cwd=tmp_path, provider=FakeProvider([]), model="fake")
    resumed = await CodingSession.resume(resumed_config, path=path)
    # 重放会重新构造摘要消息并生成时间戳，因此只比较实际喂给模型的内容。
    assert [(message.role, message.text) for message in resumed.messages] == [
        (message.role, message.text) for message in session.messages
    ]


async def test_summary_error_leaves_history_unchanged(tmp_path):
    provider = FakeProvider(
        [
            reply("原回答"),
            [AssistantErrorEvent(reason="error", error=AssistantMessage(content="boom"))],
        ]
    )
    config = CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake")
    session = await CodingSession.new(config, session_id="failed", title="任务")
    _ = [event async for event in session.prompt("原问题")]
    original = session.messages

    events = [event async for event in session.compact()]

    assert events[-1].type == "compaction_end"
    assert events[-1].reason == "manual"
    assert events[-1].aborted is True
    assert session.messages == original
    path = tmp_path / ".minitau" / "sessions" / "failed.jsonl"
    assert not any(
        isinstance(entry, CompactionEntry)
        for entry in await JsonlSessionStorage(path).read_all()
    )
    assert session.is_running is False


async def test_cancel_after_start_does_not_commit_summary():
    provider = FakeProvider([reply("摘要")])
    harness = AgentHarness(AgentHarnessConfig(provider=provider, model="fake", system="test"))
    harness.append_message(UserMessage(content="旧消息"))
    harness.append_message(AssistantMessage(content="旧回答"))
    original = harness.messages

    events = harness.compact()
    first = await anext(events)
    assert first.type == "compaction_start"
    assert harness.is_running is True
    harness.cancel()
    rest = [event async for event in events]

    assert rest[-1].type == "compaction_end"
    assert rest[-1].aborted is True
    assert harness.messages == original
    assert harness.is_running is False


async def test_started_prompt_blocks_manual_compaction():
    provider = FakeProvider([reply("完成")])
    harness = AgentHarness(AgentHarnessConfig(provider=provider, model="fake", system="test"))
    prompt_events = harness.prompt("问题")
    await anext(prompt_events)

    with pytest.raises(RuntimeError, match="already running"):
        harness.compact()

    await prompt_events.aclose()
    assert harness.is_running is False


async def test_oversized_summary_request_aborts_before_provider_call():
    provider = FakeProvider([reply("不应被调用")])
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, model="fake", system="test", context_window_tokens=40)
    )
    harness.append_message(UserMessage(content="很长的历史" * 100))
    original = harness.messages

    events = [event async for event in harness.compact()]

    assert events[-1].type == "compaction_end"
    assert events[-1].aborted is True
    assert "window" in (events[-1].error_message or "").lower()
    assert provider.calls == []
    assert harness.messages == original
