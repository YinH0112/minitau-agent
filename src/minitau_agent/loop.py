"""Minitau agent loop: cycles the provider and tools while emitting agent events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence

from minitau_agent.async_iterators import closing_async_iterator
from minitau_agent.context_window import ContextWindowExceeded
from minitau_agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from minitau_agent.message import (
    AgentMessage,
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
)
from minitau_agent.provider import CancellationToken, ModelProvider
from minitau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
)
from minitau_agent.tools import AgentTool, AgentToolResult


async def run_agent_loop(
        *, # 强制使用关键字传参，提高可读性
        provider: ModelProvider,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        prompts: Sequence[AgentMessage] = (),
        max_turns: int | None = None,
        signal: CancellationToken | None = None,
        get_steering_messages: Callable[[], Sequence[AgentMessage]] | None = None,
        get_follow_up_messages: Callable[[], Sequence[AgentMessage]] | None = None,
        before_model_request: Callable[[], AsyncIterator[AgentEvent]] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the provider/tool loop and emit agent events.
     这是一个异步生成器函数。它不返回最终结果，而是像流水线一样，
     边执行边 yield 出事件（如消息开始、工具调用、消息结束等），供上层 UI 或日志系统实时消费"""
    # 1.入口段
    # 将本轮的 prompts 追加到全局 messages 历史中，并记录在 new_messages 中。
    new_messages = list(prompts)
    if prompts:
        messages.extend(prompts)

    yield AgentStartEvent()
    # Agent开始工作
    yield TurnStartEvent()
    # 第一轮对话
    for prompt in prompts:
        yield MessageStartEvent(message=prompt) # 消息开始
        yield MessageEndEvent(message=prompt) # 消息结束

    # 2.防御段 max_turns < 1
    if max_turns is not None and max_turns < 1:
        error = _error_message(model, "max_turns must be at least 1")
        messages.append(error)
        new_messages.append(error)
        yield MessageStartEvent(message=error)
        yield MessageEndEvent(message=error)
        yield TurnEndEvent(message=error)
        yield AgentEndEvent(messages=new_messages)
        return

    # 3.记录循环前变量
    tool_by_name = {tool.name: tool for tool in tools} # 查工具
    turn = 1 # 轮数计数器
    first_turn = True
    # 入口段已经 yield 过一次 TurnStartEvent 了，第一轮循环里不能再 yield，
    # 否则测试一的事件序列会多一个 turn_start。第 2 轮起才由循环自己开轮
    pending = tuple(get_steering_messages() if get_steering_messages else ())
    # 拉一次steering

    # 特性	Steering（引导）	                    Follow-up（后续追问）
    # 语义	改变当前任务的方向	                    在当前任务完成后追加新任务
    # 注入时机	当前轮工具执行完后，下次 LLM 调用前	整个 Agent 循环完全停下后
    # 典型场景	"等等，先不要改那个文件"	        "顺便再帮我写个测试"
    # 处理队列	内层循环消费	                    外层循环消费

    # 4.双层循环
    while True:
        has_more_tools = True
        # 内层循环负责处理单轮推理 - 工具执行 - 再推理的
        while has_more_tools or pending:
            # 注入位置在 provider 调用之前——steering 的语义就是“在下一轮开始前插话”。
            # 注入完立刻清空 pending = ()，防止下轮重复注入
            if not first_turn:
                yield TurnStartEvent()
            first_turn = False

            for message in pending:
                messages.append(message)
                new_messages.append(message)
                yield MessageStartEvent(message=message)
                yield MessageEndEvent(message=message)
            pending = ()

            # max_turn 检查
            if max_turns is not None and turn > max_turns:
                error = _error_message(model, f"Agent stopped after max_turns ={max_turns}")
                messages.append(error)
                new_messages.append(error)
                yield MessageStartEvent(message=error)
                yield MessageEndEvent(message=error)
                yield TurnEndEvent(message=error)
                yield AgentEndEvent(messages=new_messages)
                return

            if signal is not None and signal.is_cancelled():
                aborted = _aborted_message(model)
                messages.append(aborted)
                new_messages.append(aborted)
                yield MessageStartEvent(message=aborted)
                yield MessageEndEvent(message=aborted)
                yield AgentEndEvent(messages=new_messages)
                return
            if before_model_request is not None:
                try:
                    async for event in before_model_request():
                        yield event
                except ContextWindowExceeded as exc:
                    error = _error_message(model, str(exc))
                    messages.append(error)
                    new_messages.append(error)
                    yield MessageStartEvent(message=error)
                    yield MessageEndEvent(message=error)
                    yield TurnEndEvent(message=error)
                    yield AgentEndEvent(messages=new_messages)
                    return
            if signal is not None and signal.is_cancelled():
                aborted = _aborted_message(model)
                messages.append(aborted)
                new_messages.append(aborted)
                yield MessageStartEvent(message=aborted)
                yield MessageEndEvent(message=aborted)
                yield AgentEndEvent(messages=new_messages)
                return

            # provider调用
            assistant = None
            assistant_events = _assistant_events(
                provider=provider,
                model=model,
                system=system,
                messages=_provider_context(messages),
                tools=tools,
                signal=signal,
            )

            # 本层创建 _assistant_events，就负责在退出时关闭它。
            # yield 暂停期间，上层可能直接关闭 Agent Loop；显式关闭才能继续向下释放 Provider 流。
            async with closing_async_iterator(assistant_events):
                async for event in assistant_events:
                    yield event
                    if isinstance(event, MessageEndEvent) and isinstance(
                        event.message, AssistantMessage
                    ):
                        assistant = event.message
            # 防御分支
            if assistant is None:
                assistant = (
                    _aborted_message(model) if signal is not None and signal.is_cancelled()
                    else _error_message(model, "Provider produced no assistant message")
                )
                yield MessageStartEvent(message=assistant)
                yield MessageEndEvent(message=assistant)
            # assistant进入历史
            #无论 assistant 是正常消息还是上面合成的错误消息，都会被追加到：
            messages.append(assistant) # messages：完整对话历史（用于后续轮次的上下文）
            new_messages.append(assistant) # new_messages：本轮新增消息集合（用于事件通知和持久化

            if assistant.stop_reason in {"error", "aborted"}:
                yield TurnEndEvent(message=assistant)
                yield AgentEndEvent(messages=new_messages)
                return

            # 工具执行
            tool_results: list[ToolResultMessage] =[]
            calls = list(assistant.tool_calls)
            has_more_tools = bool(calls)
            for call in calls:
                async for event in _execute_tool_call(call, tool_by_name, signal):
                    yield event
                    if isinstance(event, MessageEndEvent) and isinstance(
                            event.message, ToolResultMessage
                    ):
                        tool_results.append(event.message)
                        messages.append(event.message)
                        new_messages.append(event.message)
            # 结束再拉一次steering
            yield TurnEndEvent(message=assistant, tool_results=tool_results)
            if signal is not None and signal.is_cancelled():
                yield AgentEndEvent(messages=new_messages)
                return
            turn +=1
            pending = tuple(get_steering_messages() if get_steering_messages else ())

        # 支持多轮对话/追问。当模型回答完当前问题后，
        # get_follow_up_messages
        # 可以返回新的用户输入，让 Agent 继续工作，而不需要重新调用 run_agent_loop
        follow_ups = tuple(get_follow_up_messages() if get_follow_up_messages else ())
        if follow_ups:
            pending = follow_ups
            continue
        break

    yield AgentEndEvent(messages=new_messages)



