"""Translate raw provider events into canonical assistant message events.

5.2 产出 raw（线上发生了什么），本层产出 canonical（消费者看到什么）。
参考 tau stream.py 的 canonicalize_provider_stream。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from minitau_agent.message import AssistantMessage, TextContent, ToolCall
from minitau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from minitau_ai._provider_events import (
    RawStreamEnd,
    RawStreamError,
    RawTextDelta,
    RawThinkingDelta,
    RawToolCall,
)


def _snapshot(message: AssistantMessage) -> AssistantMessage:
    """每个事件深拷贝一次 partial：消费者手里的快照不能被后续 delta 改写。"""
    return message.model_copy(deep=True)
# 深拷贝全部内容另建 浅拷贝只有第一层另建

async def _end_active_block(
    partial: AssistantMessage,
    index: int | None,
) -> AsyncIterator[AssistantMessageEvent]:
    """换块前给活跃 text 块发 TextEnd——消费者的生命周期钩子。"""
    if index is None:
        return
    block = partial.content[index]
    if isinstance(block, TextContent):
        yield TextEndEvent(
            content_index=index,
            content=block.text,
            partial=_snapshot(partial),
        )

def _finish_reason(value: str | None, *, has_tools: bool) -> str:
    """厂商 finish_reason 方言 -> minitau 三值。事实（有无 toolcall）优先于声明。"""
    if has_tools or value in {"tool_calls", "tool_use", "toolUse"}:
        return "toolUse"
    if value in {"length", "max_tokens", "MAX_TOKENS", "incomplete"}:
        return "length"
    return "stop"

async def canonicalize_provider_stream(
    source: AsyncIterator[object],
    *,
    provider: str,
    model: str,
) -> AsyncIterator[AssistantMessageEvent]:
    """
    raw 事件流 -> canonical 事件流。维护 partial 快照与块状态机。
    把各大模型底层杂乱无章的原始数据碎片（Raw Events），转换成内容块事件流（Canonical Events）
    同时维护一个“正在生成中的消息快照（Partial Snapshot）
    """
    partial = AssistantMessage(provider=provider, model=model) # 正在实时生长的 AI 回复草稿
    active_index: int | None = None # 告诉程序当前正在追加的文字属于partial.content 列表里的第几个块
    active_kind: str | None = None # 状态机指针：记录 AI 当前这一秒正在写什么类型的内容
    started = False # 记录“AI 是否已经开始输出”
    terminal = False # 这个流是否已经合法地结束了

    async for event in source:
        # 网络流是随时间陆续到来的：中间存在网络等待，必须用异步等待（await）；
        # 保证应用不卡死：在等待大模型吐下一个字的空隙里，
        # 允许程序去处理 UI 渲染、检查用户是否点了取消等
        if not started:
            started = True
            yield AssistantStartEvent(partial=_snapshot(partial))

        if isinstance(event, RawTextDelta):
            if active_kind != "text":
                # 1. 把之前的块结束掉（发出 TextEndEvent 等）
                async for end_event in _end_active_block(partial, active_index):
                    yield end_event
                # 2. 在 partial 里新建一个空文本块 TextContent(text="")
                new_index = len(partial.content)
                active_index = new_index
                active_kind = "text"
                
                partial.content.append(TextContent(text=""))
                
                # 3.正式通知 UI：我要开始一段新的文本了
                yield TextStartEvent(
                    content_index=new_index, partial=_snapshot(partial)
                )
            assert active_index is not None
            block = partial.content[active_index]
            assert isinstance(block, TextContent)
            block.text += event.delta
            yield TextDeltaEvent(
                content_index=active_index,
                delta=event.delta,
                partial=_snapshot(partial),
            )
        elif isinstance(event, RawThinkingDelta):
            # minitau 尚无 ThinkingContent 块：思考增量暂时丢弃。
            # 扩展点：message.py 加 ThinkingContent 后，这里照 text 分支开块。
            continue
        elif isinstance(event, RawToolCall):
            async for end_event in _end_active_block(partial, active_index):
                yield end_event
            active_index = None
            active_kind = None
            index = len(partial.content)
            tool_call = ToolCall(id=event.id, name=event.name, arguments=event.arguments)
            partial.content.append(tool_call)
            # OpenAI 系的 toolcall 在解析层拼装完成后整体到达（无增量），
            # canonical 层没有需要流式的 JSON 分片，Start/End 背靠背发出。
            yield ToolCallStartEvent(content_index=index, partial=_snapshot(partial))
            yield ToolCallEndEvent(
                content_index=index, tool_call=tool_call, partial=_snapshot(partial)
            )
        elif isinstance(event, RawStreamEnd):
            if event.finish_reason not in {
                None, "stop", "tool_calls", "tool_use", "toolUse",
                "length", "max_tokens", "MAX_TOKENS", "incomplete",
            }:
                error = partial.model_copy(deep=True)
                error.stop_reason = "error"
                error.error_message = f"Provider stopped with reason: {event.finish_reason}"
                yield AssistantErrorEvent(reason="error", error=error)
                terminal = True
                break
            async for end_event in _end_active_block(partial, active_index):
                yield end_event
            active_index = None
            active_kind = None

            # 内容权威是 partial（保持流式到达顺序），end 只提供元数据
            final = partial.model_copy(deep=True)
            final.stop_reason = _finish_reason(  # type: ignore[assignment]
                event.finish_reason, has_tools=bool(final.tool_calls)
            )
            yield AssistantDoneEvent(reason=final.stop_reason, message=final)  # type: ignore[arg-type]
            terminal = True
        elif isinstance(event, RawStreamError):
            error = partial.model_copy(deep=True)
            error.stop_reason = "aborted" if event.aborted else "error"
            error.error_message = event.message
            yield AssistantErrorEvent(reason=error.stop_reason, error=error)
            terminal = True
            break

    if not started:
        yield AssistantStartEvent(partial=_snapshot(partial))
    if not terminal:
        # 流死了但没发终结事件：有内容也不能当成功，包成 error 上报
        error = partial.model_copy(deep=True)
        error.stop_reason = "error"
        error.error_message = "Provider stream ended without a terminal event"
        yield AssistantErrorEvent(reason="error", error=error)
