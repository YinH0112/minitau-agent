"""解析斜杠命令，并调用 CodingSession 的公开接口。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from minitau_agent.async_iterators import closing_async_iterator
from minitau_agent.events import AgentEvent, CompactionEndEvent
from minitau_coding.session import CodingSession
from minitau_coding.session_manager import SessionManager

# 事件接收器由调用方提供。命令层不知道事件最终要打印到哪里。
type EventSink = Callable[[AgentEvent], Awaitable[None]]
# 零件 1：type X = ... 明确告诉读代码的人和类型检查器——这是一个类型别名
# 零件 2：Callable[[AgentEvent], R] 
#    这个东西是一个函数/可调用对象，它：
#    接收 1 个参数，类型是 AgentEvent；
#    返回一个 Awaitable[None]
# 零件 3：Awaitable[None]（可等待，且无返回值）
#    Awaitable[T]：任何可以被 await 的对象（最常见就是 async def 定义的协程）。
#    它表示“这个函数不能直接同步调用，必须 await 它”。
#    [None]：等待完成后不返回有效数据（返回 None）。
#    合起来 Awaitable[None] ≈ “一个 async def ... -> None 形式的函数调用结果”

@dataclass(frozen=True, slots=True)
class CommandContext:
    """命令执行的“工作台”
