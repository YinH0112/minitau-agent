"""Command-line entry point for the minitau coding-agent harness."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Annotated

import typer

from minitau_agent.async_iterators import closing_async_iterator
from minitau_agent.provider import ModelProvider
from minitau_ai.factory import create_provider
from minitau_coding import __version__
from minitau_coding.rendering import EventRenderer
from minitau_coding.repl import run_repl
from minitau_coding.session import CodingSession, CodingSessionConfig
from minitau_coding.session_manager import SessionManager, SessionSnapshot


async def _open_session(
    *,
    manager: SessionManager,
    provider_name: str | None,
    model_name: str | None,
    context_window: int | None,
    resume: str | None,
    continue_latest: bool,
    title: str,
) -> tuple[CodingSession, ModelProvider]:
    """先读快照再建 Provider；失败时由本函数关闭新建的实例。"""
    snapshot: SessionSnapshot | None = None
    if continue_latest:
        snapshot = await manager.latest()
    elif resume is not None:
        snapshot = await manager.load(resume)

    stored_provider = (
        snapshot.model_change.provider_name
        if snapshot is not None and snapshot.model_change is not None
        else None
    )
    chosen_provider = provider_name or stored_provider or "fake"
    provider, default_model = create_provider(chosen_provider)
    try:
        config = CodingSessionConfig(
            cwd=manager.cwd,
            provider=provider,
            provider_name=chosen_provider,
            provider_explicit=provider_name is not None,
            model=model_name or default_model,
            model_explicit=model_name is not None,
            context_window_tokens=context_window,
            context_window_explicit=context_window is not None,
        )
        session = (
            CodingSession.from_snapshot(config, snapshot)
            if snapshot is not None
            else await CodingSession.new(
                config, session_id=manager.create(), title=title
            )
        )
        return session, provider
    except BaseException:
        await provider.aclose()
        raise


def _is_utf8_encoding(encoding: str | None) -> bool:
    """判断传入的编码是不是utf-8"""
    if encoding is None:
        return False

    normalized = encoding.lower().replace('-', '').replace('_', '')
    # 给传入的编码统一格式，将UTF_8 或 utf-8等转化为 utf8
    return normalized == 'utf8'

def _force_utf8_streams() -> None:
    """ 把不是utf8的改成utf-8"""
    for stream in (sys.stdout, sys.stderr):
        # 遍历 标准输出 和 标准错误
        encoding = getattr(stream, 'encoding', None)

        if _is_utf8_encoding(encoding):
            continue
        # 忽略 缺少 reconfigure 能力的流（如 pytest 捕获对象）与非法参数
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(ValueError):
                reconfigure(encoding="utf-8", errors="replace")
            # 强制指定为 UTF-8， 如果报错 采用replace 换一个

def _merge_stdin_prompt(prompt: str) -> str:
    """ 在有管道输入的情况下将他和prompt拼接 """
    stdin = sys.stdin
    # stdin 是 None
    if stdin is None:
        return prompt

    try:
        if stdin.isatty(): # is a teletypewriter 判断当前输入流是否连接到一个真实的终端（键盘）
            # 链接键盘，没有管道内容
            return prompt
    except (AttributeError, ValueError):
        return prompt

    try:
        piped_content = stdin.read()
    except (OSError, ValueError):
        return prompt

    if not piped_content:
        return prompt

    if not prompt:
        return piped_content

    return f"{piped_content}\n\n{prompt}"

app = typer.Typer(
    name = "minitau",
    help = "A minimal Python implementation of a Pi-style coding-agent harness.",
    add_completion = False, # 关闭 Shell 自动补全功能
    invoke_without_command = True, # 允许无子命令运行”
    context_settings={"allow_interspersed_args": True},
)

@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    prompt_args: Annotated[
        list[str] | None,
        typer.Argument(
            help="Prompt to send to the coding agent.",
        ),
    ] = None,
    print_mode: Annotated[
        bool,
        typer.Option(
            "--print",
            "-p",
            help="Run the prompt in non-interactive print mode.",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Model provider to use.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            "-m",
            help="Model name to request from the provider.",
        ),
    ] = None,
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            help="Working directory used by coding tools.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Show the minitau version and exit.",
        ),
    ] = False,
    resume: Annotated[
        str | None,
        typer.Option(
            "--resume",
            help="Resume a session by id or unique prefix.",
        ),
    ] = None,
    continue_latest: Annotated[
        bool,
        typer.Option(
            "--continue",
            "-c",
            help="Resume the most recently used session.",
        ),
    ] = False,
    context_window: Annotated[
        int | None,
        typer.Option("--context-window", help="Model context window size in tokens."),
    ] = None,
) -> None:
    """ """
    if version:
        typer.echo(f"Minitau version: {__version__}")
        raise typer.Exit() # 抛出异常 终止程序

    if ctx.invoked_subcommand is not None:
        # 只要用户调用了子命令
        return # 直接 return（结束回调函数），
    # 不做任何多余的操作，让程序流程顺利流转到对应的子命令函数（如 def create(): ...）中去执行

    working_directory = cwd or Path.cwd()
    positional_prompt = " ".join(prompt_args or ()).strip()

    if context_window is not None and context_window <= 0:
        raise typer.BadParameter("--context-window must be greater than 0")

    if resume is not None and continue_latest:
        raise typer.BadParameter("--resume and --continue cannot be combined.")

    if print_mode:
        # 管道输入只属于 print 模式。
        prompt = _merge_stdin_prompt(positional_prompt).strip()
        if not prompt:
            raise typer.BadParameter(
                'Print mode requires a prompt. Use `minitau -p "your prompt"` '
                "or pipe content through stdin."
            )

        run_print_mode(
            prompt=prompt,
            provider_name=provider,
            model_name=model,
            cwd=working_directory,
            resume=resume,
            continue_latest=continue_latest,
            context_window=context_window,
        )
        raise typer.Exit()

    # input() 需要真实终端。不能先调用 _merge_stdin_prompt()，
    # 否则它可能把交互输入当成管道内容一次性读完。
    if sys.stdin is None or not sys.stdin.isatty():
        raise typer.BadParameter("Interactive mode requires a TTY; use --print for piped input.")

    run_interactive_mode(
        initial_prompt=positional_prompt or None,
        provider_name=provider,
        model_name=model,
        cwd=working_directory,
        resume=resume,
        continue_latest=continue_latest,
        context_window=context_window,
    )
# import datatime uuid4
# def _sessions_directory(cwd: Path) -> Path:
#     """项目级会话目录：<cwd>/.minitau/sessions/。"""
#     return cwd / ".minitau/sessions"
#
# def _new_session_id() -> str:
#     """YYYYMMDD-HHMMSS-xxxxxx：文件名字典序 = 创建时间序。"""
#     stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
#     return f"{stamp}-{uuid4().hex[:6]}"
#
# def _resolve_session_file(sessions_dir: Path, ref: str) -> Path | None:
#     """把用户输入（id 或唯一前缀）解析成会话文件；歧义时报错并列出候选。"""
#     exact = sessions_dir / f"{ref}.jsonl"
#     if exact.exists():
#         return exact
#     matches = sorted(sessions_dir.glob(f"{ref}*.jsonl"))
#     # 找所有以 ref 开头、以 .jsonl 结尾的文件
#     if len(matches) == 1:
#         return matches[0]
#     if len(matches) > 1:
#         candidates = "\n ".join(path.stem for path in matches)
#         # .name	完整文件名	20260919-102943-c2985e.jsonl
#         # .stem	去扩展名	20260919-102943-c2985e
#         # .suffix	扩展名（含点）	.jsonl
#         raise typer.BadParameter(
#             f"Ambiguous session {ref!r}; candidates:\n {candidates}"
#         )
#     return None
#
# def _latest_session_file(sessions_dir: Path) -> Path | None:
#     """mtime 最新的会话文件；空目录返回 None"""
#     files = list(sessions_dir.glob("*.jsonl"))
#     if not files:
#         return None
#     return max(files, key=lambda path: path.stat().st_mtime)

def run_print_mode(
    *,
    prompt: str,
    provider_name: str | None,
    model_name: str | None,
    cwd: Path,
    resume: str | None,
    continue_latest: bool,
    context_window: int | None = None,
) -> None:
    if resume is not None and continue_latest:
        raise typer.BadParameter("--resume and --continue cannot be combined.")

    manager = SessionManager(cwd)

    async def run_and_close() -> int:
        session, provider = await _open_session(
            manager=manager,
            provider_name=provider_name,
            model_name=model_name,
            context_window=context_window,
            resume=resume,
            continue_latest=continue_latest,
            title=prompt[:40],
        )
        try:
            label = "Resumed session" if resume is not None or continue_latest else "Session"
            typer.echo(f"{label}: {session.session_id}", err=True)
            return await _run_print_session(
                session=session,
                prompt=prompt,
            )
        finally:
            await provider.aclose()

    try:
        exit_code = asyncio.run(run_and_close())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if exit_code:
        raise typer.Exit(code=exit_code)

async def _run_print_session(
    *,
    session: CodingSession,
    prompt: str,
) -> int:
    renderer = EventRenderer(
        emit_line=lambda text, is_error: typer.echo(text, err=is_error),
        emit_chunk=lambda text, is_error: typer.echo(
            text,
            err=is_error,
            nl=False,
        ),
        streaming=False,
    )

    events = session.prompt(prompt)
    async with closing_async_iterator(events):
        async for event in events:
            renderer.render(event)

    return renderer.finish()

def run_interactive_mode(
    *,
    initial_prompt: str | None,
    provider_name: str | None,
    model_name: str | None,
    cwd: Path,
    resume: str | None,
    continue_latest: bool,
    context_window: int | None,
) -> None:
    """装载会话后创建 Provider，运行 REPL，并在退出时释放 Provider。"""
    manager = SessionManager(cwd)

    async def run_and_close() -> None:
        session, provider = await _open_session(
            manager=manager,
            provider_name=provider_name,
            model_name=model_name,
            context_window=context_window,
            resume=resume,
            continue_latest=continue_latest,
            title=(initial_prompt or "")[:40],
        )
        try:
            typer.echo(f"Session: {session.session_id}", err=True)

            await run_repl(
                session,
                manager,
                read_line=lambda: input("minitau> "),
                emit=lambda text, is_error: typer.echo(text, err=is_error),
                write=lambda text, is_error: typer.echo(
                    text,
                    err=is_error,
                    nl=False,
                ),
                initial_prompt=initial_prompt,
            )
        finally:
            await provider.aclose()

    try:
        asyncio.run(run_and_close())
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


_force_utf8_streams()

if __name__ == "__main__":
    app()
