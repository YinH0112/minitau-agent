"""Context compaction: range selection and summarization requests."""

from __future__ import annotations

from dataclasses import dataclass

from minitau_agent.async_iterators import closing_async_iterator
from minitau_agent.context_window import (
    DEFAULT_COMPACTION_KEEP_RECENT_TOKENS,
    estimate_message_tokens,
)
from minitau_agent.message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from minitau_agent.provider import CancellationToken, ModelProvider
from minitau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    TextDeltaEvent,
)

COMPACTION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation "
    "between a user and an AI coding assistant, then produce a concise summary.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the "
    "conversation. ONLY output the summary."
)

COMPACTION_USER_PROMPT = (
    "The messages above are a conversation to summarize. "
    "Create a concise context summary for another LLM to continue the work. "
    "Organize it under these headings: Goal, Constraints, Completed, "
    "In progress, Next steps, Key files. "
    "Preserve exact file paths, function names, and error messages. "
    "Omit a heading only when there is no relevant information."
)

def format_history_for_compaction(messages: list[AgentMessage]) -> str:
    """Serialize provider-neutral messages for the compaction summarizer. """
    if not messages:
        return "(no new messages)"

    lines: list[str] = []
    for index, message in enumerate(messages, start=1):
        # numerate 用于在循环中同时获得“第几个”和“是什么”，默认从 0 开始，也可以指定起始编号
        attributes = f"index={index} role={message.role}"
        if isinstance(message, ToolResultMessage):
            attributes += f" name={message.tool_name}"
            # 工具结果必须标明"这是哪个工具返回的"
        lines.append(f"<message {attributes}>")
        if message.text:
            # text 是空串  不加判断
            lines.append(message.text)
        if isinstance(message, AssistantMessage) and message.tool_calls:
        # isinstance 确保是 assistant（只有它有 tool_calls 属性），and message.tool_calls 确保非空
            lines.append("<tool-calls>")
            for call in message.tool_calls:
                lines.append(f"- {call.name}: {call.arguments}")
            lines.append("</tool-calls>")
        lines.append("</message>")
    # 样例：
    # <message index=1 role=assistant>
    # <tool-calls>
    # - read_file: {'path': 'a.py'}
    # </tool-calls>
    # </message>
    return "\n".join(lines)

def build_compaction_prompt(messages: list[AgentMessage]) -> str:
    """Build the model prompt used to summarize compacted history."""
    conversation = format_history_for_compaction(messages)
    # 把整段对话包进 <conversation> 标签，后面接"请总结这段对话"的指令。
    # 分两层是为了让模型清楚：标签里的是"素材"，指令才是"任务"。
    #
    # 注意这里用 USER_PROMPT 而不是 SYSTEM_PROMPT：
    #   - COMPACTION_SYSTEM_PROMPT 已经在 run_compaction 里通过 system= 参数单独传了，
    #     再拼进用户消息就重复了（同一个指令出现两遍）。
    #   - COMPACTION_USER_PROMPT 是"针对这批素材的具体要求"，正该跟在素材后面。
    return f"<conversation>\n{conversation}\n</conversation>\n\n{COMPACTION_USER_PROMPT}"

@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """Prepared rows for one compaction run."""
    # 压缩"计划"：只描述打算怎么做，不执行任何动作。
    # 好处是可测试 —— 不用真的调模型，就能断言"该压哪几条"。
    replace_entry_ids: list[str | None]
    # 要被摘要取代的条目 id。将来写进 CompactionEntry.replaces_entry_ids，
    # 重放时 _apply_compaction 就是靠这些 id 定位要替换的行。
    # id 允许 None：harness 内存里有些消息没有对应 storage 条目
    # （手工 append / 从旧会话恢复的历史消息），这些消息照常压缩，
    # 只是落盘时被 known_ids 过滤跳过。
    messages_to_summarize: list[AgentMessage]
    # 要交给摘要模型去读的原始消息。