操作当前会话（session.prompt(...)、session.cancel()）；
创建/恢复会话（manager）；
向界面推送事件（on_event)
    """
    session: CodingSession # 当前聊天会话
    manager: SessionManager # 会话管理器
    on_event: EventSink # 事件回调


@dataclass(frozen=True, slots=True)
class CommandResult:
    """命令执行的“回执单”"""
    handled: bool  # 必填：这个命令我处理了吗？
    message: str | None = None  # 可选：要给用户显示的提示文字
    exit_requested: bool = False  # 可选：是否要退出程序（如 /quit）
    error: str | None = None  # 可选：错误信息
    prompt: str | None = None  # 可选：命令转换后的真正提问文本


type CommandHandler = Callable[[CommandContext, str], Awaitable[CommandResult]]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """命令的“登记卡片”
    把一条命令的全部元信息（叫什么、怎么用、干什么、谁来干）捆在一张卡片上。系统内部就是一个命令表
    """
    name: str
    usage: str
    help: str
    handler: CommandHandler


def _reject_extra(argument: str, usage: str) -> CommandResult | None:
    """无参数命令收到多余参数时，返回用法错误。"""
    if argument:
        return CommandResult(handled=True, error=f"Usage: {usage}")
    return None


async def _help(context: CommandContext, argument: str) -> CommandResult:
    error = _reject_extra(argument, "/help")
    if error is not None:
        return error

    lines = [
        f"{spec.usage} — {spec.help}"
        for spec in COMMANDS.values()
    ]
    return CommandResult(handled=True, message="\n".join(lines))


async def _status(context: CommandContext, argument: str) -> CommandResult:
    error = _reject_extra(argument, "/status")
    if error is not None:
        return error

    status = context.session.status
    message = (
        f"Session: {status.session_id}\n"
        f"Working directory: {status.cwd}\n"
        f"Model: {status.model}\n"
        f"Context: approximately {status.estimated_tokens}/"
        f"{status.context_window_tokens} tokens\n"
        f"Running: {status.is_running}"
    )
    return CommandResult(handled=True, message=message)


async def _sessions(context: CommandContext, argument: str) -> CommandResult:
    error = _reject_extra(argument, "/sessions")
    if error is not None:
        return error

    summaries = await context.manager.list_sessions(
        current_session_id=context.session.session_id
    )
    if not summaries:
        return CommandResult(handled=True, message="No sessions found.")

    lines: list[str] = []
    for item in summaries:
        marker = "*" if item.is_current else " "
        updated = datetime.fromtimestamp(
            item.updated_at_ns / 1_000_000_000
        ).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(
            f"{marker} {item.session_id}  {updated}  {item.title}"
        )

    return CommandResult(handled=True, message="\n".join(lines))


async def _new(context: CommandContext, argument: str) -> CommandResult:
    error = _reject_extra(argument, "/new")
    if error is not None:
        return error

    await context.session.start_new(context.manager, title="")
    return CommandResult(
        handled=True,
        message=f"Created session: {context.session.session_id}",
    )


async def _resume(context: CommandContext, argument: str) -> CommandResult:
    if not argument:
        return CommandResult(
            handled=True,
            error="Usage: /resume <session ID or unique prefix>",
        )

    await context.session.switch_to(context.manager, argument)
    return CommandResult(
        handled=True,
        message=f"Resumed session: {context.session.session_id}",
    )


async def _name(context: CommandContext, argument: str) -> CommandResult:
    if not argument:
        return CommandResult(
            handled=True,
            error="Usage: /name <title>",
        )

    await context.session.rename(argument)
    return CommandResult(
        handled=True,
        message=f"Renamed session to: {argument.strip()}",
    )


async def _compact(context: CommandContext, argument: str) -> CommandResult:
    error = _reject_extra(argument, "/compact")
    if error is not None:
        return error

    stream = context.session.compact()
    final_event: CompactionEndEvent | None = None

    # 命令层负责把整个操作消费完；调用方提供的 on_event 决定如何展示事件。
    async with closing_async_iterator(stream):
        async for event in stream:
            await context.on_event(event)
            if isinstance(event, CompactionEndEvent):
                final_event = event

    if final_event is None:
        return CommandResult(
            handled=True,
            error="Compaction ended without a result.",
        )
    if final_event.skipped:
        return CommandResult(
            handled=True,
            message="There is no history to compact.",
        )
    if final_event.aborted:
        return CommandResult(
            handled=True,
            error=final_event.error_message or "Compaction failed.",
        )

    return CommandResult(handled=True, message="Context compacted.")


async def _exit(context: CommandContext, argument: str) -> CommandResult:
    error = _reject_extra(argument, "/exit")
    if error is not None:
        return error

    # 这里只返回退出意图。关闭 Provider 是应用入口或 REPL 的职责。
    return CommandResult(handled=True, exit_requested=True)

async def _model(context: CommandContext, argument: str) -> CommandResult:
    """无参数查询当前模型；有参数切换下一轮使用的模型。"""
    if not argument:
        return CommandResult(
            handled=True,
            message=f"Current model: {context.session.status.model}",
        )

    await context.session.set_model(argument)
    return CommandResult(
        handled=True,
        message=f"Current model: {context.session.status.model}",
    )

async def _tree(context: CommandContext, argument: str) -> CommandResult:
    error = _reject_extra(argument, "/tree")
    if error is not None:
        return error

    choices = await context.session.tree_choices()
    if not choices:
        return CommandResult(
            handled=True,
            message="No safe branch points yet.",
        )

    lines: list[str] = []
    for choice in choices:
        marker = "*" if choice.active else " "
        parent = (
            choice.branch_parent_id[:8]
            if choice.branch_parent_id is not None
            else "root"
        )
        lines.append(
            f"{marker} {choice.entry_id}  <- {parent}  {choice.label}"
        )

    return CommandResult(handled=True, message="\n".join(lines))


async def _branch(context: CommandContext, argument: str) -> CommandResult:
    parts = argument.split()
    if len(parts) != 1:
        return CommandResult(
            handled=True,
            error="Usage: /branch <entry_id>",
        )

    entry_id = parts[0]
    await context.session.branch_to(entry_id)
    return CommandResult(
        handled=True,
        message=f"Active branch: {entry_id}",
    )

COMMANDS: dict[str, CommandSpec] = {
    "help": CommandSpec("help", "/help", "Show commands.", _help),
    "status": CommandSpec("status", "/status", "Show session status.", _status),
    "sessions": CommandSpec("sessions", "/sessions", "List sessions.", _sessions),
    "new": CommandSpec("new", "/new", "Create and switch to a session.", _new),
    "resume": CommandSpec(
        "resume",
        "/resume <id>",
        "Switch to an existing session.",
        _resume,
    ),
    "name": CommandSpec("name", "/name <title>", "Rename this session.", _name),
    "compact": CommandSpec("compact", "/compact", "Summarize current history.", _compact),
    "exit": CommandSpec("exit", "/exit", "Leave the application.", _exit),
    "model": CommandSpec(
        "model",
        "/model [name]",
        "Show or change the current model.",
        _model,
    ),
    "tree": CommandSpec("tree", "/tree", "List safe branch points.", _tree),
    "branch": CommandSpec("branch", "/branch <entry_id>", "Select a branch point.", _branch),
}


async def dispatch_input(
    line: str,
    context: CommandContext,
) -> CommandResult:
    """把一行输入分类为普通提问、转义提问或斜杠命令。"""
    text = line.strip()
    if not text:
        return CommandResult(handled=True)

    # //help 表示真正发给模型的 /help，不执行内置命令。
    if text.startswith("//"):
        return CommandResult(handled=False, prompt=text[1:])

    if not text.startswith("/"):
        return CommandResult(handled=False, prompt=text)

    # maxsplit=1 只分离命令词；标题内部的空格仍留在 argument 中。
    parts = text.split(maxsplit=1)
    name = parts[0][1:].lower()
    argument = parts[1] if len(parts) == 2 else ""

    spec = COMMANDS.get(name)
    if spec is None:
        return CommandResult(
            handled=True,
            error=f"Unknown command /{name}; use /help to see available commands.",
        )

    try:
        return await spec.handler(context, argument)
    except (ValueError, RuntimeError) as exc:
        # 会话层的“无匹配”“正在运行”等可预期错误变成命令结果。
        return CommandResult(handled=True, error=str(exc))
