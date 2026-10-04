"""P6.6.2: model settings are append-only, branch-aware session entries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minitau_agent.message import AssistantMessage, UserMessage
from minitau_agent.session import entries as entry_models
from minitau_agent.session.entries import MessageEntry, SessionInfoEntry
from minitau_agent.session.jsonl import entry_from_json_line, entry_to_json_line
from minitau_agent.session.memory import SessionState

ModelChangeEntry = getattr(entry_models, "ModelChangeEntry", None)
if ModelChangeEntry is None:
    pytest.skip("等待 P6.6.2 实现 ModelChangeEntry", allow_module_level=True)


def test_model_change_round_trip_contains_no_credentials() -> None:
    change = ModelChangeEntry(
        id="model-a",
        parent_id="info",
        provider_name="fake",
        model="alpha",
        context_window_tokens=8192,
    )

    line = entry_to_json_line(change)
    restored = entry_from_json_line(line)

    assert restored == change
    assert restored.type == "model_change"
    assert "api_key" not in line


def test_model_change_rejects_invalid_window_and_unexpected_secret() -> None:
    with pytest.raises(ValidationError):
        ModelChangeEntry(
            provider_name="fake", model="alpha", context_window_tokens=0
        )

    with pytest.raises(ValidationError):
        ModelChangeEntry(provider_name="fake", model="alpha", api_key="dummy")


def test_model_settings_follow_selected_branch() -> None:
    entries = [
        SessionInfoEntry(id="info"),
        ModelChangeEntry(
            id="initial",
            parent_id="info",
            provider_name="fake",
            model="alpha",
            context_window_tokens=8192,
        ),
        MessageEntry(
            id="question",
            parent_id="initial",
            message=UserMessage(content="question"),
        ),
        MessageEntry(
            id="answer",
            parent_id="question",
            message=AssistantMessage(model="alpha", content="answer"),
        ),
        ModelChangeEntry(
            id="left",
            parent_id="answer",
            provider_name="fake",
            model="beta",
            context_window_tokens=16384,
        ),
        ModelChangeEntry(
            id="right",
            parent_id="answer",
            provider_name="fake",
            model="gamma",
            context_window_tokens=4096,
        ),
    ]

    before_fork = SessionState.from_entries(entries, leaf_id="answer")
    left = SessionState.from_entries(entries, leaf_id="left")
    right = SessionState.from_entries(entries, leaf_id="right")

    assert before_fork.model_change is not None
    assert before_fork.model_change.model == "alpha"
    assert left.model_change is not None
    assert (left.model_change.model, left.model_change.context_window_tokens) == (
        "beta",
        16384,
    )
    assert right.model_change is not None
    assert (right.model_change.model, right.model_change.context_window_tokens) == (
        "gamma",
        4096,
    )
    assert [message.text for message in left.messages] == ["question", "answer"]
    assert [message.text for message in right.messages] == ["question", "answer"]


def test_legacy_session_without_model_entry_uses_no_stored_choice() -> None:
    entries = [
        SessionInfoEntry(id="info"),
        MessageEntry(
            id="question",
            parent_id="info",
            message=UserMessage(content="hello"),
        ),
    ]

    state = SessionState.from_entries(entries)

    assert state.model_change is None
    assert [message.text for message in state.messages] == ["hello"]