def _error_message(model: str, message: str) -> AssistantMessage:
    """用于构造一个标准化的“错误占位”助手消息对象用于构造一个标准化的“错误占位”
    被追加到了 messages 列表中，但其 content=[] 意味着对下一轮模型调用没有语义贡献"""
    return AssistantMessage(
        model=model,
        content=[],
        stop_reason="error",
        error_message=message,
    )


def _aborted_message(model: str) -> AssistantMessage:
    return AssistantMessage(model=model, stop_reason="aborted", error_message="Operation aborted")

async def _assistant_events(
        *,
        provider: ModelProvider,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None,
) -> AsyncIterator[AgentEvent]:
    # 将底层模型提供商（Provider）返回的原始流式事件，统一转换为上层 Agent 框架所需的标准事件格式
    source: AsyncIterator[AssistantMessageEvent] = provider.stream_response(
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        signal=signal,
    ) # 调用具体的模型提供商接口，获取一个原始的异步事件迭代器
    started = False

    # Provider 返回的 source 是本层持有的子流。外层提前结束时先关闭 source，
    # 让 Provider 有机会释放 HTTP 响应；普通 async for 只负责取事件，不负责这个关闭约定。
    async with closing_async_iterator(source):
        async for event in source:
            if isinstance(event, AssistantStartEvent):
                started = True
                yield MessageStartEvent(message=event.partial)
            elif isinstance(event, AssistantDoneEvent):
                if not started:
                    yield MessageStartEvent(message=event.message)
                yield MessageEndEvent(message=event.message)
            elif isinstance(event, AssistantErrorEvent):
                if not started:
                    yield MessageStartEvent(message=event.error)
                yield MessageEndEvent(message=event.error)
            else:
                yield MessageUpdateEvent(
                    message=event.partial,
                    assistant_message_event=event,
                )


