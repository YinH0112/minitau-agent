"""Session storage protocol and JSONL file implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from minitau_agent.session.entries import SessionEntry
from minitau_agent.session.jsonl import entries_from_json_lines, entry_to_json_line


class SessionStorage(Protocol):
    """ Append-only session storage interface"""

    async def append(self, entry: SessionEntry) -> None:
        """ Append one entry to storage"""
        ...

    async def read_all(self) -> list[SessionEntry]:
        """ Read all entries in storage order"""
        ...


class JsonlSessionStorage:
    """ Local append-only JSONL session storage"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def append(self, entry: SessionEntry) -> None:
        """ Append one entry, creating parent directories if needed"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(entry_to_json_line(entry))
            # # 离开 with 块后，file 会自动关闭，无需手动调用 file.close()

    async def read_all(self) -> list[SessionEntry]:
        """ Read all entries in files order. Missing files are empty sessions"""
        if not self.path.exists():
            return []
        return entries_from_json_lines(
            self.path.read_text(encoding="utf-8").split("\n")
        )