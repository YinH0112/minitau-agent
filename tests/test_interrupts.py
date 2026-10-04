"""P7.3：终端中断只请求取消，活动操作仍负责完整收尾。"""

from __future__ import annotations

import pytest

from minitau_ai.fake import FakeProvider
from minitau_coding.repl import run_repl
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager

interrupts = pytest.importorskip("minitau_coding.interrupts", reason="等待 P7.3 中断状态实现")
ActiveInterrupt = interrupts.ActiveInterrupt


def test_first_active_interrupt_requests_cancel_only_once() -> None:
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1

    state = ActiveInterrupt(cancel)
    state.request()

    assert calls == 1
    assert state.requested is True
    assert state.exit_after_cleanup is False

    state.request()

    assert calls == 1
    assert state.exit_after_cleanup is True


async def test_one_idle_keyboard_interrupt_keeps_repl_open(tmp_path) -> None:
    provider = FakeProvider([])
    session = await CodingSession.new(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake"),
        session_id="current",
        title="",
    )
    manager = SessionManager(tmp_path)
    inputs = iter([KeyboardInterrupt(), "/exit"])
    output: list[tuple[str, bool]] = []

    def read_line() -> str:
        value = next(inputs)
        if isinstance(value, BaseException):
            raise value
        return value

    await run_repl(
        session,
        manager,
        read_line=read_line,
        emit=lambda text, err: output.append((text, err)),
    )

    assert provider.calls == []
    assert session.is_running is False
    assert any("Ctrl+C" in text for text, _ in output)


async def test_two_quick_idle_interrupts_exit_without_running_agent(tmp_path) -> None:
    provider = FakeProvider([])
    session = await CodingSession.new(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake"),
        session_id="current",
        title="",
    )
    manager = SessionManager(tmp_path)
    reads = 0

    def read_line() -> str:
        nonlocal reads
        reads += 1
        raise KeyboardInterrupt

    await run_repl(
        session,
        manager,
        read_line=read_line,
        emit=lambda text, err: None,
    )

    assert reads == 2
    assert provider.calls == []
