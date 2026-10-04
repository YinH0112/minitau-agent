"""应用内配置的离线验收：真实 HTTP 适配器用 MockTransport，绝不调用远端。"""

from __future__ import annotations

import json
import signal

import httpx
import pytest
from typer.testing import CliRunner

from minitau_agent.session.entries import ModelChangeEntry
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from minitau_coding.cli import _open_session, app, run_interactive_mode
from minitau_coding.commands import CommandContext, dispatch_input
from minitau_coding.configuration import ConfigurationCancelled, configure_provider
from minitau_coding.provider_runtime import MissingProviderCredentials, ProviderRuntime
from minitau_coding.provider_settings import ProviderSettings, ProviderSettingsStore
from minitau_coding.repl import run_repl
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager


@pytest.fixture(autouse=True)
def clear_provider_env(monkeypatch):
    for name in (
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "OPENAI_API_KEY",
        "MINITAU_API_KEY",
        "MINITAU_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def inputs(*values):
    iterator = iter(values)

    def read(_prompt=""):
        try:
            return next(iterator)
        except StopIteration:
            raise EOFError from None

    return read


def save(runtime, name="deepseek", model="chosen-model", key="local-test-key"):
    runtime.store.save_provider(
        name,
        ProviderSettings(base_url=f"https://{name}.example/v1", model=model),
        api_key=key,
    )


@pytest.fixture
def mock_http(monkeypatch):
    requests = []
    clients = []

    def respond(request):
        requests.append(request)
        body = (
            'data: {"choices":[{"delta":{"content":"configured reply"},'
            '"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, text=body)

    def create_client(*, timeout):
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr("minitau_ai.openai_compatible.create_async_client", create_client)
    return requests, clients


def test_first_launch_configures_and_chats_without_env(tmp_path, monkeypatch, mock_http, capsys):
    requests, clients = mock_http
    prompts = []
    read = inputs("deepseek", "https://deepseek.example/v1", "chosen-model", "你好", "/exit")

    def ask(prompt):
        prompts.append(prompt)
        return read(prompt)

    monkeypatch.setattr("builtins.input", ask)
    secret_prompts = []

    def secret(prompt):
        secret_prompts.append(prompt)
        return "local-test-key"

    monkeypatch.setattr("minitau_coding.cli.getpass.getpass", secret)
    run_interactive_mode(
        initial_prompt=None,
        provider_name=None,
        model_name=None,
        cwd=tmp_path,
        resume=None,
        continue_latest=False,
        context_window=None,
    )

    assert secret_prompts and not any("API key" in prompt for prompt in prompts)
    assert len(requests) == 1
    assert str(requests[0].url) == "https://deepseek.example/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer local-test-key"
    assert json.loads(requests[0].content)["model"] == "chosen-model"
    assert clients and all(client.is_closed for client in clients)
    output = capsys.readouterr()
    assert "configured reply" in output.out
    assert "local-test-key" not in output.out + output.err
    settings = (tmp_path / ".minitau/settings.json").read_text(encoding="utf-8")
    assert "local-test-key" not in settings
    for path in (tmp_path / ".minitau/sessions").glob("*.jsonl"):
        assert "local-test-key" not in path.read_text(encoding="utf-8")


def test_restart_uses_saved_key_over_stale_env_without_prompt(tmp_path, monkeypatch, mock_http):
    save(ProviderRuntime(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-key")
    monkeypatch.setenv("MINITAU_BASE_URL", "https://stale.example/v1")
    requests, clients = mock_http
    result = CliRunner().invoke(app, ["--cwd", str(tmp_path), "-p", "hello"])
    assert result.exit_code == 0, result.output
    assert requests[0].headers["Authorization"] == "Bearer local-test-key"
    assert requests[0].url.host == "deepseek.example"
    assert all(client.is_closed for client in clients)


def test_missing_credentials_in_print_mode_does_not_prompt(tmp_path):
    result = CliRunner().invoke(
        app, ["--cwd", str(tmp_path), "--provider", "deepseek", "-p", "hello"]
    )
    assert result.exit_code == 2
    assert "/config deepseek" in result.output
    assert not (tmp_path / ".minitau/sessions").exists()


async def test_explicit_provider_without_key_can_be_configured_at_start(tmp_path):
    runtime = ProviderRuntime(tmp_path)
    configured = []

    def configure(name):
        configured.append(name)
        save(runtime, name)
        return name

    try:
        session, _provider = await _open_session(
            manager=SessionManager(tmp_path),
            provider_name="deepseek",
            model_name="override",
            context_window=None,
            resume=None,
            continue_latest=False,
            title="",
            runtime=runtime,
            configure=configure,
        )
        assert configured == ["deepseek"]
        assert session.provider_name == "deepseek"
        assert session.status.model == "override"
    finally:
        await runtime.aclose()


async def test_config_command_changes_next_request_without_losing_history(tmp_path, mock_http):
    runtime = ProviderRuntime(tmp_path)
    provider, model = runtime.create("demo")
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=provider,
            model=model,
            provider_name="demo",
            provider_factory=runtime.create,
        ),
        session_id="switch",
        title="",
    )
    output = []
    try:
        await run_repl(
            session,
            SessionManager(tmp_path),
            read_line=inputs("before", "/config deepseek", "after", "/exit"),
            emit=lambda text, err: output.append(text),
            provider_runtime=runtime,
            ask=inputs("https://deepseek.example/v1", "chosen-model"),
            ask_secret=inputs("new-key"),
        )
        requests, clients = mock_http
        assert len(requests) == 1
        payload = json.loads(requests[0].content)
        assert [item["role"] for item in payload["messages"]] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        assert payload["model"] == "chosen-model"
        assert session.provider_name == "deepseek"
        assert len(session.messages) == 4
        entries = await JsonlSessionStorage(tmp_path / ".minitau/sessions/switch.jsonl").read_all()
        assert [
            entry.provider_name for entry in entries if isinstance(entry, ModelChangeEntry)
        ] == ["demo", "deepseek"]
        assert "new-key" not in "\n".join(output)
    finally:
        await runtime.aclose()
    assert all(client.is_closed for client in clients)


async def test_config_cancel_keeps_repl_and_provider_usable(tmp_path):
    runtime = ProviderRuntime(tmp_path)
    provider, model = runtime.create("demo")
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=provider,
            model=model,
            provider_name="demo",
            provider_factory=runtime.create,
        ),
        session_id="cancel",
        title="",
    )
    previous_handler = signal.getsignal(signal.SIGINT)

    def interrupt(_prompt):
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        raise KeyboardInterrupt

    output = []
    try:
        await run_repl(
            session,
            SessionManager(tmp_path),
            read_line=inputs("/config deepseek", "still works", "/exit"),
            emit=lambda text, err: output.append(text),
            provider_runtime=runtime,
            ask=inputs("", ""),
            ask_secret=interrupt,
        )
        assert signal.getsignal(signal.SIGINT) is previous_handler
        assert session.provider_name == "demo"
        assert "Demo response to: still works" in output
        assert not runtime.store.settings_path.exists()
        assert not runtime.store.credentials_path.exists()
    finally:
        await runtime.aclose()


