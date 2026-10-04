from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from minitau_agent.async_iterators import closing_async_iterator
from minitau_agent.compaction import (
    COMPACTION_SYSTEM_PROMPT,
    CompactionPlan,
    build_compaction_prompt,
    format_compaction_summary,
    run_compaction,
    select_compaction_rows,
)
from minitau_agent.context_window import (
    DEFAULT_COMPACTION_KEEP_RECENT_TOKENS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ContextWindowExceeded,
    auto_compaction_threshold_for_context_window,
    estimate_context_usage,
)
from minitau_agent.events import (
    AgentEvent,
    CompactionEndEvent,
    CompactionReason,
    CompactionStartEvent,
    MessageEndEvent,
)
from minitau_agent.loop import run_agent_loop
from minitau_agent.message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from minitau_agent.provider import ModelProvider
from minitau_agent.session.entries import (
    CompactionEntry,
    LeafEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionInfoEntry,
)
from minitau_agent.session.storage import SessionStorage
from minitau_agent.tools import AgentTool


@dataclass(frozen=True, slots=True)
class QueueMessages:
    """值快照，用于给调用方看现在队列里有什么，所以冻结"""
    steering: tuple[AgentMessage, ...] = ()
    follow_up: tuple[AgentMessage, ...] = ()

    @property
    def count(self) -> int:
        return len(self.steering) + len(self.follow_up)

@dataclass(slots=True)
class AgentHarnessConfig:
    provider: ModelProvider
    model: str
    system: str
    tools: list[AgentTool] = field(default_factory=list)
    # 每次创建新实例时，都会重新执行一次 list()
    # 生成一个全新的 独立的空列表
    max_turns: int | None = None
    storage: SessionStorage | None = None
    # 自动压缩
    context_window_tokens: int | None = None
    # 模型上下文窗口大小
    auto_compact_enabled: bool = True
    auto_compact_token_threshold: int | None = None
    # 显式压缩阈值；None 时由窗口推导：窗口 − 保留区


class SimpleCancellationToken:
    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

