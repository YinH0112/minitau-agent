"""P3 Step 6: AgentHarness 自动压缩——阈值触发、事件广播、条目落盘。

双预算模型：
  - auto_compact_token_threshold  窗口阈值：总 token 超过它才"考虑"压缩
  - DEFAULT_COMPACTION_KEEP_RECENT_TOKENS  尾部保留预算：只压更早的，尾部原样留
压缩真正发生需要【两个条件同时成立】。
"""

from __future__ import annotations

from minitau_agent.compaction import (
    COMPACTION_SYSTEM_PROMPT,
    format_compaction_summary,
)
from minitau_agent.context_window import (
    DEFAULT_COMPACTION_RESERVE_TOKENS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
)
from minitau_agent.events import CompactionEndEvent, CompactionStartEvent
from minitau_agent.harness import AgentHarness, AgentHarnessConfig
from minitau_agent.message import AssistantMessage
from minitau_agent.session.entries import CompactionEntry
from minitau_agent.session.memory import SessionState
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_ai.fake import FakeProvider
from pi_event_helpers import assistant_done, assistant_error, assistant_start

# ≈10001 tokens/条：两条长消息就能填满 20000 的尾部保留预算，
# 让 candidate 落在"倒二"的位置，从而留出可压缩的前缀。
LONG_TEXT = "x" * 40_001


def make_config(
    provider: FakeProvider,
    *,
    threshold: int | None = None,
    enabled: bool = True,
    storage=None,
) -> AgentHarnessConfig:
    return AgentHarnessConfig(
        provider=provider,
        model="fake",
        system="test system",
        storage=storage,
        auto_compact_enabled=enabled,
        auto_compact_token_threshold=threshold,
    )


def over_threshold_provider(summary_stream) -> FakeProvider:
    """两条主线轮次（答 hi / 答长文本）+ 一条压缩摘要流。"""
    return FakeProvider(
        [
            [assistant_start(), assistant_done(AssistantMessage(content="hi"))],
            [assistant_start(), assistant_done(AssistantMessage(content=LONG_TEXT))],
            summary_stream,
        ]
    )


class TestAutoCompactThreshold:
    """阈值怎么算：显式 > 从窗口推导 > 禁用。"""

    def test_default_threshold_derived_from_context_window(self) -> None:
        harness = AgentHarness(make_config(FakeProvider([])))

        assert harness.auto_compact_token_threshold == (
            DEFAULT_CONTEXT_WINDOW_TOKENS - DEFAULT_COMPACTION_RESERVE_TOKENS
        )

    def test_explicit_threshold_wins_over_default(self) -> None:
        harness = AgentHarness(make_config(FakeProvider([]), threshold=500))

        assert harness.auto_compact_token_threshold == 500

    def test_disabled_returns_none(self) -> None:
        harness = AgentHarness(
            make_config(FakeProvider([]), threshold=500, enabled=False)
        )

        assert harness.auto_compact_token_threshold is None