async def test_branch_and_session_restore_provider_and_model_together(tmp_path, mock_http):
    runtime = ProviderRuntime(tmp_path)
    save(runtime, "deepseek", "model-a")
    save(runtime, "kimi", "model-b")
    provider, model = runtime.create("deepseek")
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=provider,
            model=model,
            provider_name="deepseek",
            provider_factory=runtime.create,
        ),
        session_id="original",
        title="",
    )
    path = tmp_path / ".minitau/sessions/original.jsonl"
    try:
        _ = [event async for event in session.prompt("first")]
        first_id = (await JsonlSessionStorage(path).read_all())[-1].id
        await session.set_provider("kimi")
        _ = [event async for event in session.prompt("second")]
        kimi_id = (await JsonlSessionStorage(path).read_all())[-1].id
        await session.branch_to(first_id)
        _ = [event async for event in session.prompt("branch")]
        assert session.provider_name == "deepseek"
        assert session.status.model == "model-a"
        await session.branch_to(kimi_id)
        _ = [event async for event in session.prompt("other branch")]
        await session.start_new(SessionManager(tmp_path))
        await session.set_provider("demo")
        await session.switch_to(SessionManager(tmp_path), "original")
        _ = [event async for event in session.prompt("restored")]
        assert session.provider_name == "kimi"
        assert session.status.model == "model-b"
        requests, _clients = mock_http
        assert [request.url.host for request in requests] == [
            "deepseek.example",
            "kimi.example",
            "deepseek.example",
            "kimi.example",
            "kimi.example",
        ]
        assert [json.loads(request.content)["model"] for request in requests] == [
            "model-a",
            "model-b",
            "model-a",
            "model-b",
            "model-b",
        ]
    finally:
        await runtime.aclose()


