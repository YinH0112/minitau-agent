"""P6.6 第一段：交互命令切换当前 Provider 的请求模型。"""

from __future__ import annotations

import pytest

from minitau_agent.message import AssistantMessage
from minitau_ai.fake import FakeProvider
from minitau_coding.commands import CommandContext, dispatch_input
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager
from pi_event_helpers import assistant_done, assistant_start

if not hasattr(CodingSession, "set_model"):
    pytest.skip("等待 P6.6 CodingSession.set_model 实现", allow_module_level=True)


def reply(text: str):
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


async def make_context(tmp_path):
    provider = FakeProvider([reply("第一答"), reply("第二答")])
    session = await CodingSession.new(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="model-a"),
        session_id="current",
        title="",
    )

    async def on_event(_event):
        return None

    context = CommandContext(
        session=session,
        manager=SessionManager(tmp_path),
        on_event=on_event,
    )
    return context, provider


async def test_model_command_changes_next_request_without_erasing_history(tmp_path) -> None:
    context, provider = await make_context(tmp_path)
    _ = [event async for event in context.session.prompt("第一问")]

    changed = await dispatch_input("/model model-b", context)
    _ = [event async for event in context.session.prompt("第二问")]

    assert changed.error is None
    assert context.session.status.model == "model-b"
    assert [call[0] for call in provider.calls] == ["model-a", "model-b"]
    assert [message.text for message in context.session.messages] == [
        "第一问", "第一答", "第二问", "第二答"
    ]


async def test_model_query_does_not_change_history_or_call_provider(tmp_path) -> None:
    context, provider = await make_context(tmp_path)

    result = await dispatch_input("/model", context)

    assert result.error is None
    assert "model-a" in (result.message or "")
    assert context.session.messages == ()
    assert provider.calls == []


async def test_model_switch_is_rejected_while_agent_is_running(tmp_path) -> None:
    context, provider = await make_context(tmp_path)
    stream = context.session.prompt("正在运行")
    await anext(stream)

    result = await dispatch_input("/model model-b", context)

    assert result.error is not None
    assert context.session.status.model == "model-a"
    await stream.aclose()
    assert provider.calls == []


async def test_empty_model_name_is_rejected(tmp_path) -> None:
    context, _provider = await make_context(tmp_path)

    result = await dispatch_input("/model    ", context)

    assert result.error is None  # 无参数表示查询，而不是切换为空模型。
    assert context.session.status.model == "model-a"
