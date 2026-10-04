"""P7.3 后半：第二次 Ctrl+C 后监督活动任务的限时清理。"""

from __future__ import annotations

import asyncio

import pytest

from minitau_coding import interrupts

if not hasattr(interrupts, "supervise_active"):
    pytest.skip("等待 P7.3 活动任务监督实现", allow_module_level=True)

ActiveInterrupt = interrupts.ActiveInterrupt
ActiveOperationCancelled = interrupts.ActiveOperationCancelled
supervise_active = interrupts.supervise_active


async def test_cancel_before_operation_starts_is_replayed() -> None:
    stopped = asyncio.Event()
    running = False
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if running:
            stopped.set()

    state = ActiveInterrupt(cancel)
    state.request()  # 此时操作尚未开始，第一次 cancel 没有作用。

    async def operation() -> str:
        nonlocal running
        running = True
        await stopped.wait()
        return "stopped"

    result = await supervise_active(
        operation(),
        state,
        poll_interval=0.001,
        grace_seconds=0.02,
        cleanup_seconds=0.1,
    )

    assert result == "stopped"
    assert calls >= 2


async def test_second_interrupt_forces_cancel_and_waits_for_finally() -> None:
    state = ActiveInterrupt(lambda: None)
    state.request()
    state.request()
    cleaned = False

    async def operation() -> None:
        nonlocal cleaned
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned = True

    with pytest.raises(ActiveOperationCancelled):
        await supervise_active(
            operation(),
            state,
            poll_interval=0.001,
            grace_seconds=0.01,
            cleanup_seconds=0.1,
        )

    assert cleaned is True
