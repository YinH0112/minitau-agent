"""P6.5.2: a persisted leaf restores the selected branch and append parent."""

from __future__ import annotations

import pytest

from minitau_agent.message import AssistantMessage, UserMessage
from minitau_agent.session.entries import LeafEntry, MessageEntry, SessionInfoEntry
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager
from pi_event_helpers import assistant_done, assistant_start


async def write_entries(path, entries) -> None:
    storage = JsonlSessionStorage(path)
    for entry in entries:
        await storage.append(entry)


def branch_entries(cwd):
    return [
        SessionInfoEntry(id="info", cwd=str(cwd), title="Original"),
        MessageEntry(id="a", parent_id="info", message=UserMessage(content="A")),
        MessageEntry(
            id="b",
            parent_id="a",
            message=AssistantMessage(model="fake", content="B"),
        ),
        MessageEntry(id="c", parent_id="b", message=UserMessage(content="C")),
        SessionInfoEntry(
            id="renamed", parent_id="c", cwd=str(cwd), title="Renamed"
        ),
        LeafEntry(id="select-b", parent_id="b", entry_id="b"),
    ]


async def test_snapshot_replays_selected_branch_but_keeps_global_title(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "branch.jsonl"
    await write_entries(path, branch_entries(tmp_path))

    snapshot = await SessionManager(tmp_path).load("branch")

    assert [message.text for message in snapshot.messages] == ["A", "B"]
    assert snapshot.entry_ids == ("a", "b")
    assert snapshot.last_entry_id == "b"
    assert snapshot.session_info.title == "Renamed"


async def test_resumed_prompt_uses_selected_leaf_as_parent(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "branch.jsonl"
    await write_entries(path, branch_entries(tmp_path))
    provider = FakeProvider(
        [[assistant_start(), assistant_done(AssistantMessage(content="D"))]]
    )
    config = CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake")

    resumed = await CodingSession.resume(config, path=path)
    _ = [event async for event in resumed.prompt("new branch")]

    entries = await JsonlSessionStorage(path).read_all()
    assert entries[-2].parent_id == "b"
    assert entries[-1].parent_id == entries[-2].id
    assert [message.text for message in provider.calls[0][2]] == [
        "A", "B", "new branch"
    ]


async def test_snapshot_rejects_leaf_pointer_to_missing_entry(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "broken.jsonl"
    await write_entries(
        path,
        [
            SessionInfoEntry(id="info", cwd=str(tmp_path)),
            LeafEntry(id="select-missing", parent_id="info", entry_id="missing"),
        ],
    )

    with pytest.raises(ValueError, match="leaf|Leaf|missing"):
        await SessionManager(tmp_path).load("broken")


async def test_messages_written_after_selection_advance_active_leaf(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "continued.jsonl"
    entries = branch_entries(tmp_path)
    entries.extend(
        [
            MessageEntry(
                id="d", parent_id="b", message=UserMessage(content="D")
            ),
            MessageEntry(
                id="e",
                parent_id="d",
                message=AssistantMessage(model="fake", content="E"),
            ),
            # A later write on another branch must not move the active leaf.
            MessageEntry(
                id="other", parent_id="c", message=UserMessage(content="other")
            ),
        ]
    )
    await write_entries(path, entries)

    snapshot = await SessionManager(tmp_path).load("continued")

    assert [message.text for message in snapshot.messages] == ["A", "B", "D", "E"]
    assert snapshot.entry_ids == ("a", "b", "d", "e")
    assert snapshot.last_entry_id == "e"
