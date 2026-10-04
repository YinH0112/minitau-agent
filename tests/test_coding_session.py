"""P6.1 的应用层验收：装配、持久化恢复和 Provider 所有权。"""

from __future__ import annotations

import pytest

from minitau_agent.message import AssistantMessage
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from pi_event_helpers import assistant_done, assistant_start

# P6.1 的实现由学习者编写。模块出现后，这些测试会自动开始执行。
session_module = pytest.importorskip(
    "minitau_coding.session", reason="等待 P6.1 CodingSession 实现"
)
CodingSession = session_module.CodingSession
CodingSessionConfig = session_module.CodingSessionConfig


def reply(text: str):
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


async def test_new_session_assembles_context_and_persists_prompt(tmp_path):
    (tmp_path / "AGENTS.md").write_text("项目规则：先读再改", encoding="utf-8")
    provider = FakeProvider([reply("完成")])
    config = CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake")

    session = await CodingSession.new(config, session_id="lesson-1", title="第一问")
    assert session.session_id == "lesson-1"
    assert session.messages == ()
    assert session.is_running is False

    events = [event async for event in session.prompt("请检查项目")]
    assert events[-1].type == "agent_end"
    assert [message.text for message in session.messages] == ["请检查项目", "完成"]

    model, system, messages, tools = provider.calls[0]
    assert model == "fake"
    assert "项目规则：先读再改" in system
    assert {tool.name for tool in tools} >= {"read", "write", "edit", "bash"}
    assert messages[0].text == "请检查项目"

    path = tmp_path / ".minitau" / "sessions" / "lesson-1.jsonl"
    entries = await JsonlSessionStorage(path).read_all()
    assert [entry.type for entry in entries] == [
        "session_info", "model_change", "message", "message"
    ]
    assert entries[0].title == "第一问"
    assert entries[0].cwd == str(tmp_path)
    assert entries[1].parent_id == entries[0].id
    assert entries[2].parent_id == entries[1].id
    assert entries[3].parent_id == entries[2].id


async def test_resume_restores_context_and_appends_to_original_chain(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "lesson-2.jsonl"
    first_provider = FakeProvider([reply("答一")])
    first_config = CodingSessionConfig(cwd=tmp_path, provider=first_provider, model="fake")
    first = await CodingSession.new(first_config, session_id="lesson-2", title="第一问")
    _ = [event async for event in first.prompt("问一")]

    second_provider = FakeProvider([reply("答二")])
    second_config = CodingSessionConfig(cwd=tmp_path, provider=second_provider, model="fake")
    resumed = await CodingSession.resume(second_config, path=path)
    assert resumed.session_id == "lesson-2"
    assert [message.text for message in resumed.messages] == ["问一", "答一"]

    _ = [event async for event in resumed.prompt("问二")]
    assert [message.text for message in second_provider.calls[0][2]] == ["问一", "答一", "问二"]
    entries = await JsonlSessionStorage(path).read_all()
    assert [entry.type for entry in entries] == [
        "session_info", "model_change", "message", "message", "message", "message"
    ]
    assert entries[4].parent_id == entries[3].id
    assert entries[5].parent_id == entries[4].id


async def test_session_borrows_provider_without_closing_it(tmp_path):
    class TrackingProvider(FakeProvider):
        def __init__(self):
            super().__init__([reply("完成")])
            self.close_count = 0

        async def aclose(self):
            self.close_count += 1

    provider = TrackingProvider()
    config = CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake")
    session = await CodingSession.new(config, session_id="lesson-3", title="问")
    _ = [event async for event in session.prompt("问")]
    assert provider.close_count == 0


async def test_resume_rejects_empty_session_file(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "empty.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    config = CodingSessionConfig(cwd=tmp_path, provider=FakeProvider([]), model="fake")

    with pytest.raises(ValueError, match="empty"):
        await CodingSession.resume(config, path=path)
