"""P3 Step 1: 会话树遍历——entries_by_id + path_to_entry（笔记 04-04）。"""

from __future__ import annotations

import pytest

from minitau_agent.message import UserMessage
from minitau_agent.session.entries import MessageEntry
from minitau_agent.session.tree import SessionTreeError, entries_by_id, path_to_entry


def msg_entry(entry_id: str, parent_id: str | None, text: str) -> MessageEntry:
    """构造可显式指定 id/parent_id 的 MessageEntry（测试用）。"""
    return MessageEntry(
        id=entry_id,
        parent_id=parent_id,
        message=UserMessage(content=text),
    )


class TestEntriesById:
    """索引 + 完整性检查。"""

    def test_indexes_all_entries_by_id(self) -> None:
        a = msg_entry("a", None, "A")
        b = msg_entry("b", "a", "B")

        by_id = entries_by_id([a, b])

        assert by_id == {"a": a, "b": b}

    def test_empty_list_gives_empty_index(self) -> None:
        assert entries_by_id([]) == {}

    def test_duplicate_ids_raise_session_tree_error(self) -> None:
        """id 是树节点的引用键，重复会让路径产生歧义——宁可显式失败。"""
        first = msg_entry("dup", None, "1")
        second = msg_entry("dup", None, "2")

        with pytest.raises(SessionTreeError, match="dup"):
            entries_by_id([first, second])


class TestPathToEntry:
    """从任意节点沿 parent_id 回溯到根，返回根→叶顺序。"""

    def test_follows_parent_chain_root_to_leaf(self) -> None:
        a = msg_entry("a", None, "A")
        b = msg_entry("b", "a", "B")
        c = msg_entry("c", "b", "C")

        path = path_to_entry([a, b, c], "c")

        assert [entry.id for entry in path] == ["a", "b", "c"]

    def test_single_node_is_its_own_path(self) -> None:
        root = msg_entry("root", None, "A")

        path = path_to_entry([root], "root")

        assert [entry.id for entry in path] == ["root"]

    def test_walks_only_the_active_branch(self) -> None:
        """分支共享祖先只存一份；回溯只走当前叶所在的那条路径。"""
        a = msg_entry("a", None, "A")
        b = msg_entry("b", "a", "B")
        c = msg_entry("c", "b", "C")   # JWT 分支
        e = msg_entry("e", "b", "E")   # Session 分支
        d = msg_entry("d", "c", "D")

        jwt_path = path_to_entry([a, b, c, d, e], "d")
        session_path = path_to_entry([a, b, c, d, e], "e")

        assert [entry.id for entry in jwt_path] == ["a", "b", "c", "d"]
        assert [entry.id for entry in session_path] == ["a", "b", "e"]

    def test_off_path_entries_never_enter_the_path(self) -> None:
        """不在路径上的条目（含列表里排在叶之后的）不进入路径。"""
        a = msg_entry("a", None, "A")
        b = msg_entry("b", "a", "B")
        other_branch = msg_entry("x", "a", "X")

        path = path_to_entry([a, b, other_branch], "b")

        assert [entry.id for entry in path] == ["a", "b"]

    def test_missing_parent_raises_session_tree_error(self) -> None:
        """parent 链断裂 → 重放会得到残缺对话 → 宁可显式失败。"""
        orphan = msg_entry("orphan", "ghost", "O")

        with pytest.raises(SessionTreeError, match="ghost"):
            path_to_entry([orphan], "orphan")

    def test_missing_leaf_raises_session_tree_error(self) -> None:
        with pytest.raises(SessionTreeError, match="nope"):
            path_to_entry([], "nope")

    def test_cyclic_parents_raise_session_tree_error(self) -> None:
        """循环引用会让终止条件永不满足——seen 集合先查后记。"""
        first = msg_entry("first", "second", "1")
        second = msg_entry("second", "first", "2")

        with pytest.raises(SessionTreeError, match="[Cc]ycle"):
            path_to_entry([first, second], "first")
