""" JSONL serialization for session entries."""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from minitau_agent.session.entries import SessionEntry

_SESSION_ENTRY_ADAPTER: TypeAdapter[SessionEntry] = TypeAdapter(SessionEntry)

# TypeAdapter 是关键工具。Pydantic 的 model_validate
# 只能用在 BaseModel 子类上，但 SessionEntry 是个联合类型别名，不是类。
# TypeAdapter 就是为"给任意类型（含联合类型）提供校验/序列化能力"而生的。
# ValidationError 是 Pydantic 的校验异常

# 对象 → 一行
def entry_to_json_line(entry: SessionEntry) -> str:
    """ Serialize one session entry to a single JSONL line. """
    return _SESSION_ENTRY_ADAPTER.dump_json(entry, exclude_none=True).decode() + "\n"

class SessionJsonlError(ValueError):
    """Raised when a session JSONL line cannot be decoded."""

# 一行 → 对象
def entry_from_json_line(line: str, *, line_number: int | None = None) -> SessionEntry:
    """Deserialize one JSONL line into a session entry."""
    location = f" on line {line_number}" if line_number is not None else ""
    try:
        return _SESSION_ENTRY_ADAPTER.validate_json(line)
    except (ValidationError, ValueError) as e:
        raise SessionJsonlError(f"Invalid session entry{location}: {e}") from e

# 多行 → 列表
def entries_from_json_lines(lines: list[str]) -> list[SessionEntry]:
    """Deserialize non-empty JSONL lines in order."""
    entries: list[SessionEntry] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        entries.append(entry_from_json_line(line, line_number=index))
    return entries

# Agent 对话产生 AgentMessage
#         ↓  MessageEntry 包装（加上 id / parent_id / timestamp）
#         ↓  entry_to_json_line 编码
# {"id":"a1","timestamp":1.78e9,"type":"message","message":{...}}   ← 追加写入文件
#         ↓  entry_from_json_line 解码
#         ↓  还原成 MessageEntry
# 下次启动可完整恢复会话

# 每产生一条消息 → 立刻编码成一行 → 追加到文件末尾
