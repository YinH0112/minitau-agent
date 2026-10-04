"""P6.5.1: branch entries and explicit root-to-leaf session replay."""

from __future__ import annotations

import pytest

from minitau_agent.message import UserMessage
from minitau_agent.session import entries as entry_models
from minitau_agent.session.entries import (
    CompactionEntry,
    MessageEntry,
    SessionInfoEntry,
)
from minitau_agent.session.jsonl import entry_from_json_line, entry_to_json_line
from minitau_agent.session.memory import SessionState
from minitau_agent.session.tree import SessionTreeError

LeafEntry = getattr(entry_models, "LeafEntry", None)
if LeafEntry is None:
    pytest.skip("等待 P6.5.1 实现 LeafEntry", allow_module_level=True)


def message(entry_id: str, parent_id: str, content: str) -> MessageEntry:
    return MessageEntry(
        id=entry_id,
        parent_id=parent_id,
        message=UserMessage(content=content),
    )


def test_leaf_entry_round_trip_is_not_a_message() -> None:
    marker = LeafEntry(id="select-b", parent_id="b", entry_id="b")

    restored = entry_from_json_line(entry_to_json_line(marker))

    assert restored == marker
    assert restored.type == "leaf"
    assert restored.entry_id == "b"


def test_explicit_leaf_replays_only_its_ancestors() -> None:
    info = SessionInfoEntry(id="info", title="branch demo")
    entries = [
        info,
        message("a", "info", "A"),
        message("b", "a", "B"),
        message("c", "b", "C"),
        message("d", "b", "D"),
    ]

    c_state = SessionState.from_entries(entries, leaf_id="c")
    d_state = SessionState.from_entries(entries, leaf_id="d")

    assert [item.text for item in c_state.messages] == ["A", "B", "C"]
    assert c_state.context_entry_ids == ("a", "b", "c")
    assert [item.text for item in d_state.messages] == ["A", "B", "D"]
    assert d_state.context_entry_ids == ("a", "b", "d")
    assert c_state.session_info == d_state.session_info == info


def test_leaf_marker_does_not_join_model_context() -> None:
    entries = [
        SessionInfoEntry(id="info"),
        message("a", "info", "A"),
        message("b", "a", "B"),
        LeafEntry(id="select-b", parent_id="b", entry_id="b"),
        message("c", "b", "C"),
    ]

    at_marker = SessionState.from_entries(entries, leaf_id="select-b")
    state = SessionState.from_entries(entries, leaf_id="c")

    assert [item.text for item in at_marker.messages] == ["A", "B"]
    assert at_marker.context_entry_ids == ("a", "b")
    assert [item.text for item in state.messages] == ["A", "B", "C"]
    assert state.context_entry_ids == ("a", "b", "c")


def test_compaction_applies_only_on_selected_branch() -> None:
    entries = [
        SessionInfoEntry(id="info"),
        message("a", "info", "A"),
        message("b", "a", "B"),
        CompactionEntry(
            id="summary",
            parent_id="b",
            summary="A and B",
            replaces_entry_ids=["a", "b"],
        ),
        message("after", "summary", "after summary"),
        message("side", "b", "side branch"),
    ]

    compacted = SessionState.from_entries(entries, leaf_id="after")
    side = SessionState.from_entries(entries, leaf_id="side")

    assert [item.text for item in compacted.messages] == [
        "Previous conversation summary:\nA and B",
        "after summary",
    ]
    assert compacted.context_entry_ids == ("summary", "after")
    assert [item.text for item in side.messages] == ["A", "B", "side branch"]
    assert side.context_entry_ids == ("a", "b", "side")


def test_unknown_leaf_is_rejected_instead_of_replaying_other_history() -> None:
    entries = [SessionInfoEntry(id="info"), message("a", "info", "A")]

    with pytest.raises(SessionTreeError):
        SessionState.from_entries(entries, leaf_id="unknown")


def test_legacy_linear_replay_still_works_without_leaf_argument() -> None:
    entries = [
        SessionInfoEntry(id="info"),
        message("a", "info", "A"),
        message("b", "a", "B"),
    ]

    state = SessionState.from_entries(entries)

    assert [item.text for item in state.messages] == ["A", "B"]
    assert state.context_entry_ids == ("a", "b")
