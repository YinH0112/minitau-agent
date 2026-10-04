"""Read → Eval → Print → Loop。"""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic

from minitau_agent.async_iterators import closing_async_iterator
from minitau_agent.events import AgentEvent
from minitau_coding.commands import CommandContext, CommandResult, dispatch_input
from minitau_coding.configuration import Ask, ConfigurationCancelled, configure_provider
from minitau_coding.interrupts import (
    ActiveInterrupt,
    ActiveOperationCancelled,
    catch_active_interrupts,
    catch_idle_interrupts,
    supervise_active,
)
from minitau_coding.provider_runtime import ProviderRuntime
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
    provider_runtime: ProviderRuntime | None = None,
    ask: Ask | None = None,
    ask_secret: Ask | None = None,
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
        provider_runtime=provider_runtime,
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

        if result.configuration_requested:
            if provider_runtime is None or ask is None or ask_secret is None:
                emit("此入口没有配置向导输入接口。", True)
                continue
            try:
                # 输入 key 时 Ctrl+C 应立即取消向导，不是向 Agent 发 cancel()。
                with catch_idle_interrupts():
                    selected = configure_provider(
                        provider_runtime,
                        result.provider_to_configure,
                        ask=ask,
                        ask_secret=ask_secret,
                        show=lambda text: emit(text, False),
                    )
                # 向导结束后，磁盘操作仍走既有活动任务清理机制。
                state = ActiveInterrupt(session.cancel)
                with catch_active_interrupts(state):
                    await supervise_active(session.set_provider(selected), state)
                if state.exit_after_cleanup:
                    return
            except ConfigurationCancelled:
                emit("已取消配置，继续使用当前供应商。", False)
            except ActiveOperationCancelled:
                return
            except (ValueError, RuntimeError, OSError) as exc:
                emit(f"配置未应用到当前会话：{exc}", True)
            else:
                emit(f"当前供应商：{session.provider_name}；模型：{session.status.model}", False)
            continue

        if not result.handled and result.prompt is not None:
            continue

        if result.error is not None:
            emit(result.error, True)
        elif result.message is not None:
            emit(result.message, False)

        if result.exit_requested:
            return
