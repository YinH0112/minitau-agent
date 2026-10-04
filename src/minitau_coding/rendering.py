"""把 Agent 事件转换成终端输出，不负责执行 Agent。"""

from __future__ import annotations

from collections.abc import Callable

from minitau_agent.events import (
    AgentEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    MessageEndEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from minitau_agent.message import AssistantMessage
from minitau_agent.provider_events import TextDeltaEvent

# 参数 1 是文字；参数 2 表示是否写入 stderr。
type Emit = Callable[[str, bool], None]


class EventRenderer:
    """把 Agent 循环广播出来的事件（minitau_agent/events.py 里那些 pydantic 事件）翻译成终端输出"""
    def __init__(
        self,
        *,
        emit_line: Emit,
        emit_chunk: Emit,
        streaming: bool,
    ) -> None:
        self._emit_line = emit_line
        self._emit_chunk = emit_chunk
        self._streaming = streaming

        # 当前这一条助手消息已经展示过的正文。
        self._printed_text = ""

        # 增量输出不会自动换行；记录是否需要在消息结束时补换行。
        self._line_open = False

        # 供 --print 模式决定进程退出码。
        self.exit_code = 0

    def render(self, event: AgentEvent) -> None:
        """消费一个事件；事件到来时即可调用，不等待整轮结束。"""
        if isinstance(event, MessageUpdateEvent):
            nested = event.assistant_message_event

            if self._streaming and isinstance(nested, TextDeltaEvent) and nested.delta:
                # delta 是“新增加的文字”；不能打印 nested.partial.text，
                # 因为 partial 是到当前为止的完整快照。
                self._emit_chunk(nested.delta, False)
                self._printed_text += nested.delta
                self._line_open = True
            return

        if isinstance(event, MessageEndEvent):
            if isinstance(event.message, AssistantMessage):
                self._finish_assistant_message(event.message)
            return

        if isinstance(event, ToolExecutionStartEvent):
            self._close_line()
            self._emit_line(f"Tool: {event.tool_name} started", True)
            return

        if isinstance(event, ToolExecutionEndEvent):
            self._close_line()
            status = "failed" if event.is_error else "finished"
            self._emit_line(f"Tool: {event.tool_name} {status}", True)
            return

        if isinstance(event, CompactionStartEvent):
            self._close_line()
            self._emit_line("Compacting context...", True)
            return

        if isinstance(event, CompactionEndEvent) and event.reason == "auto" and event.aborted:
            self._close_line()
            self._emit_line(
                f"Compaction failed: {event.error_message}",
                True,
            )

    def _finish_assistant_message(self, message: AssistantMessage) -> None:
        """
        拿最终消息 message.text（所有 TextContent 块拼起来的字符串），和已打印的 _printed_text 比对
        """
        final_text = message.text

        # 没流式输出过（--print 模式，或 provider 只发 done 不发 delta）
        if not self._streaming or not self._printed_text:
            # --print 模式只看最终消息；DemoProvider 也走这里，
            # 因为它没有发送 text_delta。
            if final_text:
                self._emit_line(final_text, False)
        elif final_text.startswith(self._printed_text):
            # 某些 Provider 的最后一个增量可能未送到展示层。
            # 最终消息是完整结果，因此只补尚未打印的尾部。
            remaining = final_text[len(self._printed_text) :]
            if remaining:
                self._emit_chunk(remaining, False)
            self._close_line()
        else:
            # 流式文字和最终消息不一致，无法仅靠追加文字修正旧输出。
            # 明确提示并显示最终版本，避免悄悄丢失正确结果。
            self._close_line()
            self._emit_line("Final message differs from streamed text.", True)
            if final_text:
                self._emit_line(final_text, False)

        if message.stop_reason in {"error", "aborted"}:
            self._close_line()
            self._emit_line(
                message.error_message or message.stop_reason,
                True,
            )
            self.exit_code = 130 if message.stop_reason == "aborted" else 1

        # 下一条助手消息重新计数。一次 Agent 运行中可能有多条助手消息，
        # 例如“先说明要读文件”→工具执行→“根据文件给出答案”。
        self._printed_text = ""

    def _close_line(self) -> None:
        """上一条正文是增量输出的、结尾没有换行，
        后面要打 stderr 状态行或换 emit_line 时，先把它收尾，避免状态行粘在正文后面 """
        if self._line_open:
            self._emit_chunk("\n", False)
            self._line_open = False

    def finish(self) -> int:
        """一个 prompt 或 compact 的事件迭代器结束后调用。 """
        self._close_line()
        self._printed_text = ""
        return self.exit_code