async def test_failed_switch_does_not_change_active_state(tmp_path, monkeypatch):
    runtime = ProviderRuntime(tmp_path)
    provider, model = runtime.create("demo")
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=provider,
            model=model,
            provider_name="demo",
            provider_factory=runtime.create,
        ),
        session_id="errors",
        title="",
    )
    path = tmp_path / ".minitau/sessions/errors.jsonl"
    try:
        with pytest.raises(MissingProviderCredentials):
            await session.set_provider("deepseek")
        assert session.provider_name == "demo"
        before = path.read_text(encoding="utf-8")

        async def disk_full(_self, _entry):
            raise OSError("disk full")

        monkeypatch.setattr(JsonlSessionStorage, "append", disk_full)
        with pytest.raises(OSError, match="disk full"):
            await session.set_provider("fake")
        assert session.provider_name == "demo"
        assert path.read_text(encoding="utf-8") == before
    finally:
        await runtime.aclose()


async def test_missing_branch_credentials_do_not_write_leaf_selection(tmp_path, mock_http):
    runtime = ProviderRuntime(tmp_path)
    save(runtime)
    provider, model = runtime.create("deepseek")
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=provider,
            model=model,
            provider_name="deepseek",
            provider_factory=runtime.create,
        ),
        session_id="branch-error",
        title="",
    )
    path = tmp_path / ".minitau/sessions/branch-error.jsonl"
    try:
        _ = [event async for event in session.prompt("first")]
        target = (await JsonlSessionStorage(path).read_all())[-1].id
        await session.set_provider("demo")
        _ = [event async for event in session.prompt("offline")]
        # 模拟凭据被用户移除，再尝试返回先前真实供应商的分支。
        runtime.store.credentials_path.write_text("{}", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        with pytest.raises(MissingProviderCredentials):
            await session.branch_to(target)
        assert path.read_text(encoding="utf-8") == before
        assert session.provider_name == "demo"
        assert session.messages[-1].text == "Demo response to: offline"
    finally:
        await runtime.aclose()


async def test_config_refreshes_client_for_same_provider(tmp_path, mock_http):
    runtime = ProviderRuntime(tmp_path)
    save(runtime, key="old-key")
    provider, model = runtime.create("deepseek")
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=provider,
            model=model,
            provider_name="deepseek",
            provider_factory=runtime.create,
        ),
        session_id="refresh",
        title="",
    )
    try:
        await run_repl(
            session,
            SessionManager(tmp_path),
            read_line=inputs("before", "/config deepseek", "after", "/exit"),
            emit=lambda _text, _err: None,
            provider_runtime=runtime,
            ask=inputs("https://replacement.example/v1", "replacement-model"),
            ask_secret=inputs("replacement-key"),
        )
        requests, clients = mock_http
        assert [request.url.host for request in requests] == [
            "deepseek.example",
            "replacement.example",
        ]
        assert [request.headers["Authorization"] for request in requests] == [
            "Bearer old-key",
            "Bearer replacement-key",
        ]
        assert json.loads(requests[-1].content)["model"] == "replacement-model"
    finally:
        await runtime.aclose()
    assert len(clients) == 2 and all(client.is_closed for client in clients)


