"""P3 Step 5: compaction —— 压缩范围选择 + 摘要请求。

对齐 Tau 真实实现（2026-09-20 逐行核实）：
- tau_coding/context_window.py 193-280：序列化 + prompt 组装
- tau_coding/session.py 1997-2028：_generate_compaction_summary（流式收集）
- tau_coding/session.py 2058-2076 + 2106-2139：_recent_preserving_compaction_plan
  + _first_recent_context_index（尾部保留切分算法）
"""

from __future__ import annotations

import pytest

from minitau_agent.compaction import (
    COMPACTION_SYSTEM_PROMPT,
    COMPACTION_USER_PROMPT,
    build_compaction_prompt,
    format_history_for_compaction,
    run_compaction,
    select_compaction_rows,
)
from minitau_agent.message import (
    AgentMessage,
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from minitau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantStartEvent,
    TextDeltaEvent,
)
from minitau_ai.fake import FakeProvider


def user(text: str) -> UserMessage:
    return UserMessage(content=text)


def assistant(text: str) -> AssistantMessage:
    return AssistantMessage(model="m", content=text)


def tool_result(text: str) -> ToolResultMessage:
    return ToolResultMessage(tool_call_id="t1", tool_name="read_file", content=text)


def rows(*items: tuple[str, AgentMessage]) -> list[tuple[str, AgentMessage]]:
    return list(items)


class TestFormatHistory:
    """历史序列化：喂给摘要模型的纯文本形态（Tau serialize_messages_for_compaction）。"""

    def test_empty_renders_placeholder(self) -> None:
        assert format_history_for_compaction([]) == "(no new messages)"

    def test_renders_index_and_role_tags(self) -> None:
        out = format_history_for_compaction([user("hello"), assistant("hi")])

        assert "<message index=1 role=user>" in out
        assert "hello" in out
        assert "<message index=2 role=assistant>" in out
        assert "hi" in out

    def test_tool_result_renders_tool_name(self) -> None:
        out = format_history_for_compaction([tool_result("ok")])

        assert "<message index=1 role=toolResult name=read_file>" in out
        assert "ok" in out

    def test_assistant_tool_calls_rendered(self) -> None:
        message = AssistantMessage(
            model="m",
            content=[
                ToolCall(id="t1", name="read_file", arguments={"path": "x.py"}),
            ],
        )

        out = format_history_for_compaction([message])

        assert "<tool-calls>" in out
        assert "- read_file: {'path': 'x.py'}" in out
        assert "</tool-calls>" in out


class TestBuildPrompt:
    """摘要请求的 prompt 组装（Tau build_compaction_summary_prompt 的裁剪版）。"""

    def test_wraps_conversation_in_tags(self) -> None:
        prompt = build_compaction_prompt([user("hi")])

        assert prompt.startswith("<conversation>\n")
        assert "hi" in prompt
        assert "</conversation>" in prompt

    def test_appends_summarization_instructions(self) -> None:
        prompt = build_compaction_prompt([user("hi")])

        assert COMPACTION_USER_PROMPT in prompt
        assert prompt.index("</conversation>") < prompt.index(COMPACTION_USER_PROMPT[:20])


