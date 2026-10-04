"""查找和读取当前项目的会话文件；不管理 Provider 或活动 Harness。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from minitau_agent.message import AgentMessage
from minitau_agent.session.entries import (
    LeafEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
)
from minitau_agent.session.memory import SessionState
from minitau_agent.session.storage import JsonlSessionStorage


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    path: Path
    messages: tuple[AgentMessage, ...]
    entry_ids: tuple[str, ...]
    last_entry_id: str
    session_info: SessionInfoEntry
    updated_at_ns: int
    model_change: ModelChangeEntry | None = None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str
    updated_at_ns: int
    is_current: bool


class SessionManager:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd.resolve()
        self.sessions_dir = self.cwd / ".minitau" / "sessions"

    def create(self) -> str:
        """生成尚未使用的 ID；真正写文件仍由 CodingSession.new() 完成。"""
        while True:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            session_id = f"{stamp}-{uuid4().hex[:6]}"
            if not (self.sessions_dir / f"{session_id}.jsonl").exists():
                return session_id

    async def load(self, ref: str) -> SessionSnapshot:
        """
        会话 ID 智能匹配与安全加载器
        按完整 ID 或唯一前缀定位，并验证整个会话。"""
        # 规定会话 ID 的引用 ref 必须纯粹由字母（大小写）、数字、下划线 _ 和连字符 - 组成
        if not re.fullmatch(r"[A-Za-z0-9_-]+", ref):
            raise ValueError(f"Invalid session reference: {ref!r}")

        exact = self.sessions_dir / f"{ref}.jsonl"
        if exact.is_file():
            path = exact
        else:
            # 如果完整文件不存在，说明用户输入的可能是一个“前缀缩写”
            matches = sorted(self.sessions_dir.glob(f"{ref}*.jsonl"))
            if not matches:
                raise ValueError(f"No session matches {ref!r}")
            if len(matches) > 1:
                # 匹配到多个，产生歧义
                names = ", ".join(item.stem for item in matches)
                raise ValueError(f"Ambiguous session {ref!r}: {names}")
            path = matches[0]

        return await self._read_snapshot(path)

    async def latest(self) -> SessionSnapshot:
        """自动定位并加载“最近一次使用（最新）”的会话快照"""
        summaries = await self.list_sessions()
        if not summaries:
            raise ValueError("No sessions found yet; run a prompt first")
        return await self.load(summaries[0].session_id)

    async def list_sessions(
        self,
        *,
        current_session_id: str | None = None,
    ) -> list[SessionSummary]:
        summaries: list[SessionSummary] = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                snapshot = await self._read_snapshot(path)
            except ValueError:
                # 一个损坏文件不应使其他会话无法列出。
                continue

            summaries.append(
                SessionSummary(
                    session_id=snapshot.session_id,
                    title=snapshot.session_info.title or "",
                    updated_at_ns=snapshot.updated_at_ns,
                    is_current=snapshot.session_id == current_session_id,
                )
            )

        # 先按更新时间降序；时间完全相同时按 ID 排，结果才稳定。
        summaries.sort(key=lambda item: (-item.updated_at_ns, item.session_id))
        return summaries

    async def _read_snapshot(self, path: Path) -> SessionSnapshot:
        """把磁盘上的 .jsonl 日志读出来，执行极其严苛的“数据完整性与拓扑校验”，
        确保没有任何损坏或篡改，然后重放历史状态并打包成一个干净的快照"""
        try:
            entries = await JsonlSessionStorage(path).read_all()
            # 异步读取该 .jsonl 文件中的所有行，反序列化为条目对象列表
            updated_at_ns = path.stat().st_mtime_ns
            # st_mtime 通常是以秒为单位的浮点数，而这里的 st_mtime_ns 获取的是纳秒
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read session {path.stem}: {exc}") from exc

        if not entries:
            raise ValueError(f"Session {path.stem} is empty")


        entries_by_id: dict[str, SessionEntry] = {}

        for entry in entries:
            if entry.id in entries_by_id:
                raise ValueError(f"Duplicate entry ID in session {path.stem}")

            if entry.parent_id is not None and entry.parent_id not in entries_by_id:
                raise ValueError(f"Broken parent_id in session {path.stem}")

            if isinstance(entry, LeafEntry):
                # 标记只能指向此前已经写入的真实节点，不能指向另一个标记。
                target = entries_by_id.get(entry.entry_id) if entry.entry_id is not None else None
                if target is None or isinstance(target, LeafEntry):
                    raise ValueError(f"Invalid leaf target in session {path.stem}")

            entries_by_id[entry.id] = entry

        latest_leaf_index = next(
            (
                index
                for index in range(len(entries) - 1, -1, -1)
                if isinstance(entries[index], LeafEntry)
            ),
            None,
        )

        if latest_leaf_index is None:
            # 旧文件没有分支标记，保持线性恢复。
            state = SessionState.from_entries(entries)
            last_entry_id = entries[-1].id
        else:
            marker = entries[latest_leaf_index]
            assert isinstance(marker, LeafEntry)

            active_id = marker.entry_id
            if active_id is None:
                raise ValueError(f"Invalid leaf target in session {path.stem}")

            # 标记之后，只有接在当前活动节点上的条目才能推动链尾。
            for entry in entries[latest_leaf_index + 1 :]:
                if entry.parent_id == active_id:
                    active_id = entry.id

            state = SessionState.from_entries(entries, leaf_id=active_id)
            last_entry_id = active_id

        # 标题和 cwd 是整个会话的元信息。改名记录即使不在所选
        # 对话分支的路径上，也应继续生效。
        info = next(
            (entry for entry in reversed(entries) if isinstance(entry, SessionInfoEntry)),
            None,
        )
        if info is None:
            raise ValueError(f"Session {path.stem} has no session info")

        if info.cwd is not None and Path(info.cwd).resolve() != self.cwd:
            raise ValueError(f"Session {path.stem} belongs to a different cwd: {info.cwd}")

        return SessionSnapshot(
            session_id=path.stem,
            path=path,
            messages=tuple(state.messages),
            entry_ids=state.context_entry_ids,
            last_entry_id=last_entry_id,
            session_info=info,
            updated_at_ns=updated_at_ns,
            model_change=state.model_change,
        )
