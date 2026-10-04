"""P2 Step 2: session storage protocol and JSONL file implementation."""

from __future__ import annotations

import pytest

from minitau_agent.message import AssistantMessage, UserMessage
from minitau_agent.session.entries import MessageEntry, SessionInfoEntry
from minitau_agent.session.jsonl import SessionJsonlError, entry_to_json_line
from minitau_agent.session.storage import JsonlSessionStorage, SessionStorage


def make_user_entry(text: str = "你好") -> MessageEntry:
    return MessageEntry(message=UserMessage(content=text))


def make_assistant_entry(text: str = "你好！我是 minitau。") -> MessageEntry:
    return MessageEntry(
        message=AssistantMessage(model="fake", content=text)
    )


class TestJsonlSessionStorage:
    """storage.py：JSONL 文件实现。"""

    async def test_missing_file_reads_as_empty_session(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "sessions" / "s1.jsonl")

        assert await storage.read_all() == []

    async def test_append_creates_missing_directories(self, tmp_path) -> None:
        path = tmp_path / "deep" / "nested" / "session.jsonl"
        storage = JsonlSessionStorage(path)

        await storage.append(make_user_entry())

        assert path.exists()

    async def test_round_trip_single_entry(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        entry = make_user_entry()

        await storage.append(entry)

        assert await storage.read_all() == [entry]

    async def test_read_all_preserves_append_order(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        entries = [
            SessionInfoEntry(cwd="d:/work", title="demo"),
            make_user_entry("第一问"),
            make_assistant_entry("第一答"),
        ]

        for entry in entries:
            await storage.append(entry)

        assert await storage.read_all() == entries

    async def test_append_never_clobbers_existing_lines(self, tmp_path) -> None:
        """模拟进程重启：两个实例先后写同一文件，历史必须完好。"""
        path = tmp_path / "s.jsonl"
        first = JsonlSessionStorage(path)
        await first.append(make_user_entry("第一次"))

        second = JsonlSessionStorage(path)
        await second.append(make_user_entry("第二次"))

        entries = await second.read_all()
        assert len(entries) == 2

    async def test_u2028_line_separator_survives_round_trip(self, tmp_path) -> None:
        """U+2028 合法地出现在 JSON 字符串值内部，绝不能被当换行切开。"""
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        await storage.append(make_user_entry("第一行\u2028第二行"))

        entries = await storage.read_all()

        assert len(entries) == 1
        assert entries[0].message.content == "第一行\u2028第二行"

    async def test_last_line_without_newline_still_parses(self, tmp_path) -> None:
        path = tmp_path / "s.jsonl"
        line = entry_to_json_line(make_user_entry()).rstrip("\n")
        path.write_text(line, encoding="utf-8")

        entries = await JsonlSessionStorage(path).read_all()

        assert len(entries) == 1

    async def test_blank_lines_are_skipped(self, tmp_path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text(
            "\n" + entry_to_json_line(make_user_entry()) + "   \n",
            encoding="utf-8",
        )

        entries = await JsonlSessionStorage(path).read_all()

        assert len(entries) == 1

    async def test_corrupt_line_raises_with_line_number(self, tmp_path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text('{"type": "message"\n', encoding="utf-8")

        with pytest.raises(SessionJsonlError, match="line 1"):
            await JsonlSessionStorage(path).read_all()

    async def test_constructor_accepts_str_and_path(self, tmp_path) -> None:
        storage = JsonlSessionStorage(str(tmp_path / "s.jsonl"))

        await storage.append(make_user_entry())

        assert len(await storage.read_all()) == 1


class TestSessionStorageProtocol:
    """storage.py：协议的结构化满足。"""

    async def test_jsonl_storage_satisfies_protocol(self, tmp_path) -> None:
        storage: SessionStorage = JsonlSessionStorage(tmp_path / "s.jsonl")

        await storage.append(make_user_entry())

        assert len(await storage.read_all()) == 1
