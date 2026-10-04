"""P6.6: model configuration persists on the active session path."""

from __future__ import annotations

import asyncio

import pytest
from typer.testing import CliRunner

from minitau_agent.message import AssistantMessage
from minitau_agent.session.entries import ModelChangeEntry
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.cli import app
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager
from pi_event_helpers import assistant_done, assistant_start


def reply(text: str):
    return [assistant_start(), assistant_done(AssistantMessage(content=text))]


async def test_model_choice_survives_restart_and_changes_next_request(tmp_path):
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=FakeProvider([]),
            model="alpha",
            context_window_tokens=8192,
        ),
        session_id="saved-model",
        title="",
    )
    await session.set_model("beta")

    snapshot = await SessionManager(tmp_path).load("saved-model")
    assert snapshot.model_change is not None
    assert snapshot.model_change.model == "beta"
    assert snapshot.model_change.context_window_tokens == 8192

    provider = FakeProvider([reply("ok")])
    resumed = CodingSession.from_snapshot(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="factory-default"),
        snapshot,
    )
    assert resumed.status.model == "beta"
    assert resumed.status.context_window_tokens == 8192
    _ = [event async for event in resumed.prompt("next")]
    assert provider.calls[0][0] == "beta"


async def test_explicit_launch_model_and_window_override_saved_choice(tmp_path):
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=FakeProvider([]),
            model="stored",
            context_window_tokens=8192,
        ),
        session_id="overrides",
        title="",
    )
    snapshot = await SessionManager(tmp_path).load(session.session_id)

    resumed = CodingSession.from_snapshot(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=FakeProvider([]),
            model="explicit",
            model_explicit=True,
            context_window_tokens=4096,
            context_window_explicit=True,
        ),
        snapshot,
    )
    assert resumed.status.model == "explicit"
    assert resumed.status.context_window_tokens == 4096
    assert snapshot.model_change is not None
    assert snapshot.model_change.model == "stored"  # 仅覆盖本次进程，不改旧日志


async def test_branching_back_restores_model_on_selected_path(tmp_path):
    path = tmp_path / ".minitau" / "sessions" / "branches.jsonl"
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=FakeProvider([reply("first")]),
            model="alpha",
            context_window_tokens=8192,
        ),
        session_id="branches",
        title="",
    )
    _ = [event async for event in session.prompt("question")]
    first_answer_id = (await JsonlSessionStorage(path).read_all())[-1].id
    await session.set_model("beta")

    await session.branch_to(first_answer_id)
    assert session.status.model == "alpha"
    await session.set_model("gamma")

    snapshot = await SessionManager(tmp_path).load("branches")
    assert snapshot.model_change is not None
    assert snapshot.model_change.model == "gamma"
    assert [message.text for message in snapshot.messages] == ["question", "first"]
    changes = [
        entry.model
        for entry in await JsonlSessionStorage(path).read_all()
        if isinstance(entry, ModelChangeEntry)
    ]
    assert changes == ["alpha", "beta", "gamma"]


async def test_failed_model_append_keeps_runtime_and_log_unchanged(tmp_path, monkeypatch):
    session = await CodingSession.new(
        CodingSessionConfig(cwd=tmp_path, provider=FakeProvider([]), model="alpha"),
        session_id="write-failure",
        title="",
    )
    path = tmp_path / ".minitau" / "sessions" / "write-failure.jsonl"
    before = await JsonlSessionStorage(path).read_all()
    original_append = JsonlSessionStorage.append

    async def failing_append(self, entry):
        if isinstance(entry, ModelChangeEntry) and entry.model == "beta":
            raise OSError("disk full")
        await original_append(self, entry)

    monkeypatch.setattr(JsonlSessionStorage, "append", failing_append)
    with pytest.raises(OSError, match="disk full"):
        await session.set_model("beta")

    assert session.status.model == "alpha"
    assert await JsonlSessionStorage(path).read_all() == before
    assert session.is_running is False


async def test_switching_sessions_restores_each_sessions_model(tmp_path):
    provider = FakeProvider([])
    first = await CodingSession.new(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="alpha"),
        session_id="first",
        title="",
    )
    await CodingSession.new(
        CodingSessionConfig(cwd=tmp_path, provider=provider, model="gamma"),
        session_id="second",
        title="",
    )
    manager = SessionManager(tmp_path)

    await first.switch_to(manager, "second")
    assert first.status.model == "gamma"
    await first.switch_to(manager, "first")
    assert first.status.model == "alpha"


def test_cli_restores_saved_provider_and_respects_explicit_model(tmp_path, monkeypatch):
    asyncio.run(
        CodingSession.new(
            CodingSessionConfig(
                cwd=tmp_path,
                provider=FakeProvider([]),
                provider_name="demo",
                model="stored",
            ),
            session_id="cli-choice",
            title="",
        )
    )
    created: list[tuple[str | None, FakeProvider]] = []

    def fake_factory(name):
        provider = FakeProvider([reply("ok")])
        created.append((name, provider))
        return provider, "factory"

    monkeypatch.setattr("minitau_coding.cli.create_provider", fake_factory)
    runner = CliRunner()
    base = ["--print", "question", "--cwd", str(tmp_path), "--resume", "cli-choice"]

    restored = runner.invoke(app, base)
    overridden = runner.invoke(app, [*base, "--model", "explicit"])

    assert restored.exit_code == 0
    assert overridden.exit_code == 0
    assert [name for name, _provider in created] == ["demo", "demo"]
    assert [provider.calls[0][0] for _name, provider in created] == [
        "stored", "explicit"
    ]
