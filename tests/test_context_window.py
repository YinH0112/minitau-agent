"""P3 Step 4: token 估算器 + 压缩阈值（笔记 05-03，对齐 Tau context_window.py）。"""

from __future__ import annotations

from minitau_agent.context_window import (
    DEFAULT_COMPACTION_RESERVE_TOKENS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    TOOL_OVERHEAD_TOKENS,
    ContextUsageEstimate,
    auto_compaction_threshold_for_context_window,
    estimate_context_usage,
    estimate_message_tokens,
    estimate_text_tokens,
    estimate_tool_tokens,
)
from minitau_agent.message import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from minitau_agent.tools import AgentTool, AgentToolResult


def make_tool(
    name: str = "read_file",
    description: str = "Read a file.",
    parameters: dict = None,
) -> AgentTool:
    async def execute(arguments):
        return AgentToolResult(content="ok")

    return AgentTool(
        name=name,
        description=description,
        parameters={} if parameters is None else parameters,
        execute_fn=execute,
    )


class TestEstimateTextTokens:
    """字符数 → token 数的确定性换算。"""

    def test_empty_text_is_zero(self) -> None:
        assert estimate_text_tokens("") == 0

    def test_exact_multiple_is_one_token(self) -> None:
        """4 字符 = 1 token（CHARS_PER_TOKEN=4）。"""
        assert estimate_text_tokens("abcd") == 1

    def test_overrun_ceils_to_next_token(self) -> None:
        """5 字符 = 2 token——向上取整。"""
        assert estimate_text_tokens("abcde") == 2

    def test_short_text_still_counts_one_token(self) -> None:
        """max(1, ...) 下限：哪怕 1 个字符也按 1 token 计。"""
        assert estimate_text_tokens("a") == 1

    def test_chinese_counts_by_characters(self) -> None:
        """按字符数不按字节数：2 个汉字 = 1 token。"""
        assert estimate_text_tokens("你好") == 1


class TestEstimateMessageTokens:
    """单条消息：开销 + 正文 + 消息类型特有部分。"""

    def test_user_message_is_overhead_plus_text(self) -> None:
        """11 字符 → 3 token 正文 + 4 开销 = 7。"""
        message = UserMessage(content="hello world")

        assert estimate_message_tokens(message) == 3 + MESSAGE_OVERHEAD_TOKENS

    def test_assistant_text_uses_aggregated_blocks(self) -> None:
        """AssistantMessage 的 text 是多块拼接，估算用拼接结果。"""
        message = AssistantMessage(model="m", content="A" * 100)

        assert estimate_message_tokens(message) == 25 + MESSAGE_OVERHEAD_TOKENS

    def test_assistant_tool_calls_add_name_and_arguments(self) -> None:
        """工具调用：name 9 字符→3 + str(arguments) 16 字符→4。"""
        message = AssistantMessage(
            model="m",
            content=[
                ToolCall(id="t1", name="read_file", arguments={"path": "x.py"}),
            ],
        )

        assert estimate_message_tokens(message) == 4 + 3 + 4

    def test_tool_result_adds_tool_name(self) -> None:
        """toolResult：开销 + content + 工具名（模型需要名字定位语境）。"""
        message = ToolResultMessage(
            tool_call_id="t1",
            tool_name="read_file",
            content="ok",
        )

        assert estimate_message_tokens(message) == 4 + 1 + 3


class TestEstimateToolTokens:
    """工具定义（system 侧注入的 schema）也有上下文成本。"""

    def test_tool_definition_has_overhead_and_parts(self) -> None:
        """16 开销 + name(3) + description(3) + str({})(1) = 23。"""
        tool = make_tool()

        assert estimate_tool_tokens(tool) == (
            TOOL_OVERHEAD_TOKENS + 3 + 3 + 1
        )


class TestEstimateContextUsage:
    """完整请求的账本：system + messages + tools 四路分项。"""

    def test_empty_request_is_all_zero(self) -> None:
        usage = estimate_context_usage(system="", messages=(), tools=())

        assert usage == ContextUsageEstimate(
            total_tokens=0,
            system_tokens=0,
            message_tokens=0,
            tool_tokens=0,
            message_count=0,
            tool_count=0,
        )

    def test_usage_sums_every_component(self) -> None:
        system = "You are helpful."      # 16 字符 → (16+3)//4 = 4
        messages = (UserMessage(content="hi"),)  # 4 + 1 = 5
        tools = (make_tool(),)           # 16 + 3 + 3 + 1 = 23

        usage = estimate_context_usage(
            system=system, messages=messages, tools=tools
        )

        assert usage.system_tokens == 4
        assert usage.message_tokens == 5
        assert usage.tool_tokens == 23
        assert usage.total_tokens == 32
        assert usage.message_count == 1
        assert usage.tool_count == 1


class TestAutoCompactionThreshold:
    """窗口-保留区模型：阈值 = 窗口 - 预留。"""

    def test_default_window_minus_reserve(self) -> None:
        assert auto_compaction_threshold_for_context_window(
            DEFAULT_CONTEXT_WINDOW_TOKENS
        ) == DEFAULT_CONTEXT_WINDOW_TOKENS - DEFAULT_COMPACTION_RESERVE_TOKENS

    def test_zero_or_negative_window_disables_compaction(self) -> None:
        assert auto_compaction_threshold_for_context_window(0) is None
        assert auto_compaction_threshold_for_context_window(-100) is None

    def test_tiny_window_clamps_to_one(self) -> None:
        """窗口小于预留时，max(1, ...) 保证阈值仍是合法正数。"""
        assert auto_compaction_threshold_for_context_window(100) == 75

    def test_custom_window(self) -> None:
        assert auto_compaction_threshold_for_context_window(32_768) == 24_576