class TestSelectCompactionRows:
    """尾部保留切分：预算从尾累加，切点对齐 user 边界。

    token 数学：每条消息 = 4 开销 + ceil(len/4)。
    user("A"*100)=29，assistant("B"*4)=5，user("C"*4)=5，assistant("D"*4)=5。
    """

    def test_fewer_than_two_rows_returns_none(self) -> None:
        assert select_compaction_rows(rows(("e0", user("x")))) is None

    def test_all_within_budget_returns_none(self) -> None:
        plan = select_compaction_rows(
            rows(("e0", user("hi")), ("e1", assistant("ok")))
        )

        assert plan is None

    def test_splits_at_user_boundary(self) -> None:
        # 反向累加：r3=5 < 10；r2=10 >= 10 → candidate=2（user）→ split=2
        plan = select_compaction_rows(
            rows(
                ("e0", user("A" * 100)),
                ("e1", assistant("B" * 4)),
                ("e2", user("C" * 4)),
                ("e3", assistant("D" * 4)),
            ),
            keep_recent_tokens=10,
        )

        assert plan is not None
        assert plan.replace_entry_ids == ["e0", "e1"]
        assert [m.text for m in plan.messages_to_summarize] == ["A" * 100, "B" * 4]

    def test_candidate_mid_turn_extends_to_next_user(self) -> None:
        # 反向累加：r4=5 < 8；r3=10 >= 8 → candidate=3（assistant，非 user）
        # → 前移到 r4（user）→ split=4，r3 也进压缩区
        plan = select_compaction_rows(
            rows(
                ("e0", user("A" * 100)),
                ("e1", assistant("B" * 4)),
                ("e2", user("C" * 4)),
                ("e3", assistant("D" * 4)),
                ("e4", user("E" * 4)),
            ),
            keep_recent_tokens=8,
        )

        assert plan is not None
        assert plan.replace_entry_ids == ["e0", "e1", "e2", "e3"]

    def test_split_at_zero_returns_none(self) -> None:
        # 反向累加：r2=5；r1=10；r0=39 >= 30 → candidate=0（user）→ split=0
        # split=0 意味着压缩前缀 rows[:0] 为空——整个上下文都装在保留区里，无事可做
        plan = select_compaction_rows(
            rows(
                ("e0", user("A" * 100)),
                ("e1", assistant("B" * 4)),
                ("e2", tool_result("ok")),
            ),
            keep_recent_tokens=30,
        )

        assert plan is None

    def test_zero_budget_compacts_everything(self) -> None:
        plan = select_compaction_rows(
            rows(
                ("e0", user("A")),
                ("e1", assistant("B")),
                ("e2", user("C")),
                ("e3", assistant("D")),
            ),
            keep_recent_tokens=0,
        )

        assert plan is not None
        assert plan.replace_entry_ids == ["e0", "e1", "e2", "e3"]

    def test_keeps_current_user_turn_when_tail_has_no_next_user(self) -> None:
        plan = select_compaction_rows(
            rows(
                ("e0", user("old" * 200)),
                ("e1", assistant("old reply")),
                ("e2", user("current")),
                ("e3", assistant("tool call")),
                ("e4", tool_result("output" * 200)),
            ),
            keep_recent_tokens=50,
        )
        assert plan is not None
        assert plan.replace_entry_ids == ["e0", "e1"]


class TestRunCompaction:
    """摘要请求：流式收集 + 请求形状（Tau _generate_compaction_summary）。"""

    async def test_collects_final_text_and_request_shape(self) -> None:
        provider = FakeProvider(
            [
                [
                    AssistantStartEvent(partial=AssistantMessage(model="m")),
                    TextDeltaEvent(
                        content_index=0, delta="Summ", partial=AssistantMessage(model="m")
                    ),
                    AssistantDoneEvent(
                        reason="stop",
                        message=AssistantMessage(model="m", content="  Summary  "),
                    ),
                ]
            ]
        )

        summary = await run_compaction(
            provider=provider,
            model="test-model",
            messages=[user("hi"), assistant("ok")],
        )

        assert summary == "Summary"
        assert len(provider.calls) == 1
        model, system, messages, tools = provider.calls[0]
        assert model == "test-model"
        assert system == COMPACTION_SYSTEM_PROMPT
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)
        assert "<conversation>" in messages[0].content
        assert "hi" in messages[0].content
        assert tools == []

    async def test_delta_fallback_when_no_done_event(self) -> None:
        provider = FakeProvider(
            [
                [
                    TextDeltaEvent(
                        content_index=0, delta="Summ", partial=AssistantMessage(model="m")
                    ),
                    TextDeltaEvent(
                        content_index=0, delta="ary", partial=AssistantMessage(model="m")
                    ),
                ]
            ]
        )

        summary = await run_compaction(
            provider=provider, model="m", messages=[user("hi")]
        )

        assert summary == "Summary"

    async def test_error_event_raises_runtime_error(self) -> None:
        provider = FakeProvider(
            [
                [
                    AssistantErrorEvent(
                        reason="error",
                        error=AssistantMessage(model="m", content="boom"),
                    ),
                ]
            ]
        )

        with pytest.raises(RuntimeError, match="compaction"):
            await run_compaction(provider=provider, model="m", messages=[user("hi")])

    async def test_error_event_closes_provider_stream(self) -> None:
        closed = False

        class ErrorProvider:
            def stream_response(self, **_kwargs):
                async def events():
                    nonlocal closed
                    try:
                        yield AssistantErrorEvent(
                            reason="error",
                            error=AssistantMessage(model="m", content="boom"),
                        )
                    finally:
                        closed = True

                return events()

        with pytest.raises(RuntimeError, match="compaction"):
            await run_compaction(provider=ErrorProvider(), model="m", messages=[user("hi")])

        assert closed is True

    async def test_empty_summary_raises_runtime_error(self) -> None:
        provider = FakeProvider(
            [
                [
                    AssistantDoneEvent(
                        reason="stop", message=AssistantMessage(model="m", content="")
                    ),
                ]
            ]
        )

        with pytest.raises(RuntimeError, match="empty"):
            await run_compaction(provider=provider, model="m", messages=[user("hi")])