async def test_provider_listing_and_model_default_do_not_leak_keys(tmp_path):
    runtime = ProviderRuntime(tmp_path)
    save(runtime)
    provider, model = runtime.create("deepseek")
    session = await CodingSession.new(
        CodingSessionConfig(
            cwd=tmp_path,
            provider=provider,
            model=model,
            provider_name="deepseek",
            provider_factory=runtime.create,
        ),
        session_id="commands",
        title="",
    )

    async def on_event(_event):
        pass

    context = CommandContext(session, SessionManager(tmp_path), on_event, runtime)
    try:
        result = await dispatch_input("/provider", context)
        assert "deepseek: 已配置" in result.message
        assert "local-test-key" not in result.message
        result = await dispatch_input("/model new-model", context)
        assert result.error is None
        assert runtime.store.load().providers["deepseek"].model == "new-model"
        result = await dispatch_input("/provider demo", context)
        assert result.error is None
        assert session.provider_name == runtime.default_provider == "demo"
    finally:
        await runtime.aclose()


def test_corrupt_credentials_error_never_contains_raw_secret(tmp_path):
    store = ProviderSettingsStore(tmp_path)
    store.credentials_path.parent.mkdir(parents=True)
    store.credentials_path.write_text('{"api_keys":{"deepseek":["secret-test"]}}')
    with pytest.raises(ValueError) as error:
        store.api_key("deepseek")
    assert "secret-test" not in str(error.value)


def test_wizard_invalid_input_and_custom_endpoint(tmp_path):
    runtime = ProviderRuntime(tmp_path)
    selected = configure_provider(
        runtime,
        None,
        ask=inputs(
            "99",
            "custom",
            "https://user:password@example.com",
            "model",
            "http://localhost:8000/v1/",
            "local-model",
        ),
        ask_secret=inputs("", "test-key"),
        show=lambda _text: None,
    )
    assert selected == "custom"
    assert runtime.settings_for("custom").base_url == "http://localhost:8000/v1"
    assert runtime.settings_for("custom").model == "local-model"
    assert runtime.api_key("custom") == "test-key"


def test_wizard_blank_key_retains_saved_key_and_cancellation_writes_nothing(tmp_path):
    runtime = ProviderRuntime(tmp_path)
    save(runtime)
    before = runtime.store.credentials_path.read_bytes()
    with pytest.raises(ConfigurationCancelled):
        configure_provider(
            runtime,
            "deepseek",
            ask=inputs("", ""),
            ask_secret=inputs("q"),
            show=lambda _text: None,
        )
    assert runtime.store.credentials_path.read_bytes() == before
    configure_provider(
        runtime,
        "deepseek",
        ask=inputs("", "other-model"),
        ask_secret=inputs(""),
        show=lambda _text: None,
    )
    assert runtime.api_key("deepseek") == "local-test-key"
    assert runtime.settings_for("deepseek").model == "other-model"


async def test_owned_providers_close_once_even_when_one_close_fails(tmp_path):
    class TrackingProvider(FakeProvider):
        def __init__(self, fail=False):
            super().__init__([])
            self.fail = fail
            self.closed = 0

        async def aclose(self):
            self.closed += 1
            if self.fail:
                raise RuntimeError("close failed")

    good, bad = TrackingProvider(), TrackingProvider(fail=True)
    runtime = ProviderRuntime(
        tmp_path, factory=lambda name: (good if name == "demo" else bad, name)
    )
    runtime.create("demo")
    runtime.create("fake")
    runtime.create("demo")
    with pytest.raises(ExceptionGroup):
        await runtime.aclose()
    assert good.closed == bad.closed == 1
    await runtime.aclose()
    assert good.closed == bad.closed == 1