class AgentHarness:
    """ """

    def __init__(
            self,
            config: AgentHarnessConfig,
            *,
            messages: Sequence[AgentMessage] = (),
            last_entry_id: str | None = None,
            entry_ids: Sequence[str | None] | None = None,
                 ) -> None:
        self._config = config
        self._messages = list(messages)
        self._entry_ids: list[str | None] = (
            list(entry_ids) if entry_ids is not None else [None] * len(self._messages)
        )
        if len(self._entry_ids) != len(self._messages):
            raise ValueError("entry_ids and messages must have the same length")
        if (
            config.storage is not None
            and last_entry_id is not None
            and messages
            and entry_ids is None
        ):
            raise ValueError("Resuming a persisted session requires entry_ids for its messages")
        self._current_signal: SimpleCancellationToken | None = None
        self._running = False
        self._steering_queue: deque[AgentMessage] = deque()
        self._follow_up_queue: deque[AgentMessage] = deque()
        self._storage = config.storage
        self._compaction_failed_this_run = False
        self._last_entry_id = last_entry_id

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return tuple(self._messages)
    
    @property
    def auto_compact_token_threshold(self) -> int | None:
        if not self._config.auto_compact_enabled:
            return None
        if self._config.auto_compact_token_threshold is not None:
            return self._config.auto_compact_token_threshold
        window = self._config.context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
        return auto_compaction_threshold_for_context_window(window)

    @property
    def config(self) -> AgentHarnessConfig:
        return self._config

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def queued_messages(self) -> QueueMessages:
        return QueueMessages(tuple(self._steering_queue), tuple(self._follow_up_queue))

    def has_queued_messages(self) -> bool:
        return bool(self._steering_queue or self._follow_up_queue)

    def append_message(self, message: AgentMessage) -> None:
        self._messages.append(message)
        self._entry_ids.append(None)

    def replace_messages(
        self, messages: Sequence[AgentMessage], *, entry_ids: Sequence[str | None] | None = None
    ) -> None:
        if self._running:
            raise RuntimeError("Cannot replace messages while the agent is running")
        new_messages = list(messages)
        new_ids = list(entry_ids) if entry_ids is not None else [None] * len(new_messages)
        if len(new_ids) != len(new_messages):
            raise ValueError("entry_ids and messages must have the same length")
        self._messages[:] = new_messages
        self._entry_ids[:] = new_ids
        
    async def select_branch(
        self,
        *,
        target_id: str,
        messages: Sequence[AgentMessage],
        entry_ids: Sequence[str],
    ) -> None:
        """持久化分支选择，并同步内存上下文与下一条消息的父节点。"""
        self._ensure_not_running()
        if self._storage is None:
            raise RuntimeError("This session has no storage")
        if self.has_queued_messages():
            raise RuntimeError("Cannot branch while messages are queued")
        if any(entry_id is None for entry_id in self._entry_ids):
            raise RuntimeError("Cannot branch with unpersisted messages")

        new_messages = list(messages)
        new_ids = list(entry_ids)
        if len(new_messages) != len(new_ids):
            raise ValueError("entry_ids and messages must have the same length")

        self._running = True
        try:
            marker = LeafEntry(parent_id=target_id, entry_id=target_id)
            await self._storage.append(marker)

            # 写入成功后才改变当前进程的状态。
            self._messages[:] = new_messages
            self._entry_ids[:] = new_ids
            self._last_entry_id = target_id
        finally:
            self._running = False

    # 队列管理
    def cancel(self) -> None:
        if self._current_signal is not None:
            self._current_signal.cancel()

    def steer(self, content: str) -> QueueMessages:
        return self.steer_message(UserMessage(content=content))

    def follow_up(self, content: str) -> QueueMessages:
        return self.follow_up_message(UserMessage(content=content))

    def steer_message(self, message: AgentMessage) -> QueueMessages:
        self._steering_queue.append(message)
        return self.queued_messages

    def follow_up_message(self, message: AgentMessage) -> QueueMessages:
        self._follow_up_queue.append(message)
        return self.queued_messages

    def clear_queues(self) -> QueueMessages:
        snapshot = self.queued_messages
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        return snapshot
    # 讨一副人血的偏方

    def prompt_message(self, message: AgentMessage) -> AsyncIterator[AgentEvent]:
        # 这里只创建惰性事件流。调用方可能永远不迭代它，因此不能在这里占用运行状态
        # 或补写历史；真正开始消费时再由 _run 原子地完成这些操作。
        self._ensure_not_running()
        return self._run(prompts=(message,))

    def prompt(self, content: str) -> AsyncIterator[AgentEvent]:
        return self.prompt_message(UserMessage(content=content))

    def continue_(self) -> AsyncIterator[AgentEvent]:
        self._ensure_not_running()
        return self._run()



    def _ensure_not_running(self) -> None:
        if self._running:
            raise RuntimeError(
                "AgentHarness is already running; use steer() or follow_up() to queue messages."
            )

    def _append_interrupted_tool_results(self) -> None:
        """处理用户中断（Interrupt/Abort）时的上下文一致性问题"""
        returned_ids = {
            message.tool_call_id
            for message in self._messages
            if isinstance(message, ToolResultMessage)
        }
        for message in tuple(self._messages):
            # tuple(self._messages) 的防御性拷贝：
            # 我们在接下来的循环中会执行 self._messages.append(...)，
            # 固定遍历范围，避免新追加的结果也进入本轮遍历。
            if not isinstance(message, AssistantMessage):
                # 只有 AssistantMessage（助手消息）才可能包含 tool_calls（工具调用请求），
                # 所以跳过其他类型的消息
                continue
            for call in message.tool_calls:
                if call.id in returned_ids:
                    continue  # 如果已经有结果了，跳过
                returned_ids.add(call.id)
                self._messages.append(
                    ToolResultMessage(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content="Tool call interrupted by user",
                        is_error=True,
                    )
                )
                self._entry_ids.append(None)

    async def _record_message(self, message: AgentMessage) -> None:
        """ 将一条消息追加到会话历史中，并与上一条记录建立父子链接。"""
        entry_id: str | None = None
        if self._storage is not None:
            entry = MessageEntry(message=message, parent_id=self._last_entry_id)
            # 上锁，append 返回之后，_last_entry_id 才前移指向新链尾
            await self._storage.append(entry)
            self._last_entry_id = entry.id
            entry_id = entry.id
        self._entry_ids.append(entry_id)

    async def _persist_unrecorded_messages(self) -> None:
        """Write messages inserted outside normal MessageEnd events, including interrupted tools."""
        if self._storage is None:
            return
        for index, entry_id in enumerate(self._entry_ids):
            if entry_id is not None:
                continue
            entry = MessageEntry(message=self._messages[index], parent_id=self._last_entry_id)
            await self._storage.append(entry)
            self._entry_ids[index] = entry.id
            self._last_entry_id = entry.id

    def _compaction_plan(self) -> CompactionPlan | None:
        """满足两个条件才返回压缩计划：超窗口阈值 且 有超出尾部保留预算的前缀。"""
        threshold = self.auto_compact_token_threshold
        if threshold is None or threshold <= 0:
            return None
        if len(self._messages) < 2:
            return None
        usage = estimate_context_usage(
            system=self._config.system,
            messages=tuple(self._messages),
            tools=tuple(self._config.tools),
        )
        if usage.total_tokens <= threshold:
            return None
        rows = list(zip(self._entry_ids, self._messages, strict=True))
        window = self._config.context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
        keep_recent = min(DEFAULT_COMPACTION_KEEP_RECENT_TOKENS, max(1, window // 4))
        return select_compaction_rows(rows, keep_recent_tokens=keep_recent)

    async def _try_auto_compact(self) -> AsyncIterator[AgentEvent]:
        """达到自动压缩条件时，把计划交给共用执行器。"""
        if self._compaction_failed_this_run:
            return
        try:
            plan = self._compaction_plan()
        except Exception as exc:
            self._compaction_failed_this_run = True
            yield CompactionEndEvent(reason="auto", aborted=True, error_message=str(exc))
            return
        if plan is None:
            return
        # start/end 都由 _execute_compaction 发送；这里再发 start 会产生重复事件。
        async for event in self._execute_compaction(
            plan,
            reason="auto",
            signal=self._current_signal,
        ):
            if isinstance(event, CompactionEndEvent) and event.aborted:
                self._compaction_failed_this_run = True
            yield event

    async def _before_model_request(self) -> AsyncIterator[AgentEvent]:
        async for event in self._try_auto_compact():
            yield event
        window = self._config.context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
        usage = estimate_context_usage(
            system=self._config.system,
            messages=tuple(self._messages),
            tools=tuple(self._config.tools),
        )
        if usage.total_tokens >= window:
            raise ContextWindowExceeded(
                f"Context requires approximately {usage.total_tokens} tokens; "
                f"configured window is {window}"
            )

    async def _apply_compaction_in_place(
            self,
            replace_entry_ids: list[str | None],
            summary: str,
    ) -> None:
        """把压缩结果落盘（CompactionEntry）并替换内存里的消息前缀。"""
        replaced_count = len(replace_entry_ids)
        known_ids = [eid for eid in replace_entry_ids if eid is not None]
        if self._storage is not None and len(known_ids) != replaced_count:
            raise ValueError("Cannot persist compaction of messages without session entry IDs")
        summary_id: str | None = None
        if self._storage is not None:
            entry = CompactionEntry(
                parent_id=self._last_entry_id,
                summary=summary,
                replaces_entry_ids=known_ids,
            )
            await self._storage.append(entry)
            self._last_entry_id = entry.id
            summary_id = entry.id
        # 内存行与磁盘条目保持同一投影：摘要行占住被替换行原来的位置
        summary_message = UserMessage(content=format_compaction_summary(summary))
        self._messages[:] = [summary_message] + self._messages[replaced_count:]
        self._entry_ids[:] = [summary_id] + self._entry_ids[replaced_count:]

    async def _run(
            self,
            *,
            prompts: Sequence[AgentMessage] = (),
    ) -> AsyncIterator[AgentEvent]:
        # 异步生成器到第一次 __anext__() 才运行。此处没有 await，因此并发启动的
        # 两条流也只能有一条先通过检查并占用 Harness；另一条会在入口报错。
        self._ensure_not_running()
        self._append_interrupted_tool_results()
        signal = SimpleCancellationToken()
        self._running = True
        self._current_signal = signal
        self._compaction_failed_this_run = False
        try:
            await self._persist_unrecorded_messages()
            loop_events = run_agent_loop(
                provider=self._config.provider,
                model=self._config.model,
                system=self._config.system,
                messages=self._messages,
                prompts=prompts,
                tools=self._config.tools,
                max_turns=self._config.max_turns,
                signal=signal,
                get_steering_messages=self._drain_steering_messages,
                get_follow_up_messages=self._drain_follow_up_messages,
                before_model_request=self._before_model_request,
            )

            # _run() 会把事件继续 yield 给 CLI/REPL；调用方可能在任意事件处提前关闭。
            # async for 负责转发事件，外层上下文管理器负责在退出时关闭子 Agent Loop。
            # 先等待子循环清理完成，再进入下面的收尾压缩和状态复位。
            async with closing_async_iterator(loop_events):
                async for event in loop_events:
                    if isinstance(event, MessageEndEvent):
                        await self._record_message(event.message)
                    yield event
            # 只有循环自然结束才会走到这里；提前关闭或异常会直接进入 finally。
            # 此时本轮消息已经落定，才适合评估是否需要自动压缩。
            final_message = self._messages[-1] if self._messages else None
            failed = (
                isinstance(final_message, AssistantMessage)
                and final_message.stop_reason in {"error", "aborted"}
            )
            if not signal.is_cancelled() and not failed:
                async for event in self._try_auto_compact():
                    yield event
        finally:
            try:
                if signal.is_cancelled():
                    self._append_interrupted_tool_results()
                    await self._persist_unrecorded_messages()
            finally:
                # 补写中断消息也可能因磁盘故障失败；运行状态不能因此卡在 busy。
                # 身份比较只清理本轮创建的令牌，不误删之后设置的令牌。
                if self._current_signal is signal:
                    self._current_signal = None
                self._running = False

    def _drain_steering_messages(self) -> tuple[AgentMessage, ...]:
        return self._drain_queue(self._steering_queue)
    def _drain_follow_up_messages(self) -> tuple[AgentMessage, ...]:
        return self._drain_queue(self._follow_up_queue)

    def _drain_queue(self, queue: deque[AgentMessage]) -> tuple[AgentMessage, ...]:
        if not queue:
            return ()
        messages = tuple(queue)
        queue.clear()
        return messages

    async def append_session_info(
        self,
        *,
        cwd: str,
        title: str,
    ) -> SessionInfoEntry:
        """追加改名记录，并由 Harness 维护正确的 parent_id。
        SessionManager 直接 storage.append(info)？
        因为 Harness 持有当前链尾 _last_entry_id。
        假设旧链尾是 A，改名追加 B(parent=A) 后，下一条用户消息必须是 C(parent=B)。
        如果绕过 Harness 写入 B，Harness 仍以为链尾是 A，就会错误写出 C(parent=A)
        """
        self._ensure_not_running()
        if self._storage is None:
            raise RuntimeError("This session has no storage")

        # 设置运行标志后再发生 await，其他会话操作就不能插进来。
        self._running = True
        try:
            await self._persist_unrecorded_messages()
            info = SessionInfoEntry(
                cwd=cwd,
                title=title,
                parent_id=self._last_entry_id,
            )
            await self._storage.append(info)
            self._last_entry_id = info.id
            return info
        finally:
            self._running = False

    async def append_model_change(
        self,
        *,
        provider_name: str,
        model: str,
        context_window_tokens: int | None,
    ) -> ModelChangeEntry:
        """先写配置，再推进链尾；失败时运行中的模型保持不变。"""
        self._ensure_not_running()
        if self._storage is None:
            raise RuntimeError("This session has no storage")
        if self.has_queued_messages():
            raise RuntimeError("Cannot change model while messages are queued")

        self._running = True
        try:
            await self._persist_unrecorded_messages()
            entry = ModelChangeEntry(
                parent_id=self._last_entry_id,
                provider_name=provider_name,
                model=model,
                context_window_tokens=context_window_tokens,
            )
            await self._storage.append(entry)
            self._last_entry_id = entry.id
            return entry
        finally:
            self._running = False
            

    # 手动compact
    def compact(
        self,
        *,
        keep_recent_tokens: int = 0,
    ) -> AsyncIterator[AgentEvent]:
        if keep_recent_tokens < 0:
            raise ValueError("keep_recent_tokens must be non-negative")

        # 与 prompt() 一样：运行中调用要立即报错。
        self._ensure_not_running()
        return self._run_manual_compaction(keep_recent_tokens=keep_recent_tokens)

    async def _run_manual_compaction(
        self,
        *,
        keep_recent_tokens: int,
    ) -> AsyncIterator[AgentEvent]:
        # compact() 只创建惰性流；第一次读取事件才执行到这里。
        # 再检查一次，防止两条在空闲时创建的流随后同时启动。
        self._ensure_not_running()
        # 上一次中断可能留下没有结果的工具调用；先补齐历史再生成摘要。
        self._append_interrupted_tool_results()

        # 与普通 prompt 共用运行标志和取消令牌，因此运行期间不能再启动其他操作。
        signal = SimpleCancellationToken()
        self._running = True
        self._current_signal = signal

        try:
            # 手工插入但尚未落盘的消息先取得 entry ID，后续 CompactionEntry
            # 才能准确记录自己替换了哪些消息。
            await self._persist_unrecorded_messages()

            # 每条有效消息与它的存储 ID 配对；strict=True 能及时发现长度不同步。
            rows = list(zip(self._entry_ids, self._messages, strict=True))
            plan = select_compaction_rows(
                rows,
                keep_recent_tokens=keep_recent_tokens,
            )

            if plan is None:
                # 没有可压缩前缀：显式告知调用方本次跳过，不请求模型。
                yield CompactionEndEvent(reason="manual", skipped=True)
                return

            # 自动和手动压缩共用摘要请求、落盘及事件发送逻辑。
            async for event in self._execute_compaction(
                plan,
                reason="manual",
                signal=signal,
            ):
                yield event
        finally:
            # 正常结束、提前关闭事件流或外层取消，都必须释放会话运行状态。
            if self._current_signal is signal:
                self._current_signal = None
            self._running = False

    def _check_compaction_request_size(self, plan: CompactionPlan) -> None:
        """估算摘要请求的体积；明显超出窗口时不调用 Provider。"""
        # 摘要模型看到的是重新包装后的 prompt，不是直接看到原消息列表。
        prompt = build_compaction_prompt(plan.messages_to_summarize)
        # 请求只含摘要 system 和一条 user 消息，不包含编码工具定义。
        estimate = estimate_context_usage(
            system=COMPACTION_SYSTEM_PROMPT,
            messages=(UserMessage(content=prompt),),
            tools=(),
        )
        window = self._config.context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS

        # 这是近似估算，作用是提前挡住确定过大的请求；不能保证端点一定接收。
        if estimate.total_tokens >= window:
            raise ContextWindowExceeded(
                f"Compaction request needs approximately {estimate.total_tokens} "
                f"tokens; configured window is {window}"
            )

    async def _execute_compaction(
        self,
        plan: CompactionPlan,
        *,
        reason: CompactionReason,
        signal: SimpleCancellationToken | None,
    ) -> AsyncIterator[AgentEvent]:
        """执行一份已选好的压缩计划；自动与手动入口共用。"""
        # 先通知调用方进入压缩阶段。此时尚未请求模型，也尚未修改历史。
        yield CompactionStartEvent(reason=reason)

        try:
            # 调用方可能在收到 start 后立刻取消，因此请求前先检查令牌。
            if signal is not None and signal.is_cancelled():
                raise RuntimeError("Context compaction cancelled")

            self._check_compaction_request_size(plan)

            # 只把计划选中的旧消息送去总结；摘要完成前，原历史保持不变。
            summary = await run_compaction(
                provider=self._config.provider,
                model=self._config.model,
                messages=plan.messages_to_summarize,
                signal=signal,
            )

            # 摘要返回与提交落盘之间也可能发生取消，提交前必须再检查一次。
            if signal is not None and signal.is_cancelled():
                raise RuntimeError("Context compaction cancelled")

            # 先追加 CompactionEntry，成功后才在内存中用摘要替换旧消息。
            await self._apply_compaction_in_place(plan.replace_entry_ids, summary)
        except Exception as exc:
            # 普通失败转换成结束事件，供界面展示；不会把失败摘要写进有效上下文。
            # 外层 task.cancel() 属于 CancelledError，不在这里吞掉，交给上层 finally 清理。
            yield CompactionEndEvent(
                reason=reason,
                aborted=True,
                error_message=str(exc),
            )
            return

        # 只有完成持久化和内存替换后才报告成功。
        yield CompactionEndEvent(reason=reason)
