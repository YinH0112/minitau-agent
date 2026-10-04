"""Cross-layer regressions found during the P1-P5 reliability review."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from time import monotonic

import httpx
import pytest
from typer.testing import CliRunner

import minitau_coding.cli as coding_cli
from minitau_agent.harness import AgentHarness, AgentHarnessConfig, SimpleCancellationToken
from minitau_agent.message import AssistantMessage, TextContent, ToolCall, UserMessage
from minitau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantStartEvent,
)
from minitau_agent.session.entries import MessageEntry
from minitau_agent.session.memory import SessionState
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_agent.tools import AgentTool, AgentToolResult
from minitau_ai.env import OpenAICompatibleConfig
from minitau_ai.fake import FakeProvider
from minitau_ai.openai_compatible import OpenAICompatibleProvider
from minitau_coding.tools import create_bash_tool, create_edit_tool


@pytest.mark.parametrize(
    ("body", "expected_reason"),
    [
        ('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', "error"),
        ('data: {"error":{"message":"backend failed"}}\n\ndata: [DONE]\n\n', "error"),
        ("data: [DONE]\n\n", "error"),
        (
            'data: {"choices":[{"delta":{"content":"complete"},"finish_reason":"stop"}]}\n\n',
            "stop",
        ),
    ],
)
async def test_sse_requires_a_real_completion(body: str, expected_reason: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(api_key="test", max_retries=0), client=client
        )
        events = [
            event
            async for event in provider.stream_response(
                model="test", system="", messages=[], tools=[]
            )
        ]
    if expected_reason == "stop":
        assert isinstance(events[-1], AssistantDoneEvent)
        assert events[-1].message.stop_reason == "stop"
    else:
        assert isinstance(events[-1], AssistantErrorEvent)
        assert events[-1].error.stop_reason == "error"


async def test_invalid_streamed_tool_arguments_are_never_executable() -> None:
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "bash", "arguments": "{bad"}}
        ]}, "finish_reason": "tool_calls"}]},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    ) as client:
        provider = OpenAICompatibleProvider(OpenAICompatibleConfig(api_key="test"), client=client)
        events = [
            event
            async for event in provider.stream_response(
                model="test", system="", messages=[], tools=[]
            )
        ]
    assert isinstance(events[-1], AssistantErrorEvent)
    assert "invalid JSON arguments" in (events[-1].error.error_message or "")


async def test_cancellation_interrupts_a_stalled_sse_stream() -> None:
    class PausedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            return None

    token = SimpleCancellationToken()
    started = asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=PausedStream())
        )
    ) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(api_key="test"), client=client
        )

        async def consume():
            events = []
            async for event in provider.stream_response(
                model="test", system="", messages=[], tools=[], signal=token
            ):
                events.append(event)
                if event.type == "text_delta":
                    started.set()
            return events

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            token.cancel()
            events = await asyncio.wait_for(task, timeout=2)
            assert isinstance(events[-1], AssistantErrorEvent)
            assert events[-1].reason == "aborted"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_closing_provider_event_stream_closes_http_response() -> None:
    """模型事件流中途关闭后，底层 HTTP 响应必须在 aclose 返回前关闭。"""

    class CloseTrackingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    body = CloseTrackingStream()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))
    ) as client:
        provider = OpenAICompatibleProvider(OpenAICompatibleConfig(api_key="test"), client=client)
        events = provider.stream_response(model="test", system="", messages=[], tools=[])

        # 让 HTTP 流真正开始，再由调用方主动停止读取后续模型事件。
        while True:
            event = await events.__anext__()
            if event.type == "text_delta":
                break

        await events.aclose()
        assert body.closed


async def test_restored_history_stays_compacted_after_another_replay(tmp_path: Path) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    for message in [UserMessage(content="old question"), AssistantMessage(content="old answer")]:
        await storage.append(MessageEntry(message=message))
    entries = await storage.read_all()
    state = SessionState.from_entries(entries)
    harness = AgentHarness(
        AgentHarnessConfig(provider=FakeProvider([]), model="fake", system="", storage=storage),
        messages=state.messages,
        entry_ids=state.context_entry_ids,
        last_entry_id=entries[-1].id,
    )
    await harness._apply_compaction_in_place(list(state.context_entry_ids), "summary")
    replayed = SessionState.from_entries(await storage.read_all())
    assert [message.text for message in replayed.messages] == [
        message.text for message in harness.messages
    ]
    assert replayed.context_entry_ids[0] != state.context_entry_ids[0]


def test_persisted_resume_requires_message_entry_ids(tmp_path: Path) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    with pytest.raises(ValueError, match="requires entry_ids"):
        AgentHarness(
            AgentHarnessConfig(provider=FakeProvider([]), model="fake", system="", storage=storage),
            messages=[UserMessage(content="old")], last_entry_id="old-id",
        )


async def test_interrupted_tool_result_is_persisted_before_next_prompt(tmp_path: Path) -> None:
    storage = JsonlSessionStorage(tmp_path / "session.jsonl")
    call = ToolCall(id="c1", name="bash", arguments={"command": "echo never ran"})
    original = MessageEntry(message=AssistantMessage(content=[call]))
    await storage.append(original)
    provider = FakeProvider([[
        AssistantStartEvent(partial=AssistantMessage()),
        AssistantDoneEvent(reason="stop", message=AssistantMessage(content="continued")),
    ]])
    harness = AgentHarness(
        AgentHarnessConfig(provider=provider, model="fake", system="", storage=storage),
        messages=[original.message], entry_ids=[original.id], last_entry_id=original.id,
    )
    _ = [event async for event in harness.prompt("continue")]
    entries = await storage.read_all()
    assert [entry.message.role for entry in entries] == [
        "assistant", "toolResult", "user", "assistant"
    ]
    assert entries[1].parent_id == original.id
    assert entries[2].parent_id == entries[1].id


async def test_tool_loop_compacts_before_its_next_model_request() -> None:
    async def execute(_arguments):
        return AgentToolResult(content="o" * 1400)

    tool = AgentTool(name="big", description="large output", parameters={"type": "object"},
                     execute_fn=execute)
    call = ToolCall(id="c1", name="big", arguments={})
    provider = FakeProvider([
        [AssistantStartEvent(partial=AssistantMessage()), AssistantDoneEvent(
            reason="toolUse", message=AssistantMessage(content=[call])
        )],
        [AssistantStartEvent(partial=AssistantMessage()), AssistantDoneEvent(
            reason="stop", message=AssistantMessage(content="summary")
        )],
        [AssistantStartEvent(partial=AssistantMessage()), AssistantDoneEvent(
            reason="stop", message=AssistantMessage(content="finished")
        )],
    ])
    harness = AgentHarness(AgentHarnessConfig(
        provider=provider, model="fake", system="", tools=[tool],
        context_window_tokens=1000,
    ), messages=[UserMessage(content="old"), AssistantMessage(content="x" * 1800)])
    events = [event async for event in harness.prompt("run")]
    assert any(event.type == "compaction_end" for event in events)
    assert provider.calls[1][1] != ""  # the middle call is the summarizer
    assert provider.calls[2][2][0].text.startswith("Previous conversation summary:")
    assert harness.messages[-1].text == "finished"


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (b"alpha\nbeta\n", b"ALPHA\nbeta\n"),
        (b"alpha\r\nbeta\r\n", b"ALPHA\r\nbeta\r\n"),
        (b"\xef\xbb\xbfalpha\nbeta", b"\xef\xbb\xbfALPHA\nbeta"),
    ],
)
async def test_edit_preserves_file_bytes_outside_change(
    tmp_path: Path, original: bytes, expected: bytes
) -> None:
    path = tmp_path / "example.txt"
    path.write_bytes(original)
    await create_edit_tool(cwd=tmp_path).execute(
        {"path": "example.txt", "edits": [{"oldText": "alpha", "newText": "ALPHA"}]}
    )
    assert path.read_bytes() == expected


async def test_bash_timeout_returns_before_child_finishes(tmp_path: Path) -> None:
    executable = str(sys.executable).replace("\\", "/")
    start = monotonic()
    result = await create_bash_tool(cwd=tmp_path).execute(
        {"command": f'"{executable}" -c "import time; time.sleep(8)"', "timeout": 0.2}
    )
    assert result.details["timed_out"] is True
    assert monotonic() - start < 6


async def test_cancel_running_bash_prevents_second_model_request(tmp_path: Path) -> None:
    executable = str(sys.executable).replace("\\", "/")
    call = ToolCall(
        id="c1", name="bash",
        arguments={"command": f'"{executable}" -c "import time; time.sleep(8)"'},
    )
    provider = FakeProvider([
        [
            AssistantStartEvent(partial=AssistantMessage()),
            AssistantDoneEvent(reason="toolUse", message=AssistantMessage(content=[call])),
        ],
        [AssistantStartEvent(partial=AssistantMessage()), AssistantDoneEvent(
            reason="stop", message=AssistantMessage(content=[TextContent(text="unexpected")])
        )],
    ])
    harness = AgentHarness(AgentHarnessConfig(
        provider=provider, model="fake", system="", tools=[create_bash_tool(cwd=tmp_path)]
    ))
    started = asyncio.Event()

    async def consume() -> None:
        async for event in harness.prompt("run"):
            if event.type == "tool_execution_start":
                started.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0.2)
        start = monotonic()
        harness.cancel()
        await asyncio.wait_for(task, timeout=6)
        assert monotonic() - start < 6
        assert len(provider.calls) == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_cli_reports_provider_error_and_nonzero_exit(monkeypatch, tmp_path: Path) -> None:
    provider = FakeProvider([[
        AssistantErrorEvent(
            reason="error", error=AssistantMessage(
                stop_reason="error", error_message="simulated HTTP 401"
            )
        )
    ]])
    monkeypatch.setattr(coding_cli, "create_provider", lambda name: (provider, "fake"))
    result = CliRunner().invoke(
        coding_cli.app, ["--print", "hello", "--cwd", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "simulated HTTP 401" in result.stderr
    assert result.stdout == ""
