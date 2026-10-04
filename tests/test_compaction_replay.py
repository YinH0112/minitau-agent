"""P3 Step 3: 重放升级——消息行携带 id + _apply_compaction 替换（笔记 05-01）。"""

from __future__ import annotations

from minitau_agent.message import AssistantMessage, UserMessage
from minitau_agent.session.entries import CompactionEntry, MessageEntry
from minitau_agent.session.memory import (
    SessionState,
    _apply_compaction,
    _format_compaction_summary,
)


def msg_entry(entry_id: str, text: str, role: str = "user") -> MessageEntry:
    """构造显式 id 的 MessageEntry；parent 链由 helper 自动接上。"""
    message: UserMessage | AssistantMessage
    if role == "user":
        message = UserMessage(content=text)
    else:
        message = AssistantMessage(model="m", content=[])
    return MessageEntry(id=entry_id, message=message)


def compact(summary: str, *replaced: str) -> CompactionEntry:
    return CompactionEntry(id="comp", summary=summary, replaces_entry_ids=list(replaced))


class TestFormatCompactionSummary:
    """注入模型的摘要文案格式。"""

    def test_wraps_summary_with_marker(self) -> None:
        text = _format_compaction_summary("用户讨论了 JWT")

        assert text == "Previous conversation summary:\n用户讨论了 JWT"


class TestApplyCompaction:
    """替换算法：命中 id 的消息行被一条摘要行取代。"""

    def test_replaces_matched_rows_in_place(self) -> None:
        """A 被替换 → 摘要出现在 A 原来的位置，B 原样保留。"""
        rows = [("a", UserMessage(content="A")), ("b", UserMessage(content="B"))]

        result = _apply_compaction(rows, compact("摘要", "a"))

        assert len(result) == 2
        assert result[0][0] == "comp"
        assert result[0][1].text == "Previous conversation summary:\n摘要"
        assert result[1] == rows[1]

    def test_preserves_order_of_unmatched_rows(self) -> None:
        """被替换的多行在原位置塌缩成一行，未命中的行序不变。"""
        rows = [
            ("a", UserMessage(content="A")),
            ("b", UserMessage(content="B")),
            ("c", UserMessage(content="C")),
        ]

        result = _apply_compaction(rows, compact("s", "a", "b"))

        assert [row_id for row_id, _ in result] == ["comp", "c"]
        assert result[0][1].text.startswith("Previous conversation summary:")

    def test_no_match_appends_summary_at_end(self) -> None:
        """replaces 列表指向不存在的 id（老文件/手改文件）——兜底：追加到末尾。"""
        rows = [("a", UserMessage(content="A"))]

        result = _apply_compaction(rows, compact("s", "ghost"))

        assert [row_id for row_id, _ in result] == ["a", "comp"]

    def test_idempotent_against_repeated_compaction_of_same_range(self) -> None:
        """同一范围被两次压缩：第二次找不到旧 id，摘要作为新行追加。"""
        rows = [("a", UserMessage(content="A")), ("b", UserMessage(content="B"))]

        first = _apply_compaction(rows, compact("s1", "a", "b"))
        second = _apply_compaction(first, compact("s2", "a", "b"))

        assert [row_id for row_id, _ in second] == ["comp", "comp"]
        assert second[0][1].text.endswith("s1")
        assert second[1][1].text.endswith("s2")


class TestFromEntriesWithCompaction:
    """重放主链路：CompactionEntry 分支接入 from_entries。"""

    def test_compaction_shrinks_context_and_keeps_tail(self) -> None:
        """笔记 05-01 的对照表：A~E 压缩成一条摘要，尾部消息保留。"""
        entries = [
            msg_entry("a", "A"),
            msg_entry("b", "B"),
            msg_entry("c", "C"),
            msg_entry("d", "D"),
            msg_entry("e", "E"),
            compact("旧对话：A 到 D 的摘要", "a", "b", "c", "d"),
            msg_entry("f", "F"),
        ]

        state = SessionState.from_entries(entries)

        assert len(state.messages) == 3
        assert state.messages[0].text == "Previous conversation summary:\n旧对话：A 到 D 的摘要"
        # e 在 compact 之前入史、未被替换 → 紧跟摘要原位保留
        assert state.messages[1].text == "E"
        # f 在 compact 之后追加 → 队尾
        assert state.messages[2].text == "F"

    def test_summary_message_is_user_message(self) -> None:
        """摘要注入为 UserMessage——作为上下文背景，不冒充 assistant 说过。"""
        entries = [msg_entry("a", "A"), compact("s", "a")]

        state = SessionState.from_entries(entries)

        assert isinstance(state.messages[0], UserMessage)
