"""Read → Eval → Print → Loop。"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from minitau_agent.async_iterators import closing_async_iterator
from minitau_agent.events import AgentEvent
from minitau_coding.commands import CommandContext, CommandResult, dispatch_input
from minitau_coding.interrupts import (
    ActiveInterrupt,
    ActiveOperationCancelled,
    catch_active_interrupts,
    catch_idle_interrupts,
    supervise_active,
)
from minitau_coding.rendering import EventRenderer
from minitau_coding.session import CodingSession
from minitau_coding.session_manager import SessionManager

type ReadLine = Callable[[], str]
type Emit = Callable[[str, bool], None]


async def run_repl(
    session: CodingSession,
    manager: SessionManager,
    *,
    read_line: ReadLine,
    emit: Emit,
    write: Emit | None = None,
    initial_prompt: str | None = None,
) -> None:
    renderer = EventRenderer(
        emit_line=emit,
        emit_chunk=write if write is not None else emit,
        streaming=write is not None,
    )

    # None 表示现在没有活动操作，正处于等待输入的状态。
    active_state: ActiveInterrupt | None = None

    async def on_event(event: AgentEvent) -> None:
        # 极小的竞态：Ctrl+C 可能恰好发生在流开始消费之前。
        # 那时 Harness 还没建立本轮取消令牌，第一次 cancel() 可能无效；
        # 收到首个事件后再送一次取消请求即可覆盖这个窗口。
        if active_state is not None and active_state.requested:
            session.cancel()

        renderer.render(event)

    async def consume_prompt(text: str) -> None:
        stream = session.prompt(text)
        async with closing_async_iterator(stream):
            async for event in stream:
                await on_event(event)

    context = CommandContext(
        session=session,
        manager=manager,
        on_event=on_event,
    )

    async def process_line(line_1: str) -> CommandResult:
        result_1 = await dispatch_input(line_1, context)
        if not result_1.handled and result_1.prompt is not None:
            await consume_prompt(result_1.prompt)
        return result_1

    if initial_prompt:
        state = ActiveInterrupt(session.cancel)
        try:
            with catch_active_interrupts(state):
                await supervise_active(
                    consume_prompt(initial_prompt),
                    state,
                )
        except ActiveOperationCancelled:
            return
        finally:
            renderer.finish()

        if state.exit_after_cleanup:
            return

    last_idle_interrupt: float | None = None

    while True:
        try:
            with catch_idle_interrupts():
                line = read_line()
        except EOFError:
            return
        except KeyboardInterrupt:
            now = monotonic()
            if last_idle_interrupt is not None and now - last_idle_interrupt <= 2.0:
                return

            last_idle_interrupt = now
            emit("再按一次 Ctrl+C 退出；也可以输入 /exit。", True)
            continue

        last_idle_interrupt = None
        state = ActiveInterrupt(session.cancel)

        try:
            with catch_active_interrupts(state):
                result = await supervise_active(
                    process_line(line),
                    state,
                )
        except ActiveOperationCancelled:
            return
        finally:
            renderer.finish()

        if state.exit_after_cleanup:
            return

        if not result.handled and result.prompt is not None:
            continue

        if result.error is not None:
            emit(result.error, True)
        elif result.message is not None:
            emit(result.message, False)

        if result.exit_requested:
            return