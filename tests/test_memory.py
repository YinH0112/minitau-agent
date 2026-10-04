"""P2 Step 3: SessionState.from_entries 重放行为测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from minitau_agent.message import AssistantMessage, UserMessage
from minitau_agent.session.entries import MessageEntry, SessionInfoEntry
from minitau_agent.session.jsonl import entries_from_json_lines, entry_to_json_line
from minitau_agent.session.memory import SessionState


def user_entry(text: str) -> MessageEntry:
    return MessageEntry(message=UserMessage(content=text))


def assistant_entry(text: str) -> MessageEntry:
    return MessageEntry(message=AssistantMessage(model="fake", content=text))


class TestFromEntries:
    """from_entries：条目列表 → 内存状态的重放算法。"""

    def test_empty_entries_returns_empty_state(self) -> None:
        state = SessionState.from_entries([])

        assert state.messages == []
        assert state.session_info is None

    def test_messages_collected_in_order(self) -> None:
        entries = [user_entry("第一问"), assistant_entry("第一答"), user_entry("第二问")]

        state = SessionState.from_entries(entries)

        assert state.messages == [entry.message for entry in entries]

    def test_message_entries_are_projected_not_wrapped(self) -> None:
        """投影：messages 里是内层 AgentMessage，不是 MessageEntry 信封。"""
        entries = [user_entry("你好")]

        state = SessionState.from_entries(entries)

        assert isinstance(state.messages[0], UserMessage)

    def test_only_message_entries_go_to_messages(self) -> None:
        """SessionInfoEntry 是元数据，绝不能混进喂模型的 messages。"""
        entries = [
            SessionInfoEntry(cwd="d:/work", title="demo"),
            user_entry("你好"),
        ]

        state = SessionState.from_entries(entries)

        assert len(state.messages) == 1
        assert state.session_info is not None
        assert state.session_info.title == "demo"

    def test_session_info_last_one_wins(self) -> None:
        """多条 SessionInfoEntry：最后一条代表当前生效的元数据。"""
        entries = [
            SessionInfoEntry(cwd="d:/old", title="旧标题"),
            user_entry("改个标题吧"),
            SessionInfoEntry(cwd="d:/new", title="新标题"),
        ]

        state = SessionState.from_entries(entries)

        assert state.session_info is not None
        assert state.session_info.title == "新标题"
        assert state.session_info.cwd == "d:/new"

    def test_state_is_frozen(self) -> None:
        state = SessionState.from_entries([user_entry("你好")])

        with pytest.raises(FrozenInstanceError):
            state.session_info = SessionInfoEntry()  # type: ignore[misc]

    def test_jsonl_round_trip_projection(self) -> None:
        """全链路：entry → JSON 行 → 解析 → 重放，投影结果与原消息相等。"""
        original = [user_entry("你好"), assistant_entry("你好！我是 minitau。")]
        lines = [entry_to_json_line(entry) for entry in original]

        restored = SessionState.from_entries(entries_from_json_lines(lines))

        assert restored.messages == [entry.message for entry in original]
