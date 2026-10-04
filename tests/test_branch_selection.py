"""P6.5.3: select a safe branch point and preserve it across restarts."""

from __future__ import annotations

import pytest

from minitau_agent.message import AssistantMessage, ToolCall, UserMessage
from minitau_agent.session.entries import (
    CompactionEntry,
    LeafEntry,
    MessageEntry,
    SessionInfoEntry,
)
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager
from pi_event_helpers import assistant_done, assistant_start


def reply(text: str):
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


async def test_select_then_continue_and_resume_stays_on_new_branch(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "branch.jsonl"
    first = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=FakeProvider([reply("first answer"), reply("old answer")]),
            model="fake",
        ),
        session_id="branch",
        title="demo",
    )
    _ = [event async for event in first.prompt("first question")]
    _ = [event async for event in first.prompt("old question")]
    before = await JsonlSessionStorage(path).read_all()
    selected_id = before[3].id  # completed first assistant turn

    await first.branch_to(selected_id)

    selected = await JsonlSessionStorage(path).read_all()
    assert isinstance(selected[-1], LeafEntry)
    assert selected[-1].entry_id == selected_id
    assert [message.text for message in first.messages] == [
        "first question", "first answer"
    ]

    next_config = CodingSessionConfig(
        cwd=tmp_path,
        provider=FakeProvider([reply("new answer")]),
        model="fake",
    )
    resumed = await CodingSession.resume(next_config, path=path)
    assert [message.text for message in resumed.messages] == [
        "first question", "first answer"
    ]
    _ = [event async for event in resumed.prompt("new question")]

    final = await SessionManager(tmp_path).load("branch")
    assert [message.text for message in final.messages] == [
        "first question", "first answer", "new question", "new answer"
    ]
    assert final.last_entry_id != selected_id
    assert final.last_entry_id == (await JsonlSessionStorage(path).read_all())[-1].id


async def test_rejecting_incomplete_branch_point_does_not_change_history(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "branch.jsonl"
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=FakeProvider([reply("answer")]),
            model="fake",
        ),
        session_id="branch",
        title="demo",
    )
    _ = [event async for event in session.prompt("question")]
    before = await JsonlSessionStorage(path).read_all()
    user_entry_id = before[2].id

    with pytest.raises(ValueError, match="safe|complete|assistant|branch"):
        await session.branch_to(user_entry_id)

    assert await JsonlSessionStorage(path).read_all() == before
    assert [message.text for message in session.messages] == ["question", "answer"]


async def test_completed_compaction_is_a_branchable_point(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "branch.jsonl"
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=FakeProvider([reply("answer"), reply("summary")]),
            model="fake",
        ),
        session_id="branch",
        title="demo",
    )
    _ = [event async for event in session.prompt("question")]
    _ = [event async for event in session.compact()]
    before = await JsonlSessionStorage(path).read_all()
    compaction = before[-1]
    assert isinstance(compaction, CompactionEntry)

    await session.branch_to(compaction.id)

    after = await JsonlSessionStorage(path).read_all()
    assert isinstance(after[-1], LeafEntry)
    assert after[-1].entry_id == compaction.id
    assert [message.text for message in session.messages] == [
        "Previous conversation summary:\nsummary"
    ]


async def test_unanswered_tool_call_cannot_be_selected(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "branch.jsonl"
    storage = JsonlSessionStorage(path)
    entries = [
        SessionInfoEntry(id="info", cwd=str(tmp_path)),
        MessageEntry(
            id="user",
            parent_id="info",
            message=UserMessage(content="run a tool"),
        ),
        MessageEntry(
            id="tool-use",
            parent_id="user",
            message=AssistantMessage(
                model="fake",
                content=[ToolCall(id="call-1", name="read", arguments={})],
                stop_reason="toolUse",
            ),
        ),
    ]
    for entry in entries:
        await storage.append(entry)
    config = CodingSessionConfig(cwd=tmp_path, provider=FakeProvider([]), model="fake")
    session = await CodingSession.resume(config, path=path)

    with pytest.raises(ValueError, match="safe|complete|tool|branch"):
        await session.branch_to("tool-use")

    assert await storage.read_all() == entries
