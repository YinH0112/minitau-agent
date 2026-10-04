"""P7.1：最小顺序 REPL 的离线验收。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import minitau_coding.cli as cli_module
from minitau_agent.message import AssistantMessage
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.cli import app
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager
from pi_event_helpers import assistant_done, assistant_start

repl_module = pytest.importorskip("minitau_coding.repl", reason="等待 P7.1 REPL 实现")
if not hasattr(repl_module, "run_repl"):
    pytest.skip("等待 P7.1 run_repl 实现", allow_module_level=True)
run_repl = repl_module.run_repl


def reply(text: str):
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


def scripted_input(*lines: str):
    iterator = iter(lines)

    def read_line() -> str:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise EOFError from exc

    return read_line


async def make_session(tmp_path, *streams):
    provider = FakeProvider(streams)
    config = CodingSessionConfig(cwd=tmp_path, provider=provider, model="fake")
    session = await CodingSession.new(config, session_id="current", title="初始会话")
    return session, provider, SessionManager(tmp_path)


async def test_three_prompts_share_one_session_and_finish_before_next_input(tmp_path):
    session, provider, manager = await make_session(
        tmp_path, reply("答一"), reply("答二"), reply("答三")
    )
    output: list[tuple[str, bool]] = []

    await run_repl(
        session,
        manager,
        read_line=scripted_input("问一", "问二", "问三"),
        emit=lambda text, err: output.append((text, err)),
    )

    assert len(provider.calls) == 3
    assert [message.text for message in session.messages] == [
        "问一", "答一", "问二", "答二", "问三", "答三"
    ]
    assert [text for text, err in output if not err] == ["答一", "答二", "答三"]


async def test_initial_prompt_runs_before_reading_keyboard(tmp_path):
    session, provider, manager = await make_session(tmp_path, reply("第一答"))
    reads = 0

    def read_line() -> str:
        nonlocal reads
        reads += 1
        raise EOFError

    output: list[tuple[str, bool]] = []
    await run_repl(
        session,
        manager,
        read_line=read_line,
        emit=lambda text, err: output.append((text, err)),
        initial_prompt="第一问",
    )

    assert reads == 1
    assert len(provider.calls) == 1
    assert session.messages[0].text == "第一问"
    assert ("第一答", False) in output


async def test_commands_switch_session_and_resume_existing_history(tmp_path):
    session, provider, manager = await make_session(tmp_path, reply("答一"), reply("答二"))
    output: list[tuple[str, bool]] = []

    await run_repl(
        session,
        manager,
        read_line=scripted_input("问一", "/new", "/resume current", "问二", "/exit"),
        emit=lambda text, err: output.append((text, err)),
    )

    assert session.session_id == "current"
    assert [message.text for message in session.messages] == ["问一", "答一", "问二", "答二"]
    assert len(provider.calls) == 2
    entries = await JsonlSessionStorage(
        tmp_path / ".minitau" / "sessions" / "current.jsonl"
    ).read_all()
    assert [entry.type for entry in entries] == [
        "session_info", "model_change", "message", "message", "message", "message"
    ]


async def test_double_slash_is_sent_to_model_and_exit_does_not_call_model(tmp_path):
    session, provider, manager = await make_session(tmp_path, reply("收到"))
    output: list[tuple[str, bool]] = []

    await run_repl(
        session,
        manager,
        read_line=scripted_input("//help", "/exit", "不会执行"),
        emit=lambda text, err: output.append((text, err)),
    )

    assert len(provider.calls) == 1
    assert session.messages[0].text == "/help"


async def test_eof_exits_without_calling_model(tmp_path):
    session, provider, manager = await make_session(tmp_path)

    await run_repl(
        session,
        manager,
        read_line=scripted_input(),
        emit=lambda text, err: None,
    )

    assert provider.calls == []
    assert session.is_running is False


def test_cli_without_print_rejects_non_tty_input(tmp_path):
    result = CliRunner().invoke(app, ["--cwd", str(tmp_path)])

    assert result.exit_code == 2
    assert "TTY" in result.output or "--print" in result.output


def test_interactive_entry_closes_provider_once_on_eof(tmp_path, monkeypatch):
    class TrackingProvider(FakeProvider):
        def __init__(self):
            super().__init__([])
            self.close_count = 0

        async def aclose(self):
            self.close_count += 1

    provider = TrackingProvider()
    monkeypatch.setattr(cli_module, "create_provider", lambda _name: (provider, "fake"))

    def end_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", end_input)
    cli_module.run_interactive_mode(
        initial_prompt=None,
        provider_name="fake",
        model_name=None,
        cwd=tmp_path,
        resume=None,
        continue_latest=False,
        context_window=None,
    )

    assert provider.close_count == 1
