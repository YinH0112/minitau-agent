"""CodingSession：把项目配置、持久化历史和 AgentHarness 组成一个会话。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from minitau_agent.context_window import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    estimate_context_usage,
)
from minitau_agent.events import AgentEvent
from minitau_agent.harness import AgentHarness, AgentHarnessConfig
from minitau_agent.message import AgentMessage, AssistantMessage, ToolResultMessage
from minitau_agent.provider import ModelProvider
from minitau_agent.session.entries import (
    CompactionEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionEntry,
    SessionInfoEntry,
)
from minitau_agent.session.memory import SessionState
from minitau_agent.session.storage import JsonlSessionStorage
from minitau_agent.session.tree import entries_by_id
from minitau_coding.context import discover_project_context
from minitau_coding.session_manager import SessionManager, SessionSnapshot
from minitau_coding.system_prompt import BuildSystemPromptOptions, build_system_prompt
from minitau_coding.tools import create_coding_tools


@dataclass(frozen=True, slots=True)
class CodingSessionConfig:
    cwd: Path
    provider: ModelProvider
    model: str
    context_window_tokens: int | None = None
    provider_name: str = "fake"
    provider_explicit: bool = False
    model_explicit: bool = False
    context_window_explicit: bool = False
    # 由应用入口提供，负责依据本地设置创建并管理新 Provider。
    # Session 仍然只借用实例，离线测试或嵌入式调用可以不提供。
    provider_factory: Callable[[str | None], tuple[ModelProvider, str]] | None = None


def _effective_config(
    launch: CodingSessionConfig,
    stored: ModelChangeEntry | None,
) -> CodingSessionConfig:
    """启动参数优先；跨供应商恢复时必须连 Provider 实例一起恢复。"""
    if stored is None:
        return launch
    if stored.provider_name != launch.provider_name:
        if not launch.provider_explicit and launch.provider_factory is not None:
            provider, _default_model = launch.provider_factory(stored.provider_name)
            launch = replace(launch, provider=provider, provider_name=stored.provider_name)
        elif not launch.provider_explicit:
            raise ValueError(
                f"Session uses provider {stored.provider_name!r}; "
                f"current provider is {launch.provider_name!r}"
            )
        else:
            # 显式 --provider 选择了另一家时，不能沿用旧 Provider 的模型和窗口。
            return launch
    return replace(
        launch,
        model=launch.model if launch.model_explicit else stored.model,
        context_window_tokens=(
            launch.context_window_tokens
            if launch.context_window_explicit
            else stored.context_window_tokens
        ),
    )

@dataclass(frozen=True, slots=True)
class CodingSessionStatus:
    session_id: str
    cwd: Path
    model: str
    estimated_tokens: int
    context_window_tokens: int
    is_running: bool

@dataclass(frozen=True, slots=True)
class TreeChoice:
    entry_id: str
    branch_parent_id: str | None
    label: str
    active: bool


def _branch_state_if_safe(
    entries: list[SessionEntry],
    entry: SessionEntry,
) -> SessionState | None:
    """安全节点返回其重放状态；不可分叉的节点返回 None。"""
    if isinstance(entry, CompactionEntry):
        pass
    elif isinstance(entry, MessageEntry) and isinstance(
        entry.message, AssistantMessage
    ):
        assistant = entry.message
        if assistant.stop_reason != "stop" or assistant.tool_calls:
            return None
    else:
        return None

    state = SessionState.from_entries(entries, leaf_id=entry.id)

    pending_calls: set[str] = set()
    for message in state.messages:
        if isinstance(message, AssistantMessage):
            pending_calls.update(call.id for call in message.tool_calls)
        elif isinstance(message, ToolResultMessage):
            pending_calls.discard(message.tool_call_id)

    return None if pending_calls else state


def _tree_preview(text: str) -> str:
    """把多行内容变成适合终端显示的一行短预览。"""
    printable = "".join(char if char.isprintable() else " " for char in text)
    one_line = " ".join(printable.split())
    if not one_line:
        return "(empty)"
    return one_line[:80] + ("…" if len(one_line) > 80 else "")

def _build_harness_config(
    config: CodingSessionConfig,
    storage: JsonlSessionStorage,
) -> AgentHarnessConfig:
    """把同一个项目目录对应的工具和系统提示词装配到 Harness。"""
    tools = create_coding_tools(cwd=config.cwd)
    context_files = discover_project_context(config.cwd)
    system = build_system_prompt(
        BuildSystemPromptOptions(
            cwd=config.cwd,
            tools=tuple(tools),
            context_files=context_files,
        )
    )
    return AgentHarnessConfig(
        provider=config.provider,
        model=config.model,
        system=system,
        tools=tools,
        storage=storage,
        context_window_tokens=config.context_window_tokens,
    )


class CodingSession:
    def __init__(
        self,
        *,
        config: CodingSessionConfig,
        session_id: str,
        harness: AgentHarness,
    ) -> None:
        self._launch_config = config
        self._config = config
        self._session_id = session_id
        self._harness = harness

    # 有@classmethod（类方法）：不需要任何实例，直接报出大名 CodingSession.new(...) 就能直接调用
    @classmethod
    async def new(
        cls,
        config: CodingSessionConfig,
        *,
        session_id: str,
        title: str,
    ) -> CodingSession:
        """创建会话文件和首条元信息，不调用模型。"""
        path = config.cwd / ".minitau" / "sessions" / f"{session_id}.jsonl"
        if path.exists():
            raise ValueError(f"Session {session_id} already exists")

        storage = JsonlSessionStorage(path)
        harness_config = _build_harness_config(config, storage)

        info = SessionInfoEntry(cwd=str(config.cwd), title=title)
        await storage.append(info)
        model_entry = ModelChangeEntry(
            parent_id=info.id,
            provider_name=config.provider_name,
            model=config.model,
            context_window_tokens=config.context_window_tokens,
        )
        await storage.append(model_entry)

        harness = AgentHarness(
            harness_config,
            last_entry_id=model_entry.id,
        )
        return cls(config=config, session_id=session_id, harness=harness)

    @classmethod
    async def resume(
        cls,
        config: CodingSessionConfig,
        *,
        path: Path,
    ) -> CodingSession:
        """重放原文件，并从最后一个条目继续追加。
