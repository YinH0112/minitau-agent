"""P3 Step 2: CompactionEntry——第三种条目类型（笔记 04-02 + 05 系列）。"""

from __future__ import annotations

import pytest

from minitau_agent.message import UserMessage
from minitau_agent.session.entries import CompactionEntry, MessageEntry
from minitau_agent.session.jsonl import (
    SessionJsonlError,
    entry_from_json_line,
    entry_to_json_line,
)
from minitau_agent.session.tree import path_to_entry


def msg_entry(entry_id: str, parent_id: str | None, text: str) -> MessageEntry:
    """构造可显式指定 id/parent_id 的 MessageEntry（测试用）。"""
    return MessageEntry(
        id=entry_id,
        parent_id=parent_id,
        message=UserMessage(content=text),
    )


class TestCompactionEntrySchema:
    """schema：两个自有字段 + 继承的公共字段。"""

    def test_type_discriminator_is_compaction(self) -> None:
        entry = CompactionEntry(summary="摘要", replaces_entry_ids=["a", "b"])

        assert entry.type == "compaction"

    def test_summary_is_plain_string(self) -> None:
        """磁盘存原始摘要文本；包装成 UserMessage 是重放层（Step 3）的事。"""
        entry = CompactionEntry(summary="用户要求改用 JWT")

        assert entry.summary == "用户要求改用 JWT"

    def test_replaces_entry_ids_defaults_to_empty_list(self) -> None:
        entry = CompactionEntry(summary="s")

        assert entry.replaces_entry_ids == []

    def test_each_instance_gets_its_own_list(self) -> None:
        """default_factory 防止所有实例共享同一个列表。"""
        first = CompactionEntry(summary="a")
        second = CompactionEntry(summary="b")

        first.replaces_entry_ids.append("x")

        assert second.replaces_entry_ids == []

    def test_inherits_common_fields_and_chains(self) -> None:
        """它仍是树节点：id/timestamp 自动生成，parent_id 接链。"""
        root = msg_entry("root", None, "A")

        entry = CompactionEntry(summary="s", parent_id=root.id)

        assert entry.parent_id == "root"
        assert entry.id
        assert entry.timestamp > 0


class TestCompactionEntryJsonl:
    """判别联合扩展：路由 + 往返 + 默认值落盘。"""

    def test_union_routes_compaction_type(self) -> None:
        line = (
            '{"type":"compaction","summary":"旧对话摘要",'
            '"replaces_entry_ids":["e1","e2"]}'
        )

        entry = entry_from_json_line(line)

        assert isinstance(entry, CompactionEntry)
        assert entry.summary == "旧对话摘要"
        assert entry.replaces_entry_ids == ["e1", "e2"]

    def test_round_trip_preserves_all_fields(self) -> None:
        entry = CompactionEntry(
            id="c1",
            parent_id="root",
            summary="第一轮压缩摘要",
            replaces_entry_ids=["a", "b"],
        )

        restored = entry_from_json_line(entry_to_json_line(entry))

        assert restored == entry

    def test_empty_replaces_list_survives_round_trip(self) -> None:
        """空列表不是 None——exclude_none 不剔除它，落盘后回读仍是 []。"""
        entry = CompactionEntry(id="c1", summary="s")

        line = entry_to_json_line(entry)
        restored = entry_from_json_line(line)

        assert '"replaces_entry_ids"' in line
        assert restored.replaces_entry_ids == []

    def test_misspelled_type_still_rejected(self) -> None:
        """联合扩员不能放松校验：拼错的 type 照样拒绝。"""
        with pytest.raises(SessionJsonlError):
            entry_from_json_line('{"type":"compactionx","summary":"s"}')


class TestCompactionEntryInTree:
    """压缩条目参与 parent 链——为 Step 3 重放和 Step 5 触发做地基。"""

    def test_participates_in_parent_chain(self) -> None:
        """压缩后新消息挂在 CompactionEntry 之下——链条不断。"""
        a = msg_entry("a", None, "A")
        c = CompactionEntry(id="c", parent_id="a", summary="摘要")
        d = msg_entry("d", "c", "D")

        path = path_to_entry([a, c, d], "d")

        assert [entry.id for entry in path] == ["a", "c", "d"]
