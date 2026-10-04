"""P7.4: verify the complete offline coding-agent scenario."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from minitau_agent.message import AssistantMessage, ToolCall, ToolResultMessage
from minitau_agent.session.entries import CompactionEntry, LeafEntry
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager
from pi_event_helpers import assistant_done, assistant_start


def tool_turn(call: ToolCall) -> list:
    return [assistant_start(), assistant_done(AssistantMessage(content=[call]))]


def final_turn(text: str) -> list:
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


async def consume(session: CodingSession, prompt: str) -> list:
    return [event async for event in session.prompt(prompt)]


async def test_complete_react_file_workflow_survives_compaction_and_branch(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("before\n", encoding="utf-8")
    read_call = ToolCall(
        id="read-1", name="read", arguments={"path": "notes.txt"}
    )
    write_call = ToolCall(
        id="write-1",
        name="write",
        arguments={"path": "notes.txt", "content": "alpha\nbeta\n"},
    )
    edit_call = ToolCall(
        id="edit-1",
        name="edit",
        arguments={
            "path": "notes.txt",
            "edits": [{"oldText": "alpha", "newText": "ALPHA"}],
        },
    )
    bash_call = ToolCall(
        id="bash-1", name="bash", arguments={"command": "echo p74-ok"}
    )
    provider = FakeProvider(
        [
            tool_turn(read_call),
            final_turn("read complete"),
            tool_turn(write_call),
            final_turn("write complete"),
            tool_turn(edit_call),
            final_turn("edit complete"),
            tool_turn(bash_call),
            final_turn("bash complete"),
            final_turn("Goal: notes were edited and verified."),
            final_turn("resumed answer"),
        ]
    )
    session = await CodingSession.new(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake"),
        session_id="p74",
        title="workflow",
    )

    await consume(session, "read notes")
    await consume(session, "write notes")
    await consume(session, "edit notes")
    await consume(session, "verify notes")

    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "ALPHA\nbeta\n"
    assert any(
        isinstance(message, ToolResultMessage) and "p74-ok" in message.content
        for message in session.messages
    )

    compact_events = [event async for event in session.compact()]
    assert compact_events[-1].type == "compaction_end"
    assert compact_events[-1].aborted is False

    path = tmp_path / ".minitau" / "sessions" / "p74.jsonl"
    entries = await JsonlSessionStorage(path).read_all()
    compaction = next(entry for entry in entries if isinstance(entry, CompactionEntry))
    snapshot = await SessionManager(tmp_path).load("p74")
    resumed = CodingSession.from_snapshot(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="factory"),
        snapshot,
    )
    assert resumed.messages == tuple(snapshot.messages)
    await consume(resumed, "continue after restore")

    await resumed.branch_to(compaction.id)
    branch_entries = await JsonlSessionStorage(path).read_all()
    assert isinstance(branch_entries[-1], LeafEntry)
    assert branch_entries[-1].entry_id == compaction.id
    assert resumed.messages == (
        resumed.messages[0],
    )
    assert "Previous conversation summary:" in resumed.messages[0].text


async def test_cancelled_long_bash_can_continue_in_same_session(tmp_path: Path) -> None:
    executable = str(sys.executable).replace("\\", "/")
    slow_call = ToolCall(
        id="slow-bash",
        name="bash",
        arguments={"command": f'"{executable}" -c "import time; time.sleep(8)"'},
    )
    provider = FakeProvider(
        [tool_turn(slow_call), final_turn("continued after cancel")]
    )
    session = await CodingSession.new(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake"),
        session_id="cancel-continue",
        title="cancel",
    )
    tool_started = asyncio.Event()

    async def run() -> None:
        async for event in session.prompt("run a long command"):
            if event.type == "tool_execution_start":
                tool_started.set()

    task = asyncio.create_task(run())
    await asyncio.wait_for(tool_started.wait(), timeout=2)
    session.cancel()
    await asyncio.wait_for(task, timeout=6)

    assert session.is_running is False
    assert len(provider.calls) == 1
    await consume(session, "continue")
    assert len(provider.calls) == 2
    assert session.messages[-1].text == "continued after cancel"
