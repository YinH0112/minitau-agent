"""P6.4：命令分流、参数传递和会话操作的离线验收。"""

from __future__ import annotations

import pytest

from minitau_agent.message import AssistantMessage
from minitau_ai.fake import FakeProvider
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager
from pi_event_helpers import assistant_done, assistant_start

commands_module = pytest.importorskip("minitau_coding.commands", reason="等待 P6.4 commands 实现")
if not hasattr(commands_module, "dispatch_input"):
    pytest.skip("等待 P6.4 dispatch_input 实现", allow_module_level=True)

CommandContext = commands_module.CommandContext
dispatch_input = commands_module.dispatch_input


def reply(text: str):
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


async def make_context(tmp_path, *streams):
    provider = FakeProvider(streams)
    config = CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake")
    session = await CodingSession.new(config, session_id="current", title="起始标题")
    manager = SessionManager(tmp_path)
    events = []

    async def on_event(event):
        events.append(event)

    return CommandContext(session=session, manager=manager, on_event=on_event), events


async def test_plain_text_and_double_slash_are_prompts(tmp_path):
    context, _ = await make_context(tmp_path)

    plain = await dispatch_input("请检查文件", context)
    escaped = await dispatch_input("//help", context)

    assert plain.handled is False
    assert plain.prompt == "请检查文件"
    assert escaped.handled is False
    assert escaped.prompt == "/help"
    assert context.session.messages == ()


async def test_unknown_command_never_becomes_model_prompt(tmp_path):
    context, _ = await make_context(tmp_path)

    result = await dispatch_input("/does-not-exist", context)

    assert result.handled is True
    assert result.prompt is None
    assert result.error is not None
    assert context.session.messages == ()


async def test_help_is_generated_from_command_registry(tmp_path):
    context, _ = await make_context(tmp_path)

    result = await dispatch_input("/help", context)

    assert result.error is None
    assert "/resume" in (result.message or "")
    assert "/compact" in (result.message or "")


async def test_name_preserves_chinese_and_internal_spaces(tmp_path):
    context, _ = await make_context(tmp_path)

    result = await dispatch_input("/name 我的 新 会话", context)

    assert result.error is None
    summaries = await context.manager.list_sessions()
    assert summaries[0].title == "我的 新 会话"
    assert context.session.messages == ()


async def test_resume_missing_argument_does_not_switch(tmp_path):
    context, _ = await make_context(tmp_path)

    result = await dispatch_input("/resume   ", context)

    assert result.error is not None
    assert context.session.session_id == "current"


async def test_new_and_sessions_use_current_session(tmp_path):
    context, _ = await make_context(tmp_path)

    created = await dispatch_input("/new", context)
    listed = await dispatch_input("/sessions", context)

    assert created.error is None
    assert context.session.session_id != "current"
    assert "current" in (listed.message or "")
    assert context.session.session_id in (listed.message or "")


async def test_status_reports_current_session_without_calling_model(tmp_path):
    context, _ = await make_context(tmp_path)

    result = await dispatch_input("/status", context)

    assert result.error is None
    assert "current" in (result.message or "")
    assert "fake" in (result.message or "")
    assert context.session.messages == ()


async def test_compact_forwards_events_to_sink(tmp_path):
    context, seen = await make_context(tmp_path, reply("原回答"), reply("摘要"))
    _ = [event async for event in context.session.prompt("原问题")]

    result = await dispatch_input("/compact", context)

    assert result.error is None
    assert [event.type for event in seen] == ["compaction_start", "compaction_end"]
    assert [message.text for message in context.session.messages] == [
        "Previous conversation summary:\n摘要"
    ]


async def test_compact_rejects_unexpected_argument(tmp_path):
    context, seen = await make_context(tmp_path)

    result = await dispatch_input("/compact now", context)

    assert result.error is not None
    assert seen == []


async def test_running_session_rejects_mutating_command(tmp_path):
    context, _ = await make_context(tmp_path, reply("完成"))
    events = context.session.prompt("正在运行")
    await anext(events)

    result = await dispatch_input("/new", context)

    assert result.error is not None
    assert context.session.session_id == "current"
    await events.aclose()


async def test_exit_returns_intent_without_closing_session(tmp_path):
    context, _ = await make_context(tmp_path)

    result = await dispatch_input("/exit", context)

    assert result.exit_requested is True
    assert context.session.is_running is False