def select_compaction_rows(
        rows: list[tuple[str | None, AgentMessage]],
        *,
        keep_recent_tokens: int = DEFAULT_COMPACTION_KEEP_RECENT_TOKENS,
) -> CompactionPlan | None:
    """Select the compaction prefix while preserving the recent tail."""
    # rows 是 [(条目id, 消息), ...]，按时间从旧到新。
    # 返回 None 表示"这次不用压缩"（调用方看到 None 就该跳过整件事）。

    if not rows:
        return None

    if len(rows) == 1:
        message = rows[0][1]

        # 自动压缩仍不处理单条消息；手动全压可以处理一条真实消息。
        if keep_recent_tokens > 0:
            return None

        # 如果唯一一条已经是摘要，再摘要一次没有意义。
        if isinstance(message, UserMessage) and message.text.startswith(
            "Previous conversation summary:\n"
        ):
            return None

        return CompactionPlan(
            replace_entry_ids=[rows[0][0]],
            messages_to_summarize=[message],
        )

    first_kept = _first_recent_index(rows, keep_recent_tokens=keep_recent_tokens)
    # first_kept = 保留区的第一条消息下标。
    # 它前面（不含它）的全部压缩，它和它之后的原样保留。

    if first_kept is None or first_kept <= 0:
        # None：所有消息加起来都没超过预算，不用压。
        # <=0 ：保留区从 0 开始，等于"没有可压的前缀"，也不压。
        return None

    replaced = rows[:first_kept]
    return CompactionPlan(
        replace_entry_ids=[entry_id for entry_id, _message in replaced],
        messages_to_summarize=[message for _entry_id, message in replaced],
    )
    # 把 (id, message) 元组拆成两个平行列表：
    #   id 列表      -> 给 CompactionEntry 记录"我替换了谁"
    #   message 列表 -> 给摘要模型去读
    # 两个列表长度相同、元素一一对应。


def _first_recent_index(
        rows: list[tuple[str | None, AgentMessage]],
        *,
        keep_recent_tokens: int,
) -> int | None:
    """Return the first kept index: token budget from the tail, aligned to a user turn.

    做两件事：
      1) 从【最新】往回累加 token，累计到 >= keep_recent_tokens 就停 —— 这一段是"保留区"
      2) 把切分点【往后挪到第一条 user 消息】—— 保证不会把一问一答切断

    为什么必须对齐到 user？
      如果保留区从 assistant 或 toolResult 开始，模型会看到"没有提问的回答"、
      "没有调用的工具结果"，上下文就断了。所以宁可多留一点，也要从一次完整的提问开始。
    """
    if keep_recent_tokens <= 0:
        # 【/compact 的语义入口】预算 0 = "最近一条都不留" = 整个会话压成一条摘要。
        #
        # 同一个函数靠这个参数区分两种用法：
        #   自动压缩（超阈值触发，默认 20000）-> 保守：只压更早的，保留近期上下文
        #   用户显式 /compact            -> 激进：全部压掉，只留一条摘要
        # 自动触发必须保守（不能悄悄弄丢上下文），用户显式要求就该彻底（人家想重开）。
        #
        # ⚠️ 注意这一行【翻转】了 0 的语义：
        #    如果删掉它让 0 走主逻辑，第一条就满足 "累计 >= 0"，candidate 落在最后一条，
        #    然后"对齐到 user"在尾部找不到 user -> 返回 None（不压）。
        #    也就是说：有守卫 = 全压，没守卫 = 不压，两者正好相反。
        #
        # 端到端效果（实测）：6 条消息 + CompactionEntry(替换全部)
        #    -> 重放后 messages = ['Previous conversation summary:\n...']，6 条变 1 条。
        return len(rows)

    accumulate_tokens = 0
    candidate: int | None = None
    for index in range(len(rows) - 1, -1, -1):
        # range(最后, -1, -1) 就是"倒着走"：len-1, len-2, ..., 1, 0
        accumulate_tokens += estimate_message_tokens(rows[index][1])
        if accumulate_tokens >= keep_recent_tokens:
            candidate = index
            break
    if candidate is None:
        # 走完全部都没花完预算：说明消息总量还没超，不需要压缩。
        return None

    split = candidate
    while split < len(rows) and rows[split][1].role != "user":
        split += 1
    if split >= len(rows):
        # 最新回合可能尚无下一条 user。向前找到该回合起点，保留整个回合。
        for index in range(candidate, -1, -1):
            if rows[index][1].role == "user":
                return index if index > 0 else None
        return None
    return split

    # ── 数据样例（8 条消息，tokens 如下）──────────────────────────
    #   idx: 0        1          2           3          4      5          6      7
    #  role: user  assistant  toolResult  assistant   user  assistant   user  assistant
    # tokens: 10       10          9          10         6       10         6      10
    #
    # keep_recent_tokens = 30：
    #   倒着加：idx7=10 → idx6=16 → idx5=26 → idx4=32 >= 30 停 → candidate=4
    #   idx4 本身就是 user → split=4
    #   => 压缩 rows[0:4]，保留 rows[4:]
    #
    # keep_recent_tokens = 20：
    #   倒着加：idx7=10 → idx6=16 → idx5=26 >= 20 停 → candidate=5
    #   idx5 是 assistant，往后找 user → idx6 → split=6
    #   => 压缩 rows[0:6]，保留 rows[6:]
    #
    # keep_recent_tokens = 10：
    #   倒着加：idx7=10 >= 10 停 → candidate=7
    #   idx7 之后没有 user 了 → 返回 None（不压）
    #
    # keep_recent_tokens = 200：
    #   全加完才 71，没到 200 → candidate=None → 返回 None（不压）
    #
    # 注意：预算 30 和 60 会得到【相同】的 split=4，因为对齐步骤把它们
    #       吸附到了同一个 user 边界上。这是对齐带来的"粒度变粗"效果。

