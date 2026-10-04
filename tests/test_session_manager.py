"""P6.2：会话发现、候选装载、切换回滚和改名链尾。"""

from __future__ import annotations

import os

import pytest

from minitau_agent.message import AssistantMessage
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.session import CodingSession, CodingSessionConfig
from pi_event_helpers import assistant_done, assistant_start

manager_module = pytest.importorskip(
    "minitau_coding.session_manager", reason="等待 P6.2 SessionManager 实现"
)
SessionManager = getattr(manager_module, "SessionManager", None)
if SessionManager is None:
    pytest.skip("等待 P6.2 SessionManager 类实现", allow_module_level=True)


def provider_with_reply(text: str) -> FakeProvider:
    return FakeProvider(
        [[assistant_start(), assistant_done(AssistantMessage(content=text))]]
    )


async def create_session(cwd, session_id: str, title: str) -> CodingSession:
    config = CodingSessionConfig(cwd=cwd, provider=provider_with_reply("完成"), model="fake")
    return await CodingSession.new(config, session_id=session_id, title=title)


async def test_list_sessions_replays_titles_marks_current_and_skips_bad_file(tmp_path):
    await create_session(tmp_path, "older", "旧标题")
    await create_session(tmp_path, "newer", "新标题")
    directory = tmp_path / ".minitau" / "sessions"
    (directory / "broken.jsonl").write_text("{broken", encoding="utf-8")
    os.utime(
        directory / "older.jsonl",
        ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000),
    )
    os.utime(
        directory / "newer.jsonl",
        ns=(1_700_000_001_000_000_000, 1_700_000_001_000_000_000),
    )

    summaries = await SessionManager(tmp_path).list_sessions(current_session_id="newer")

    assert [item.session_id for item in summaries] == ["newer", "older"]
    assert [item.title for item in summaries] == ["新标题", "旧标题"]
    assert [item.is_current for item in summaries] == [True, False]


async def test_load_accepts_unique_prefix_and_rejects_ambiguous_or_unsafe_ref(tmp_path):
    await create_session(tmp_path, "shared-one", "一")
    await create_session(tmp_path, "shared-two", "二")
    manager = SessionManager(tmp_path)

    snapshot = await manager.load("shared-one")
    assert snapshot.session_id == "shared-one"
    assert snapshot.last_entry_id is not None
    assert snapshot.path.name == "shared-one.jsonl"

    with pytest.raises(ValueError, match="Ambiguous"):
        await manager.load("shared")
    with pytest.raises(ValueError):
        await manager.load("../other")
    with pytest.raises(ValueError):
        await manager.load("shared*")


async def test_latest_is_stable_when_files_have_same_timestamp(tmp_path):
    await create_session(tmp_path, "alpha", "甲")
    await create_session(tmp_path, "beta", "乙")
    directory = tmp_path / ".minitau" / "sessions"
    for session_id in ("alpha", "beta"):
        os.utime(
            directory / f"{session_id}.jsonl",
            ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000),
        )

    manager = SessionManager(tmp_path)
    first_list = await manager.list_sessions()
    second_list = await manager.list_sessions()
    assert [item.session_id for item in first_list] == [item.session_id for item in second_list]
    assert (await manager.latest()).session_id == first_list[0].session_id


async def test_failed_switch_keeps_current_session_usable(tmp_path):
    current = await create_session(tmp_path, "current", "当前")
    manager = SessionManager(tmp_path)
    bad_path = tmp_path / ".minitau" / "sessions" / "bad.jsonl"
    bad_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError):
        await current.switch_to(manager, "bad")

    assert current.session_id == "current"
    _ = [event async for event in current.prompt("继续")]
    assert [message.text for message in current.messages] == ["继续", "完成"]


async def test_successful_switch_uses_target_history_and_chain(tmp_path):
    current = await create_session(tmp_path, "current", "当前")
    target = await create_session(tmp_path, "target", "目标")
    _ = [event async for event in target.prompt("目标旧问题")]

    await current.switch_to(SessionManager(tmp_path), "target")
    assert current.session_id == "target"
    assert [message.text for message in current.messages] == ["目标旧问题", "完成"]

    _ = [event async for event in current.prompt("目标新问题")]
    path = tmp_path / ".minitau" / "sessions" / "target.jsonl"
    entries = await JsonlSessionStorage(path).read_all()
    assert entries[4].parent_id == entries[3].id
    assert [message.text for message in current.messages] == [
        "目标旧问题", "完成", "目标新问题", "完成"
    ]


async def test_start_new_switches_only_after_session_was_created(tmp_path):
    session = await create_session(tmp_path, "current", "当前")
    old_id = session.session_id
    manager = SessionManager(tmp_path)

    await session.start_new(manager, title="新的会话")

    assert session.session_id != old_id
    assert session.messages == ()
    entries = await JsonlSessionStorage(
        tmp_path / ".minitau" / "sessions" / f"{session.session_id}.jsonl"
    ).read_all()
    assert len(entries) == 2
    assert entries[0].title == "新的会话"


async def test_busy_session_rejects_switch_and_rename(tmp_path):
    session = await create_session(tmp_path, "current", "当前")
    manager = SessionManager(tmp_path)
    events = session.prompt("运行中")
    await anext(events)

    with pytest.raises(RuntimeError, match="running"):
        await session.switch_to(manager, "current")
    with pytest.raises(RuntimeError, match="running"):
        await session.rename("新标题")

    await events.aclose()
    assert session.is_running is False


async def test_loading_session_from_another_cwd_is_rejected(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    await create_session(other, "foreign", "外部")
    source = other / ".minitau" / "sessions" / "foreign.jsonl"
    target = tmp_path / ".minitau" / "sessions" / "foreign.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())

    with pytest.raises(ValueError, match="cwd"):
        await SessionManager(tmp_path).load("foreign")


async def test_rename_appends_info_and_next_message_uses_new_parent(tmp_path):
    session = await create_session(tmp_path, "renamed", "旧标题")
    await session.rename("新标题")

    path = tmp_path / ".minitau" / "sessions" / "renamed.jsonl"
    before = await JsonlSessionStorage(path).read_all()
    assert [entry.type for entry in before] == [
        "session_info", "model_change", "session_info"
    ]
    assert before[-1].parent_id == before[1].id
    assert before[-1].title == "新标题"

    _ = [event async for event in session.prompt("下一问")]
    after = await JsonlSessionStorage(path).read_all()
    assert after[3].parent_id == before[-1].id
    summaries = await SessionManager(tmp_path).list_sessions()
    assert summaries[0].title == "新标题"