class TestAutoCompactTrigger:
    """触发条件：超过阈值 且 前缀超出尾部保留预算。"""

    async def test_small_context_never_compacts(self, tmp_path) -> None:
        """两条消息远小于 20k 保留预算：阈值被超过也不压缩。"""
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        provider = FakeProvider(
            [[assistant_start(), assistant_done(AssistantMessage(content="hi"))]]
        )
        harness = AgentHarness(make_config(provider, threshold=1, storage=storage))

        events = [event async for event in harness.prompt("hello")]

        assert not [
            e for e in events if e.type in {"compaction_start", "compaction_end"}
        ]
        assert len(await storage.read_all()) == 2

    async def test_over_threshold_compacts_prefix_and_emits_events(self, tmp_path) -> None:
        """第二轮后：开头两条被摘要取代，事件在 agent_end 之后广播。"""
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        harness = AgentHarness(
            make_config(over_threshold_provider(
                [assistant_start(), assistant_done(AssistantMessage(content="summary text"))]
            ), threshold=1, storage=storage)
        )

        _ = [event async for event in harness.prompt("hello")]
        events = [event async for event in harness.prompt(LONG_TEXT)]

        compaction = [
            e for e in events if isinstance(e, (CompactionStartEvent, CompactionEndEvent))
        ]
        assert [e.type for e in compaction] == ["compaction_start", "compaction_end"]
        assert compaction[0].reason == "auto"
        assert compaction[1].aborted is False
        assert events[-1].type == "compaction_end"

        # 不比较完整模型：timestamp 是自动生成的毫秒时间戳，
        # 期望对象和实际对象的创建时间必然差几毫秒，整模型 == 永远失败。
        # 比较语义内容（role / text）才是对"压缩结果"的真实断言。
        assert [m.role for m in harness.messages] == ["user", "user", "assistant"]
        assert harness.messages[0].text == format_compaction_summary("summary text")
        assert harness.messages[1].text == LONG_TEXT
        assert harness.messages[2].text == LONG_TEXT

        entries = await storage.read_all()
        assert len(entries) == 5
        compaction_entry = entries[-1]
        assert isinstance(compaction_entry, CompactionEntry)
        assert compaction_entry.summary == "summary text"
        assert compaction_entry.replaces_entry_ids == [entries[0].id, entries[1].id]

    async def test_summarizer_sees_only_compacted_prefix(self) -> None:
        """摘要请求只带被压掉的前缀，不带整个上下文；且不带工具。"""
        provider = over_threshold_provider(
            [assistant_start(), assistant_done(AssistantMessage(content="s"))]
        )
        harness = AgentHarness(make_config(provider, threshold=1))

        _ = [event async for event in harness.prompt("hello")]
        _ = [event async for event in harness.prompt(LONG_TEXT)]

        model, system, messages, tools = provider.calls[2]
        assert model == "fake"
        assert system == COMPACTION_SYSTEM_PROMPT
        assert tools == []
        assert "hello" in messages[0].content
        assert LONG_TEXT not in messages[0].content

    async def test_replay_reproduces_compacted_context(self, tmp_path) -> None:
        """落盘后重放：摘要原位取代被替换消息，与内存状态一致。"""
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        harness = AgentHarness(
            make_config(over_threshold_provider(
                [assistant_start(), assistant_done(AssistantMessage(content="summary text"))]
            ), threshold=1, storage=storage)
        )
        _ = [event async for event in harness.prompt("hello")]
        _ = [event async for event in harness.prompt(LONG_TEXT)]

        state = SessionState.from_entries(await storage.read_all())

        # 同上：重放生成的消息对象与内存对象时间戳不同，比较文本投影。
        assert [m.text for m in state.messages] == [m.text for m in harness.messages]
        assert state.messages[0].text == format_compaction_summary("summary text")

    async def test_compacted_context_feeds_next_turn(self) -> None:
        """压缩后的摘要出现在下一轮 provider 请求里。"""
        provider = FakeProvider(
            [
                [assistant_start(), assistant_done(AssistantMessage(content="hi"))],
                [assistant_start(), assistant_done(AssistantMessage(content=LONG_TEXT))],
                [assistant_start(), assistant_done(AssistantMessage(content="summary text"))],
                [assistant_start(), assistant_done(AssistantMessage(content="final"))],
            ]
        )
        harness = AgentHarness(make_config(provider, threshold=1))

        _ = [event async for event in harness.prompt("hello")]
        _ = [event async for event in harness.prompt(LONG_TEXT)]
        harness.config.auto_compact_enabled = False
        _ = [event async for event in harness.prompt("next")]

        context = provider.calls[3][2]
        assert context[0].text == format_compaction_summary("summary text")
        assert len(context) == 4  # 摘要 + 两条长消息 + 新提问

    async def test_disabled_never_compacts(self, tmp_path) -> None:
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        provider = FakeProvider(
            [
                [assistant_start(), assistant_done(AssistantMessage(content="hi"))],
                [assistant_start(), assistant_done(AssistantMessage(content=LONG_TEXT))],
            ]
        )
        harness = AgentHarness(
            make_config(provider, threshold=1, enabled=False, storage=storage)
        )

        _ = [event async for event in harness.prompt("hello")]
        events = [event async for event in harness.prompt(LONG_TEXT)]

        assert not [
            e for e in events if e.type in {"compaction_start", "compaction_end"}
        ]
        assert len(await storage.read_all()) == 4

    async def test_summary_failure_aborts_without_losing_turn(self, tmp_path) -> None:
        """摘要模型报错：压缩中止、历史原样保留、harness 仍可继续用。"""
        storage = JsonlSessionStorage(tmp_path / "s.jsonl")
        provider = over_threshold_provider([assistant_error("summarizer broke")])
        harness = AgentHarness(make_config(provider, threshold=1, storage=storage))

        _ = [event async for event in harness.prompt("hello")]
        events = [event async for event in harness.prompt(LONG_TEXT)]

        end = next(e for e in events if isinstance(e, CompactionEndEvent))
        assert end.aborted is True
        assert "Context compaction failed" in (end.error_message or "")

        assert len(harness.messages) == 4  # 原样保留
        assert harness.is_running is False
        assert len(await storage.read_all()) == 4  # 没有 CompactionEntry