def _provider_context(messages: list[AgentMessage]) -> list[AgentMessage]:
    """
    上下文过滤器
    同时满足三个条件的message才会被过滤
    属于AssistantMessage,stop_reason属于error或aborted,content为空
    """
    return [
        message
        for message in messages
        if not (
            isinstance(message, AssistantMessage)
            and message.stop_reason in {"error", "aborted"}
            and not message.content
        )
    ]

async def _execute_tool_call(
        call: ToolCall,
        tools: Mapping[str, AgentTool],
        signal: CancellationToken | None,
) -> AsyncIterator[AgentEvent]:
    yield ToolExecutionStartEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        args=call.arguments,
    )
    if signal is not None and signal.is_cancelled():
        result, is_error = AgentToolResult(content="Operation aborted"), True
    else:
        tool = tools.get(call.name)
        if tool is None:
            result, is_error = AgentToolResult(content=f"Tool {call.name} not found"), True
        else:
            result, is_error = await _run_tool(tool, call, signal)

    yield ToolExecutionEndEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        result=result,
        is_error=is_error,
    )
    message = ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=result.content,
        is_error=is_error,
    )
    yield MessageStartEvent(message=message)
    yield MessageEndEvent(message=message)

async def _wait_for_cancel(signal: CancellationToken) -> None:
    while not signal.is_cancelled():
        await asyncio.sleep(0.05)


async def _run_tool(
    tool: AgentTool, call: ToolCall, signal: CancellationToken | None
) -> tuple[AgentToolResult, bool]:
    # 普通工具异常转成错误结果；外层 task.cancel() 的 CancelledError 保持向上传播。
    try:
        if signal is None:
            return await tool.execute(call.arguments), False
        execute = asyncio.create_task(tool.execute(call.arguments, signal=signal))
        watch = asyncio.create_task(_wait_for_cancel(signal))
        try:
            done, _ = await asyncio.wait(
                {execute, watch},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if watch in done or signal.is_cancelled():
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(execute),
                        timeout=5,
                    )
                    return result, True
                except TimeoutError:
                    return AgentToolResult(content="Operation aborted"), True

            return execute.result(), False
        finally:
            # execute 和 watch 是本函数创建的独立任务。只取消等待它们的父任务，
            # 不会自动取消这两个子任务；正常、超时、协作取消和外层取消都必须经过这里。
            watch.cancel()
            if not execute.done():
                execute.cancel()
            # cancel() 只是发出请求；gather() 等待工具自己的 finally 真正跑完。
            # 子任务的取消异常作为结果收集，不覆盖父任务原有的取消或返回值。
            await asyncio.gather(execute, watch, return_exceptions=True)
    except Exception as exc:
        return AgentToolResult(content=str(exc)), True
