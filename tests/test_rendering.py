"""P7.2：事件展示既不重复文本，也不遗漏仅有 done 的回答。"""

from __future__ import annotations

import pytest

from minitau_agent.events import (
    CompactionEndEvent,
    CompactionStartEvent,
    MessageEndEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from minitau_agent.message import AssistantMessage, TextContent
from minitau_agent.provider_events import TextDeltaEvent
from minitau_agent.tools import AgentToolResult

rendering = pytest.importorskip("minitau_coding.rendering", reason="等待 P7.2 renderer 实现")
EventRenderer = rendering.EventRenderer


def capture_renderer(*, streaming: bool = True):
    output: list[tuple[str, str]] = []

    def emit_line(text: str, is_error: bool) -> None:
        output.append(("stderr-line" if is_error else "stdout-line", text))

    def emit_chunk(text: str, is_error: bool) -> None:
        output.append(("stderr-chunk" if is_error else "stdout-chunk", text))

    renderer = EventRenderer(
        emit_line=emit_line,
        emit_chunk=emit_chunk,
        streaming=streaming,
    )
    return renderer, output


def delta(text: str) -> MessageUpdateEvent:
    partial = AssistantMessage(content=[TextContent(text=text)])
    return MessageUpdateEvent(
        message=partial,
        assistant_message_event=TextDeltaEvent(
            content_index=0,
            delta=text,
            partial=partial,
        ),
    )


def done(text: str, *, reason: str = "stop", error: str | None = None) -> MessageEndEvent:
    return MessageEndEvent(
        message=AssistantMessage(
            content=[TextContent(text=text)] if text else [],
            stop_reason=reason,
            error_message=error,
        )
    )


def test_streaming_deltas_and_final_text_are_printed_once() -> None:
    renderer, output = capture_renderer()
    renderer.render(delta("你"))
    renderer.render(delta("好"))
    renderer.render(done("你好"))
    renderer.finish()

    assert output == [
        ("stdout-chunk", "你"),
        ("stdout-chunk", "好"),
        ("stdout-chunk", "\n"),
    ]


def test_final_message_adds_missing_tail() -> None:
    renderer, output = capture_renderer()
    renderer.render(delta("你"))
    renderer.render(done("你好"))
    renderer.finish()

    assert output == [
        ("stdout-chunk", "你"),
        ("stdout-chunk", "好"),
        ("stdout-chunk", "\n"),
    ]


def test_multiple_text_blocks_follow_content_order() -> None:
    renderer, output = capture_renderer()
    first = AssistantMessage(content=[TextContent(text="先读")])
    second = AssistantMessage(
        content=[TextContent(text="先读"), TextContent(text="再答")]
    )
    renderer.render(
        MessageUpdateEvent(
            message=first,
            assistant_message_event=TextDeltaEvent(
                content_index=0, delta="先读", partial=first
            ),
        )
    )
    renderer.render(
        MessageUpdateEvent(
            message=second,
            assistant_message_event=TextDeltaEvent(
                content_index=1, delta="再答", partial=second
            ),
        )
    )
    renderer.render(MessageEndEvent(message=second))
    renderer.finish()

    assert output == [
        ("stdout-chunk", "先读"),
        ("stdout-chunk", "再答"),
        ("stdout-chunk", "\n"),
    ]


def test_done_only_provider_uses_final_text() -> None:
    renderer, output = capture_renderer()
    renderer.render(done("离线回答"))
    renderer.finish()

    assert output == [("stdout-line", "离线回答")]


def test_print_mode_ignores_deltas_and_uses_final_text() -> None:
    renderer, output = capture_renderer(streaming=False)
    renderer.render(delta("你"))
    renderer.render(delta("好"))
    renderer.render(done("你好"))
    renderer.finish()

    assert output == [("stdout-line", "你好")]


def test_error_keeps_partial_text_and_reports_error_to_stderr() -> None:
    renderer, output = capture_renderer()
    renderer.render(delta("已生成"))
    renderer.render(done("已生成", reason="error", error="请求失败"))
    renderer.finish()

    assert output == [
        ("stdout-chunk", "已生成"),
        ("stdout-chunk", "\n"),
        ("stderr-line", "请求失败"),
    ]


def test_tool_status_between_two_assistant_messages() -> None:
    renderer, output = capture_renderer()
    renderer.render(delta("正在读取"))
    renderer.render(done("正在读取"))
    renderer.render(ToolExecutionStartEvent(tool_call_id="1", tool_name="read", args={}))
    renderer.render(
        ToolExecutionEndEvent(
            tool_call_id="1",
            tool_name="read",
            result=AgentToolResult(content=[]),
            is_error=False,
        )
    )
    renderer.render(done("读取完成"))
    renderer.finish()

    assert output == [
        ("stdout-chunk", "正在读取"),
        ("stdout-chunk", "\n"),
        ("stderr-line", "Tool: read started"),
        ("stderr-line", "Tool: read finished"),
        ("stdout-line", "读取完成"),
    ]


def test_compaction_progress_is_reported_to_stderr() -> None:
    renderer, output = capture_renderer()
    renderer.render(CompactionStartEvent(reason="auto"))
    renderer.render(
        CompactionEndEvent(reason="auto", aborted=True, error_message="预算不足")
    )
    renderer.finish()

    assert output == [
        ("stderr-line", "Compacting context..."),
        ("stderr-line", "Compaction failed: 预算不足"),
    ]