1.读全量日志：entries = await storage.read_all()，把整个 .jsonl 文件的所有历史行读入内存；
2.状态重放：state = SessionState.from_entries(entries)：
        像放电影一样，把从第 0 行到最后一行发生的所有事件重放一遍，
在内存中瞬间还原出崩溃或退出前的完整对话树（messages）和上下文（entry_ids）；
3.无缝衔接： 把还原出来的历史消息 messages 塞给大脑 AgentHarness，
并把指针标记在最后一行 last_entry_id=entries[-1].id。
这样，下一次你再向 Agent 提问时，它就能完全记住上一轮退出了的记忆，继续往文件末尾追加新内容
        """
        manager = SessionManager(config.cwd)
        if path.resolve().parent != manager.sessions_dir:
            raise ValueError("Session file is outside the current project")

        snapshot = await manager.load(path.stem)
        return cls.from_snapshot(config, snapshot)

    async def branch_to(self, entry_id: str) -> None:
        """选择完整助手回合或压缩节点作为新分支起点。"""
        self._ensure_idle("branch")
        target_id = entry_id.strip()
        if not target_id:
            raise ValueError("Branch entry ID cannot be empty")

        manager = SessionManager(self._config.cwd)
        # 先复用现有加载器检查整份文件，避免在损坏的会话上追加标记。
        await manager.load(self._session_id)

        path = manager.sessions_dir / f"{self._session_id}.jsonl"
        entries = await JsonlSessionStorage(path).read_all()
        selected = entries_by_id(entries).get(target_id)
        if selected is None:
            raise ValueError(f"Unknown session entry: {target_id}")

        state = _branch_state_if_safe(entries, selected)
        if state is None:
            raise ValueError("Branch point must be a safe complete assistant turn or compaction")

        # 先准备目标 Provider；配置缺失时不能先落盘 LeafEntry 再报错。
        effective = _effective_config(self._launch_config, state.model_change)
        await self._harness.select_branch(
            target_id=target_id,
            messages=state.messages,
            entry_ids=state.context_entry_ids,
        )
        self._apply_model_config(effective)

    async def tree_choices(self) -> tuple[TreeChoice, ...]:
        """读取可分叉节点，供 REPL 展示。"""
        self._ensure_idle("show tree")

        manager = SessionManager(self._config.cwd)
        snapshot = await manager.load(self._session_id)
        entries = await JsonlSessionStorage(snapshot.path).read_all()
        by_id = entries_by_id(entries)

        safe_ids = {
            entry.id
            for entry in entries
            if _branch_state_if_safe(entries, entry) is not None
        }

        def closest_safe_parent(parent_id: str | None) -> str | None:
            current = parent_id
            while current is not None:
                if current in safe_ids:
                    return current
                current = by_id[current].parent_id
            return None

        choices: list[TreeChoice] = []
        for entry in entries:
            if entry.id not in safe_ids:
                continue

            if isinstance(entry, CompactionEntry):
                label = f"compaction: {_tree_preview(entry.summary)}"
            elif isinstance(entry, MessageEntry) and isinstance(
                entry.message, AssistantMessage
            ):
                label = f"assistant: {_tree_preview(entry.message.text)}"
            else:
                continue

            choices.append(
                TreeChoice(
                    entry_id=entry.id,
                    branch_parent_id=closest_safe_parent(entry.parent_id),
                    label=label,
                    active=entry.id == snapshot.last_entry_id,
                )
            )

        return tuple(choices)

    @classmethod
    def from_snapshot(
        cls,
        config: CodingSessionConfig,
        snapshot: SessionSnapshot,
    ) -> CodingSession:
        """从已验证的快照构造候选会话，不修改原会话。"""
        effective = _effective_config(config, snapshot.model_change)
        storage = JsonlSessionStorage(snapshot.path)
        harness = AgentHarness(
            _build_harness_config(effective, storage),
            messages=snapshot.messages,
            entry_ids=snapshot.entry_ids,
            last_entry_id=snapshot.last_entry_id,
        )
        session = cls(
            config=config,
            session_id=snapshot.session_id,
            harness=harness,
        )
        session._config = effective
        return session

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def provider_name(self) -> str:
        return self._config.provider_name

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return self._harness.messages

    @property
    def is_running(self) -> bool:
        return self._harness.is_running

    @property
    def status(self) -> CodingSessionStatus:
        harness_config = self._harness.config
        usage = estimate_context_usage(
            system=harness_config.system,
            messages=self.messages,
            tools=tuple(harness_config.tools),
        )
        window = self._config.context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS

        return CodingSessionStatus(
            session_id=self.session_id,
            cwd=self._config.cwd,
            model=self._config.model,
            estimated_tokens=usage.total_tokens,
            context_window_tokens=window,
            is_running=self.is_running,
        )

    def prompt(self, text: str) -> AsyncIterator[AgentEvent]:
        return self._harness.prompt(text)

    def cancel(self) -> None:
        self._harness.cancel()

    async def set_model(self, model: str) -> None:
        """把模型选择写入当前分支，然后更新下一轮请求配置。"""
        self._ensure_idle("change model")

        selected = model.strip()
        if not selected:
            raise ValueError("Model name cannot be empty")
        if any(character.isspace() for character in selected):
            raise ValueError("Model name cannot contain whitespace")

        if selected == self._config.model:
            return
        await self._harness.append_model_change(
            provider_name=self._config.provider_name,
            model=selected,
            context_window_tokens=self._config.context_window_tokens,
        )
        # 写入成功后才切换；持久化失败不会出现界面与日志分歧。
        self._apply_model_config(replace(self._config, model=selected))

    async def set_provider(self, name: str) -> None:
        """先创建客户端，再写分支配置，最后同时切换 Provider 和模型。"""
        self._ensure_idle("change provider")
        factory = self._config.provider_factory
        if factory is None:
            raise ValueError("当前入口不支持供应商切换；请使用交互 CLI")
        # 即使名字相同也重建客户端：/config 可能刚刚更换了 key 或端点。
        provider, model = factory(name)
        config = replace(
            self._config,
            provider=provider,
            provider_name=name,
            model=model,
            context_window_tokens=None,
            provider_explicit=False,
            model_explicit=False,
            context_window_explicit=False,
        )
        await self._harness.append_model_change(
            provider_name=name, model=model, context_window_tokens=None
        )
        self._apply_model_config(config)
        # 用户在界面里作出新选择后，后续 /resume、/branch 再按目标分支恢复。
        self._launch_config = config

    def _apply_model_config(self, config: CodingSessionConfig) -> None:
        self._harness.config.provider = config.provider
        self._harness.config.model = config.model
        self._harness.config.context_window_tokens = config.context_window_tokens
        self._config = config

    def _ensure_idle(self, act: str = "change") -> None:
        """检查当前是否空闲"""
        if self._harness.is_running:
            raise RuntimeError(f"Cannot {act} session while the agent is running")

    def _ensure_cwd(self, manager: SessionManager) -> None:
        if manager.cwd != self._config.cwd.resolve():
            raise ValueError("Session manager belongs to a different cwd")

    async def switch_to(
            self,
            manager: SessionManager,
            ref: str,
        ) -> None:
        """完整装载目标会话后，再替换当前状态"""
        self._ensure_idle("switch")
        self._ensure_cwd(manager)

        snapshot = await manager.load(ref)

        candidate = type(self).from_snapshot(self._launch_config, snapshot)

        self._ensure_idle()
        self._harness = candidate._harness
        self._session_id = candidate._session_id
        self._config = candidate._config



    async def start_new(self,
                        manager: SessionManager,
                        *,
                        title: str = "",) -> None:
        """新会话创建成功后，再替换当前状态"""
        self._ensure_idle("create")
        self._ensure_cwd(manager)

        candidate = await type(self).new(
            self._config,
            session_id=manager.create(),
            title=title,
        )
        self._harness = candidate._harness
        self._session_id = candidate._session_id
        self._config = candidate._config

    async def rename(self, title: str) -> None:
        self._ensure_idle("rename")
        title = title.strip()
        if not title:
            raise ValueError("Session title cannot be empty")

        await self._harness.append_session_info(
            cwd=str(self._config.cwd),
            title=title,
        )

    def compact(
        self,
        *,
        keep_recent_tokens: int = 0,
    ) -> AsyncIterator[AgentEvent]:
        return self._harness.compact(keep_recent_tokens=keep_recent_tokens)