async def run_compaction(
    *,
    provider: ModelProvider,
    model: str,
    messages: list[AgentMessage],
    signal: CancellationToken | None = None,
) -> str:
    """Request a compaction summary from the provider and return it."""
    # 注意这里传入的 messages 是【待压缩的旧消息】，不是整个会话。
    # 整个会话要经过 select_compaction_rows 挑出前缀后才送到这里。
    # 把"待压缩的消息"变成"给摘要模型看的任务书"
    prompt = build_compaction_prompt(messages)

    # 摘要请求本身也是一次"对话"：只有一条用户消息，内容是要总结的对话 + 指令。
    # 不带任何工具 —— 摘要模型不需要干活，只需要读和写。
    summary_messages: list[AgentMessage] = [UserMessage(content=prompt)]
    text_parts: list[str] = []
    final_text: str | None = None

    source = provider.stream_response(
        model=model,
        system=COMPACTION_SYSTEM_PROMPT,
        messages=summary_messages,
        tools=[],
        signal=signal,
    )
    # 摘要失败、取消或调用方中断时，先等待 Provider 流关闭，再决定是否更新历史。
    # 正常完成时同样由上下文管理器回收流，避免把本次请求的资源留给下一轮。
    async with closing_async_iterator(source):
        async for event in source:
            if signal is not None and signal.is_cancelled():
                raise RuntimeError("Context compaction cancelled")
            if isinstance(event, TextDeltaEvent):
                # 流式增量：逐块收集，做"备选文本"。
                # 例：delta="用户要求" → delta="把登录" → delta="改成 JWT"
                text_parts.append(event.delta)
            elif isinstance(event, AssistantDoneEvent):
                # 流结束，拿到完整消息。优先用它，因为它最准确。
                final_text = event.message.text
            elif isinstance(event, AssistantErrorEvent):
                # 摘要失败不能静默 —— 否则会拿空摘要去替换真实历史，数据就丢了。
                raise RuntimeError(f"Context compaction failed: {event.reason}")

    # 两条取文本的路径，优先用 done 事件带来的完整文本：
    #   - 有 final_text  -> 用它（权威）
    #   - 没有           -> 把收集到的增量拼起来（有些 provider 不发 done）
    # 双保险是为了兼容不同 provider 的实现差异。
    summary = (final_text if final_text is not None else "".join(text_parts)).strip()
    if signal is not None and signal.is_cancelled():
        raise RuntimeError("Context compaction cancelled")
    if not summary:
        # 空摘要 = 会把历史抹成空白。这是不可接受的失败，必须炸出来。
        raise RuntimeError("Compaction summarization returned an empty summary")
    return summary

def format_compaction_summary(summary: str) -> str:
    return f"Previous conversation summary:\n{summary}"
