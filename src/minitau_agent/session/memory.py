"""In-memory session state reconstruction"""

from __future__ import annotations

from dataclasses import dataclass

from minitau_agent.message import AgentMessage, UserMessage
from minitau_agent.session.entries import (
    CompactionEntry,
    LeafEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
)
from minitau_agent.session.tree import path_to_entry

_UNSET_LEAF_ID = object()

@dataclass(frozen=True, slots=True)
class SessionState:
    """
    In-memory session state reconstructed from append-only entries
    这是“投影”而非“复制”：
    - 存储层 Entry 带 id/parent_id/timestamp/type 等元数据，喂模型完全不需要
    - SessionState 只保留模型真正需要的东西：消息列表 + 当前生效的元数据
    - frozen/slots 让它成为真正的值对象：创建即定型、不可变、内存紧凑
    """

    # 按文件顺序重放得到消息列表， 给provider的message参数
    messages: list[AgentMessage]
    context_entry_ids: tuple[str, ...] = ()

    # 最后一个sessioninfoentry
    session_info: SessionInfoEntry | None = None
    model_change: ModelChangeEntry | None = None

    @classmethod
    def from_entries(
        cls,
        entries: list[SessionEntry],
        *,
        leaf_id: str | None | object = _UNSET_LEAF_ID,
    ) -> SessionState:
        """Reconstruct session state from a linear list of entries
        消息全部收集且保序（只取内层 AgentMessage，丢掉信封），
        元信息只留列表中最后一条（last-write-wins，不看时间戳）
        指定 leaf_id 时只重放根到该节点的路径
        """


        if leaf_id is _UNSET_LEAF_ID:
            # 兼容旧调用：未传 leaf_id 时仍按文件顺序重放。
            replay_entries = entries # 全量重放（文件里所有条目
        elif leaf_id is None:
            # 显式传 None 表示根节点之前的空路径。
            replay_entries = []  # 空路径：一条都不放
        elif isinstance(leaf_id, str):
            # 只取目标节点的祖先及目标本身；其他分支不会进入上下文。
            replay_entries = path_to_entry(entries, leaf_id)
        else:
            raise TypeError("leaf_id must be a string or None")

        message_rows: list[tuple[str, AgentMessage]] = []
        session_info: SessionInfoEntry | None = None
        model_change: ModelChangeEntry | None = None

        for entry in replay_entries:
            match entry:
                case MessageEntry(message=msg):
                    message_rows.append((entry.id, msg))
                case SessionInfoEntry():
                    session_info = entry
                case ModelChangeEntry():
                    # 仅重放所选路径，最后一条配置覆盖此前配置。
                    model_change = entry
                case CompactionEntry():
                    message_rows = _apply_compaction(message_rows, entry)
                case LeafEntry():
                    # 这是分支位置标记，不是对话消息。
                    continue

        return cls(
            messages=[message for _entry_id, message in message_rows],
            context_entry_ids=tuple(entry_id for entry_id, _ in message_rows),
            session_info=session_info,
            model_change=model_change,
        )

                

def _apply_compaction(
        message_rows: list[tuple[str, AgentMessage]],
        entry: CompactionEntry,
) -> list[tuple[str, AgentMessage]]:
    """压缩替换：命中 id 的行被一行摘要取代，其余保序保留。 """
    replaced_ids = set(entry.replaces_entry_ids)
    # replaces_entry_ids 是 list，查 in 是 O(n)；转 set 后是 O(1)。消息多的时候差别明显
    retained: list[tuple[str, AgentMessage]] = []
    inserted_summary = False

    for entry_id, message in message_rows:
        if entry_id not in replaced_ids:
            retained.append((entry_id, message))
            continue
        if not inserted_summary:
            # 命中后在第一条插入摘要 compact
            summary = UserMessage(content=_format_compaction_summary(entry.summary))
            retained.append((entry.id, summary))
            inserted_summary = True

    if not inserted_summary:
        retained.append((entry.id, UserMessage(content=_format_compaction_summary(entry.summary))))
    return retained

def _format_compaction_summary(summary: str) -> str:
    return f"Previous conversation summary:\n{summary}"
