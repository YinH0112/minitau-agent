"""Session tree traversal over append-only entries."""

from __future__ import annotations

from minitau_agent.session.entries import SessionEntry


class SessionTreeError(ValueError):
    """Raised when the session entry tree is inconsistent."""
    # 每个模块拥有自己的错误类型，错误类型是模块 API 的一部分

def entries_by_id(entries: list[SessionEntry]) -> dict[str, SessionEntry]:
    """Build an id -> entry index, rejecting duplicate ids"""
    result: dict[str, SessionEntry] = {}
    for entry in entries:
        if entry.id in result:
            raise SessionTreeError(f"Duplicate session entry id: {entry.id}")
        result[entry.id] = entry
    return result

def path_to_entry(entries: list[SessionEntry], leaf_id: str) -> list[SessionEntry]:
    """Walk the parent chain from a leaf back to the root, root-first order. """
    by_id = entries_by_id(entries)
    path: list[SessionEntry] = []
    seen: set[str] = set()
    current_id: str | None = leaf_id

    while current_id is not None:
        if current_id in seen:
            raise SessionTreeError(f"Cycle detected at session entry: {current_id}")
        seen.add(current_id)
        entry = by_id.get(current_id)
        if entry is None:
            raise SessionTreeError(f"Misssing session entry: {current_id}")
        path.append(entry)
        current_id = entry.parent_id

    path.reverse()
    return path