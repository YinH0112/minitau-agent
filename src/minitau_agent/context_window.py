"""Approximate context-size estimation for minitau sessions."""

from __future__ import annotations

from dataclasses import dataclass

from minitau_agent.message import AgentMessage, AssistantMessage, ToolResultMessage
from minitau_agent.tools import AgentTool

CHARS_PER_TOKEN = 4 # 英文文本的经验比率（GPT 系 tokenizer 约 1 token ≈ 4 字符
MESSAGE_OVERHEAD_TOKENS = 4 # 每条消息的协议包装成本
TOOL_OVERHEAD_TOKENS = 16 # 工具定义的包装成本
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000 # 未知模型的兜底窗口
DEFAULT_COMPACTION_RESERVE_TOKENS = 16_384
# 窗口-保留区模型的“保留区” 压缩阈值 = 窗口 − 16k，意味着“上下文离满还有 16k 时就动手”
DEFAULT_COMPACTION_KEEP_RECENT_TOKENS = 20_000
# 尾部保留区的大小——最近 20k token 的消息原样保留

def estimate_text_tokens(text: str) -> int:
    """Return a deterministic rough token estimate for text. """
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN -1) // CHARS_PER_TOKEN)


def estimate_message_tokens(message: AgentMessage) -> int:
    """ Return a rough token estimate for one provider-neutral message"""
    tokens = MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(message.text)
    if isinstance(message, AssistantMessage):
        tokens += sum(
            estimate_text_tokens(call.name) + estimate_text_tokens(str(call.arguments))
            for call in message.tool_calls
        )
    elif isinstance(message, ToolResultMessage):
        tokens += estimate_text_tokens(message.tool_name)
    return tokens

def estimate_tool_tokens(tool: AgentTool) -> int:
    """ Return a rough token estimate for one tool definition"""
    return (
        TOOL_OVERHEAD_TOKENS
        + estimate_text_tokens(tool.name)
        + estimate_text_tokens(tool.description)
        + estimate_text_tokens(str(tool.input_schema))
    )

@dataclass(frozen=True, slots=True)
class ContextUsageEstimate:
    """ Deterministic context-size accounting for one provider request."""

    total_tokens: int
    system_tokens: int
    message_tokens: int
    tool_tokens: int
    message_count: int
    tool_count: int

def estimate_context_usage(
        *,
        system: str,
        messages: tuple[AgentMessage, ...],
        tools: tuple[AgentTool, ...],
) -> ContextUsageEstimate:
    system_tokens = estimate_text_tokens(system)
    message_tokens = sum(estimate_message_tokens(message) for message in messages)
    tool_tokens = sum(estimate_tool_tokens(tool) for tool in tools)
    return ContextUsageEstimate(
        total_tokens=system_tokens + message_tokens + tool_tokens,
        system_tokens=system_tokens,
        message_tokens=message_tokens,
        tool_tokens=tool_tokens,
        message_count=len(messages),
        tool_count=len(tools),
    )

def auto_compaction_threshold_for_context_window(context_window_tokens: int) -> int | None:
    """Return the automatic compaction threshold for a context window."""
    if context_window_tokens <= 0:
        return None
    reserve = min(DEFAULT_COMPACTION_RESERVE_TOKENS, max(1, context_window_tokens // 4))
    return max(1, context_window_tokens - reserve)
class ContextWindowExceeded(RuntimeError):
    """The current context cannot fit into the configured model window."""

