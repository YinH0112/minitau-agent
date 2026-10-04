"""REPL 的 Ctrl+C 状态与临时 SIGINT 处理器。"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import Any

# signal.signal(signal.SIGINT, handler)   # 捕获 Ctrl+C
# signal.signal(signal.SIGTERM, handler)  # 捕获 kill/关窗

@dataclass(slots=True)
class ActiveInterrupt:
    """记录一次活动操作收到的中断请求。"""

    cancel: Callable[[], None]
    requested: bool = False # 是否已请求过中断
    exit_after_cleanup: bool = False # 清理完成后是否应该退出程序

    def request(self) -> None:
        if not self.requested:
            self.requested = True
            self.cancel() # 第 1 次：只踩刹车，不关机
        else:
            # 第二次中断不越过 finally；由 REPL 在操作结束后退出。
            self.exit_after_cleanup = True # 第 2 次：标记“清理完就走人”

class ActiveOperationCancelled(Exception):
    """第二次 Ctrl+C 后，活动任务已取消且清理已完成。"""


class CleanupTimeoutError(RuntimeError):
    """任务收到强制取消后，仍未能确认它已完成清理。"""


@contextmanager
def catch_active_interrupts(state: ActiveInterrupt) -> Iterator[None]:
    """仅在模型请求或命令运行期间接管 Ctrl+C。"""
    def on_sigint(_signum: int, _frame: FrameType | None) -> None:
        state.request()
        
    # 暂时让 on_sigint 接管 Ctrl+C，同时记住原来是谁处理，以便用完后恢复
    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@contextmanager
def catch_idle_interrupts() -> Iterator[None]:
    """读取键盘输入时，让 Ctrl+C 表现为 KeyboardInterrupt。"""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


async def supervise_active[T](
        operation: Coroutine[Any, Any, T],
        state: ActiveInterrupt,
        *,
        poll_interval: float = 0.05,
        grace_seconds: float = 5.0,
        cleanup_seconds: float = 5.0,
) -> T:
    """监督一次 prompt 或命令，必要时升级取消力度。"""
    task = asyncio.create_task(operation)
    loop = asyncio.get_running_loop()
    deadline : float | None = None

    try:
        while True:
            if task.done():
                # await 会取回结果；如果任务本身失败，异常也会在这里传播。
                return await task

            if state.requested:
                # 覆盖“按 Ctrl+C 时任务尚未启动”的时间窗口。
                # session.cancel() 只是把令牌置为 cancelled，重复调用安全。
                state.cancel()

            if state.exit_after_cleanup:
                if deadline is None:
                    # 第二次 Ctrl+C 后才开始计算宽限期。
                    deadline = loop.time() + grace_seconds

                if loop.time() >= deadline:
                    # 协作式取消未让操作结束，升级为 asyncio 任务取消。
                    task.cancel()

                    # asyncio.wait 在超时后会返回，不会替我们声称任务已结束。
                    done, _pending = await asyncio.wait(
                        {task},
                        timeout=cleanup_seconds,
                    )
                    if task not in done:
                        raise CleanupTimeoutError(
                            "Active operation did not finish cleanup after task cancellation."
                        )

                    try:
                        return await task
                    except asyncio.CancelledError as exc:
                        # 已经确认任务结束；把 asyncio 的底层异常转换为
                        # REPL 能识别的“清理后退出”结果。
                        raise ActiveOperationCancelled from exc

            # 定期醒来检查中断状态，同时让活动任务运行。
            await asyncio.wait({task}, timeout=poll_interval)

    except asyncio.CancelledError:
        # 监督器本身被外层取消时，也要回收它创建的子任务。
        task.cancel()
        done, _pending = await asyncio.wait(
            {task},
            timeout=cleanup_seconds,
        )
        if task not in done:
            raise CleanupTimeoutError(
                "Active operation did not finish cleanup after outer cancellation."
            ) from None
        raise

