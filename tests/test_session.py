"""P2 Step 1: session entries and JSONL serialization round-trips."""

from __future__ import annotations

import json

import pytest

from minitau_agent.message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from minitau_agent.session.entries import (
    MessageEntry,
    SessionInfoEntry,
)
from minitau_agent.session.jsonl import (
    SessionJsonlError,
    entries_from_json_lines,
    entry_from_json_line,
    entry_to_json_line,
)


def make_user_entry() -> MessageEntry:
    return MessageEntry(message=UserMessage(content="你好"))


def make_assistant_entry() -> MessageEntry:
    return MessageEntry(
        message=AssistantMessage(model="fake", content="你好！我是 minitau。")
    )


class TestSessionEntries:
    """entries.py：条目模型本身。"""

    def test_message_entry_wraps_agent_message(self) -> None:
        entry = make_user_entry()

        assert entry.type == "message"
        assert entry.message.role == "user"
        assert entry.message.content == "你好"

    def test_entries_get_unique_ids(self) -> None:
        assert make_user_entry().id != make_user_entry().id

    def test_parent_id_defaults_to_none(self) -> None:
        assert make_user_entry().parent_id is None

    def test_timestamp_is_unix_seconds(self) -> None:
        assert make_user_entry().timestamp > 1_000_000_000

    def test_session_info_entry_fields(self) -> None:
        entry = SessionInfoEntry(cwd="d:/work", title="demo")

        assert entry.type == "session_info"
        assert entry.cwd == "d:/work"
        assert entry.title == "demo"


class TestEntryToJsonLine:
    """jsonl.py：序列化方向（对象 → 一行 JSON）。"""

    def test_line_is_valid_json_ending_with_newline(self) -> None:
        line = entry_to_json_line(make_user_entry())

        assert line.endswith("\n")
        payload = json.loads(line)
        assert payload["type"] == "message"

    def test_none_fields_are_excluded(self) -> None:
        payload = json.loads(entry_to_json_line(make_user_entry()))
        assert "parent_id" not in payload

        info = json.loads(entry_to_json_line(SessionInfoEntry(cwd="d:/work")))
        assert "title" not in info

    def test_entry_layer_snake_case_message_layer_camel_case(self) -> None:
        """存储格式的双轨命名：entry 层 snake_case，内嵌消息层 camelCase。"""
        entry = MessageEntry(
            message=ToolResultMessage(
                tool_call_id="call_1",
                tool_name="bash",
                content="ok",
            )
        )
        payload = json.loads(entry_to_json_line(entry))
        message = payload["message"]

        assert message["toolCallId"] == "call_1"
        assert message["toolName"] == "bash"


class TestEntryFromJsonLine:
    """jsonl.py：反序列化方向（一行 JSON → 对象）。"""

    def test_round_trip_user_entry(self) -> None:
        original = make_user_entry()

        restored = entry_from_json_line(entry_to_json_line(original))

        assert restored == original

    def test_round_trip_assistant_entry(self) -> None:
        original = make_assistant_entry()

        restored = entry_from_json_line(entry_to_json_line(original))

        assert restored == original

    def test_round_trip_session_info_entry(self) -> None:
        original = SessionInfoEntry(cwd="d:/work", title="demo")

        restored = entry_from_json_line(entry_to_json_line(original))

        assert restored == original

    def test_discriminator_routes_to_correct_entry_class(self) -> None:
        line = json.dumps(
            {
                "type": "session_info",
                "cwd": "d:/work",
                "id": "e1",
                "timestamp": 1.0,
            }
        )

        entry = entry_from_json_line(line)

        assert isinstance(entry, SessionInfoEntry)

    def test_invalid_json_raises_session_jsonl_error(self) -> None:
        with pytest.raises(SessionJsonlError):
            entry_from_json_line("{not json")

    def test_unknown_type_raises_session_jsonl_error(self) -> None:
        with pytest.raises(SessionJsonlError):
            entry_from_json_line(json.dumps({"type": "wat"}))

    def test_missing_message_field_raises(self) -> None:
        with pytest.raises(SessionJsonlError):
            entry_from_json_line(json.dumps({"type": "message"}))

    def test_error_message_includes_line_number(self) -> None:
        with pytest.raises(SessionJsonlError, match="line 7"):
            entry_from_json_line("{not json", line_number=7)


class TestEntriesFromJsonLines:
    """jsonl.py：多行批处理。"""

    def test_empty_lines_are_skipped(self) -> None:
        lines = ["", "   ", entry_to_json_line(make_user_entry()), ""]

        entries = entries_from_json_lines(lines)

        assert len(entries) == 1
        assert entries[0].message.content == "你好"

    def test_order_is_preserved(self) -> None:
        lines = [
            entry_to_json_line(make_user_entry()),
            entry_to_json_line(make_assistant_entry()),
        ]

        entries = entries_from_json_lines(lines)

        assert [entry.message.role for entry in entries] == ["user", "assistant"]

    def test_empty_input_returns_empty_list(self) -> None:
        assert entries_from_json_lines([]) == []
