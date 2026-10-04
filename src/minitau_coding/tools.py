"""Coding tools: read/write/edit/bash for local filesystem and shell."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import math
import os
import signal  # 杀进程用（注意：与本文件内 None 参数无冲突）
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from minitau_agent.provider import CancellationToken
from minitau_agent.tools import AgentTool, AgentToolResult
from minitau_agent.types import JSONValue


class ToolInputError(ValueError):
    """Raised when a tool receives invalid structured arguments."""

DEFAULT_MAX_OUTPUT_LINES = 2000
DEFAULT_MAX_OUTPUT_BYTES = 50 * 1024  # 50KB

# ─── 参数提取辅助函数 ────

def _str_arg(arguments: Mapping[str, JSONValue], name: str) -> str:
    """Extract a required string argument"""
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string, got {type(value).__name__}")
    return value

def _optional_int_arg(arguments: Mapping[str, JSONValue], name: str) -> int | None:
    """Extract an optional integer argument."""
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f"{name} must be a integer, got {type(value).__name__}")
    return value


def _optional_float_arg(arguments: Mapping[str, JSONValue], name: str) -> float | None:
    """Extract an optional float argument (e.g. bash timeout in seconds)."""
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ToolInputError(f"{name} must be a number")
    if isinstance(value, int):
        return float(value)
    if not isinstance(value, float):
        raise ToolInputError(f"{name} must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ToolInputError(f"{name} must be finite")
    return value

def _path_arg(arguments: Mapping[str, JSONValue], name: str, *, cwd: Path) -> Path:
    """Resolve a path argument relative to cwd, with boundary check."""
    raw = _str_arg(arguments, name)
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    resolved = path.resolve()
    if not resolved.is_relative_to(cwd.resolve()):
        raise ToolInputError(f"Path {raw!r} is outside the working directory {cwd}")
    return resolved


# ─── 截断函数 ─────
@dataclass(frozen=True, slots=True)
class TruncationResult:
    """截断结果：内容 + 元数据。continuation hint 全靠这些字段拼出来。"""

    content: str
    truncated: bool
    truncated_by: str | None      # "lines" / "bytes" / None（没截断）
    total_lines: int
    output_lines: int
    first_line_exceeds_limit: bool


def truncate_head(text: str, *, max_lines: int, max_bytes: int) -> TruncationResult:
    """从头部截断，保留前 N 行 / N 字节，带完整元数据。"""
    lines = text.split("\n")
    total_lines = len(lines)
    total_bytes = len(text.encode("utf-8"))

    # 没超任何限制：原样返回
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(text, False, None, total_lines, total_lines, False)

    # 首行自己就超字节上限（如 minified JS）：一行都放不下，显式标记
    # 而不是默默返回空串让模型不知所措
    if lines and len(lines[0].encode("utf-8")) > max_bytes:
        return TruncationResult("", True, "bytes", total_lines, 0, True)

    # 逐行装入，行数和字节双闸门
    result_lines: list[str] = []
    used_bytes = 0
    truncated_by = "lines"

    for index, line in enumerate(lines[:max_lines]):
        # 换行符只在非首行计数（首行前面没有换行）
        line_bytes = len(line.encode("utf-8")) + (1 if index > 0 else 0)
        if used_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        result_lines.append(line)
        used_bytes += line_bytes

    output = "\n".join(result_lines)
    return TruncationResult(output, True, truncated_by, total_lines, len(result_lines), False)

def _truncate_string_to_bytes_from_end(text: str, max_bytes: int) -> str:
    """单行超过字节预算时，从末尾裁出最后 max_bytes 个字节。
    可能切进多字节字符，用 errors='ignore' 丢残缺头。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")

def truncate_tail(text: str, *, max_lines: int, max_bytes: int) -> TruncationResult:
    """从尾部截断，保留最后 N 行/字节——报错和结果通常在输出末尾。"""
    lines = text.split("\n")
    total_lines = len(lines)
    total_bytes = len(text.encode("utf-8"))

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(text, False, None, total_lines, total_lines, False)

    result_lines: list[str] = []
    used_bytes = 0
    truncated_by = "lines"
    for line in reversed(lines):
        if len(result_lines) >= max_lines:
            truncated_by = "lines"
            break
        line_bytes = len(line.encode("utf-8")) + (1 if result_lines else 0)
        #  为什么要 encode("utf-8") 再 len
        # len(line) 数的是字符数，len(line.encode("utf-8")) 数的是字节数
        # 循环里 result_lines 非空，说明"更靠后的行已经放进去了"，
        # 那么当前行和它们之间存在一个换行符；result_lines 为空说明这是第一条（最靠后那条），
        # 它后面没有任何东西，自然没有换行符
        if used_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not result_lines:
                # 第一条（从尾数）就超预算：不是丢弃，从尾裁一段，标记不完整
                clipped = _truncate_string_to_bytes_from_end(line, max_bytes)
                result_lines.insert(0, clipped)
            break
        result_lines.insert(0, line)
        used_bytes += line_bytes

    output = "\n".join(result_lines)
    return TruncationResult(
        output, True, truncated_by, total_lines, len(result_lines), False
    )

# ─── read 工具 ───────
def create_read_tool(*, cwd: Path) -> AgentTool:
    """Create the read tool for reading files."""
    MAX_LINES = 2000
    MAX_BYTES = 50 * 1024  # 50KB

    async def execute(arguments: Mapping[str, JSONValue]) -> AgentToolResult:
        path = _path_arg(arguments, "path", cwd=cwd)
        offset = _optional_int_arg(arguments, "offset")
        limit = _optional_int_arg(arguments, "limit")

        if offset is not None and offset < 0:
            raise ToolInputError("offset must be at least 0")
        if limit is not None and limit < 1:
            raise ToolInputError("limit must be at least 1")

        if not path.exists():
            raise ToolInputError(f"File not found: {path}")
        if path.is_dir():
            raise ToolInputError(f"Path is a directory: {path}")

        # CRLF/CR 统一归一化为 LF：
        # 不做这步，Windows 文件每行尾挂 \r，edit 的 oldText 匹配会悄悄失败
        text = path.read_text(encoding="utf-8")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        all_lines = text.split("\n")
        total_lines = len(all_lines)

        # 1-indexed 行号语义转内部 0-indexed
        start_line = 0 if offset is None or offset == 0 else offset - 1
        if start_line >= total_lines:
            # 越尾必须显式报错：空结果对模型没有信息量，会引发盲目重试
            raise ToolInputError(
                f"Offset {offset} is beyond end of file ({total_lines} lines total)"
            )

            # 按用户 limit 切片
        user_limited_lines: int | None = None
        if limit is not None:
            end_line = min(start_line + limit, total_lines)
            selected = "\n".join(all_lines[start_line:end_line])
            user_limited_lines = end_line - start_line
        else:
            selected = "\n".join(all_lines[start_line:])

        # 截断（行数/字节双闸门）
        truncation = truncate_head(selected, max_lines=MAX_LINES, max_bytes=MAX_BYTES)
        start_display = start_line + 1  # 给模型看的行号是 1-indexed

        # 拼 continuation hint：截断不是问题，模型不知道"怎么继续"才是问题
        if truncation.first_line_exceeds_limit:
            first_line_size = len(all_lines[start_line].encode("utf-8"))
            output = (
                f"[Line {start_display} is {first_line_size} bytes, exceeds "
                f"{MAX_BYTES} bytes limit. Use bash to read a slice of this line.]"
            )
        elif truncation.truncated:
            end_display = start_display + truncation.output_lines - 1
            next_offset = end_display + 1
            output = truncation.content
            if truncation.truncated_by == "lines":
                output += (
                    f"\n\n[Showing lines {start_display}-{end_display} of {total_lines}. "
                    f"Use offset={next_offset} to continue.]"
                )
            else:
                output += (
                    f"\n\n[Showing lines {start_display}-{end_display} of {total_lines} "
                    f"({MAX_BYTES} bytes limit). Use offset={next_offset} to continue.]"
                )
        elif user_limited_lines is not None and start_line + user_limited_lines < total_lines:
            # 用户给了 limit、没触发截断、但文件没读完：也要给导航
            remaining = total_lines - (start_line + user_limited_lines)
            next_offset = start_line + user_limited_lines + 1
            output = (
                f"{truncation.content}\n\n[{remaining} more lines in file. "
                f"Use offset={next_offset} to continue.]"
            )
        else:
            output = truncation.content

        return AgentToolResult(
            content=output,
            details={
                "path": str(path),
                "total_lines": total_lines,
                "output_lines": truncation.output_lines,
                "truncated": truncation.truncated,
                "truncated_by": truncation.truncated_by,
            }
        )

    return AgentTool(
        name="read",
        description=(
            "Read the contents of a file. Output is truncated to "
            f"{MAX_LINES} lines or {MAX_BYTES // 1024}KB (whichever is hit first). "
            "Use offset/limit for large files; when you need the full file, "
            "continue with offset until complete."
        ),
        prompt_snippet="Read file contents",
        prompt_guidelines=("Use read to examine files instead of cat or sed.",),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (relative or absolute)",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (0 or 1 = start of file)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read",
                },
            },
            "required": ["path"],
        },
        execute_fn=execute,
    )


# ─── 文件锁 ─────
class _FileLock:
    """Per-path coroutine-level write lock.

    设计意图：防止同一进程内多个协程并发写同一个文件。
    - 实现：全局 dict[Path, asyncio.Lock]，按路径取锁
    - 与 tau-main 的 _file_lock 一致：只做协程级互斥，不做进程级锁
    - 局限：两个独立进程同时写同一文件时此锁无效（单进程 agent 场景下够用）

    注：tau 原版同名实现也只做协程锁；此处如实说明，不虚标 msvcrt/fcntl 进程锁。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> None:
        await self._lock.acquire()

    async def __aexit__(self, *arg: object) -> None:
        # 进程级锁在 release 前释放
        self._lock.release()


# 全局锁表：每个路径一个锁
_file_locks: dict[Path, _FileLock] = {}

def _get_file_lock(path: Path) -> _FileLock:
    resolved = path.resolve()
    lock = _file_locks.get(resolved)
    if lock is None:
        lock = _FileLock(resolved)
        _file_locks.setdefault(resolved, lock)
    return lock

# ─── 行尾检测 ─────

def detect_line_ending(text: str) -> str:
    """Detect the line ending style of the text.

    设计意图：保留原文件的行尾风格。
    - 如果文件用 CRLF（Windows），写入时也用 CRLF
    - 如果文件用 LF（Unix），写入时也用 LF
    - 新文件默认 LF

    实现细节：
    - 统计 \r\n 和 \n 的数量
    - 哪种多就用哪种
    - 都没有就用 \n
    """
    crlf_count = text.count("\r\n")
    lf_count = text.count("\n") - crlf_count
    if crlf_count > lf_count:
        return "\r\n"
    return "\n"


def normalize_to_lf(text: str) -> str:
    """CRLF/CR 全部归一为 LF——模型世界只有 LF。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    """写盘前把 LF 还原成文件原风格。与 detect_line_ending 配对成闭环。"""
    return text.replace("\n", "\r\n") if ending == "\r\n" else text



# ─── write 工具 ─────

def create_write_tool(*, cwd: Path) -> AgentTool:
    """Create the write tool for writing files."""
    MAX_BYTES = 10 * 1024 * 1024  # 10MB

    async def execute(arguments: Mapping[str, JSONValue]) -> AgentToolResult:
        path = _path_arg(arguments, "path", cwd=cwd)
        content = _str_arg(arguments, "content")

        # 1.校验大小
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MAX_BYTES:
            raise ToolInputError(
                f"Content too large: {content_bytes} bytes (max {MAX_BYTES})"
            )

        # 2. 检测行尾 + 建目录 + 写入，全部放进同一临界区
        #    （原先行尾检测在锁外：写入前另一协程若先改了文件，检测结果就过期了）
        lock = _get_file_lock(path)
        async with lock:
            line_ending = "\n"
            if path.exists():
                try:
                    # 用 read_bytes + decode：Windows 文本模式的 read_text 会把
                    # \r\n 翻成 \n，导致检测不出原有 CRLF 风格。二进制读保留原貌。
                    existing_text = path.read_bytes().decode("utf-8")
                    line_ending = detect_line_ending(existing_text)
                except (OSError, UnicodeDecodeError):
                    pass  # 读不了（二进制/无权限）就用默认 LF

            #  标准化行尾（统一转成目标风格）
            normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
            if line_ending == "\r\n":
                normalized_content = normalized_content.replace("\n", "\r\n")

            # 建目录挪进锁内，与 tau 对齐：目录和文件是同一个临界区
            # 用 write_bytes 精确落盘：文本模式的 write_text 在 Windows 会把 \n
            # 翻成 \r\n，导致计算出的 line_ending 被 OS 翻译二次改写（"默认 LF"
            # 反而落盘成 CRLF）。二进制写入让 restore 算出的行尾 100% 落盘。
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(normalized_content.encode("utf-8"))

        # 3. 返回：字段名与值必须说同一种话
        lines = normalized_content.split(line_ending)
        return AgentToolResult(
            content=f"Successfully wrote {len(normalized_content)} characters to {path}",
            details={
                "path": str(path),
                "characters": len(normalized_content),
                "lines": len(lines),
                "line_ending": line_ending,
            },
        )

    return AgentTool(
        name="write",
        prompt_snippet="Create or overwrite files",
        prompt_guidelines=("Use write only for new files or complete rewrites.",),
        description="Write content to a file. Creates parent directories if needed.",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (relative or absolute)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
        execute_fn=execute,
    )


# ─── edit 工具 ─────

UTF8_BOM = "\ufeff"  # Windows 记事本遗产：剥掉匹配、写回复原
# 读进来的文本开头有可能有 1 个不可见字符 U+FEFF
# 如果有，先切下来存着，在干净文本上做匹配和替换，写文件时再原样拼回开头

def _count_occurrences(content: str, text: str) -> int:
    """
    数 text 在 content 里出现了几次，非重叠计数，返回次数
    等价于 content.count(text)，tau 手写循环语义相同。
    """
    if not text:
        # 空串在 find 里"处处命中"且 start 永不前进——必死循环。
        # 空旧文本在 apply_edits 的第一道闸门已被拒，这里是纵深防御：
        # 合约违例属于程序错误，抛 ValueError 而不是返回误导性的 0。
        raise ValueError("text must not be empty")
    count = 0
    start = 0
    while True:
        index = content.find(text, start)
        if index == -1:
            return count
        count += 1
        start = index + len(text)


def _validate_non_overlapping(spans: list[tuple[int, int, str]]) -> None:
    """
    span 按起点排序后，相邻两个只要有交叠就报错。
    校验多个编辑区间互不重叠。有重叠就抛 ToolInputError，不重叠则静默返回 None
    """
    previous_end = -1
    for start, end, _new_text in sorted(spans):
        if start < previous_end:
            raise ToolInputError("Edits must not overlap")
        previous_end = end


def _strip_bom(content: str) -> tuple[str, str]:
    """返回 (bom, 去掉 BOM 后的内容)。BOM 必须剥掉否则文件头的 oldText 匹配不上。"""
    return (UTF8_BOM, content[1:]) if content.startswith(UTF8_BOM) else ("", content)


def _prepare_edit_arguments(arguments: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """容错层：把模型的两种'方言'归一成规范的 edits 列表。

    ① edits 传成 JSON 字符串 → 解开
    ② 顶层 oldText/newText（单编辑便捷写法）→ 收编进列表
    """
    prepared = dict(arguments)
    # 复制成可变 dict
    edits_value = prepared.get("edits")
    # 取出来，可能是 list / str / None / 不存在
    if isinstance(edits_value, str):
        try:
            parsed = json.loads(edits_value) # 文本 → Python 对象
        except json.JSONDecodeError:
            parsed = None # 解析失败，不抛错，标记成 None
        if isinstance(parsed, list):
            prepared["edits"] = parsed

    old_text = prepared.get("oldText")
    new_text = prepared.get("newText")
    if isinstance(old_text, str) and isinstance(new_text, str):
        # 一次编辑要同时知道"改什么"和"改成什么"
        edits = prepared.get("edits")
        edit_list = edits if isinstance(edits, list) else []
        prepared["edits"] = [*edit_list, {"oldText": old_text, "newText": new_text}]
        prepared.pop("oldText", None)
        prepared.pop("newText", None)
        # 游（apply_edits_to_normalized_content）只认 edits 列表。
        # 留着顶层 oldText/newText 会造成两处表达同一件事，语义重复且容易被重复处理。
    return prepared


def _edits_arg(arguments: Mapping[str, JSONValue]) -> list[dict[str, str]]:
    """严格层：归一之后只认一种形状，不合格直接拒。"""
    value = arguments.get("edits")
    if not isinstance(value, list) or not value:
        raise ToolInputError("edits must contain at least one replacement.")
    edits: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ToolInputError(f"edits[{index}] must be an object")
        old_text = item.get("oldText")
        new_text = item.get("newText")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ToolInputError(f"edits[{index}].oldText and .newText must be strings")
        edits.append({"oldText": old_text, "newText": new_text})
    return edits


def _not_found_error(path: str, edit_index: int, total_edits: int) -> str:
    """
    错误信息必须'可执行'：提示最常见的失败原因。
    你给的 oldText 在文件里找不到
    """
    if total_edits == 1:
        return (
            f"Could not find the exact text in {path}. The old text must match exactly "
            "including all whitespace and newlines."
        )
    return (
        f"Could not find edits[{edit_index}] in {path}. The oldText must match exactly "
        "including all whitespace and newlines."
    )


def _duplicate_error(path: str, edit_index: int, total_edits: int, occurrences: int) -> str:
    """找到了 N 处，无法确定改哪一处"""
    if total_edits == 1:
        return (
            f"Found {occurrences} occurrences of the text in {path}. The text must be unique. "
            "Please provide more context to make it unique."
        )
    return (
        f"Found {occurrences} occurrences of edits[{edit_index}] in {path}. "
        "Each oldText must be unique. Please provide more context to make it unique."
    )


def _empty_old_text_error(path: str, edit_index: int, total_edits: int) -> str:
    # 空 oldText 必须拒收
    if total_edits == 1:
        return f"oldText must not be empty in {path}."
    return f"edits[{edit_index}].oldText must not be empty in {path}."


def _no_change_error(path: str, total_edits: int) -> str:
    # 替换全部做完后，比较结果和原文。一样就报错
    if total_edits == 1:
        return (
            f"No changes made to {path}. The replacement produced identical content. "
            "This might indicate an issue with special characters or the text not existing "
            "as expected."
        )
    return f"No changes made to {path}. The replacements produced identical content."


def apply_edits_to_normalized_content(
    normalized_content: str,
    edits: list[dict[str, str]],
    path: str,
) -> tuple[str, str]:
    """核心算法（纯函数）：全验 → 倒序换 → no-change 报错。

    原子性的来源：校验阶段零写入，任何失败在写入前就抛出；
    替换阶段只是字符串拼接，不可能失败。
    """
    # edits 也归一化：模型给的 oldText 混了 CRLF 也能匹配
    normalized_edits = [
        {"oldText": normalize_to_lf(edit["oldText"]), "newText": normalize_to_lf(edit["newText"])}
        for edit in edits
    ]

    # 第一道：空 oldText 拒收
    for index, edit in enumerate(normalized_edits):
        if not edit["oldText"]:
            raise ToolInputError(_empty_old_text_error(path, index, len(normalized_edits)))

    # 第二道：每个 oldText 恰好命中一次（唯一性是防模型手滑，不是限制）
    matches: list[tuple[int, int, str]] = []
    for index, edit in enumerate(normalized_edits):
        occurrences = _count_occurrences(normalized_content, edit["oldText"])
        if occurrences == 0:
            raise ToolInputError(_not_found_error(path, index, len(normalized_edits)))
        if occurrences > 1:
            raise ToolInputError(
                _duplicate_error(path, index, len(normalized_edits), occurrences)
            )
        start = normalized_content.index(edit["oldText"])
        matches.append((start, start + len(edit["oldText"]), edit["newText"]))

    # 第三道：span 交叠拒收（两个 edit 都声称拥有同一片原文，意图不可判定）
    _validate_non_overlapping(matches)

    # 倒序替换：所有 span 坐标基于原文，从后往前换不影响未处理的坐标
    new_content = normalized_content
    for start, end, new_text in sorted(matches, reverse=True):
        new_content = f"{new_content[:start]}{new_text}{new_content[end:]}"

    # 第四道：no-change 也是错——假成功比失败更糟
    if new_content == normalized_content:
        raise ToolInputError(_no_change_error(path, len(normalized_edits)))
    return normalized_content, new_content


def generate_diff_string(old: str, new: str) -> tuple[str, int | None]:
    """ndiff 风格 diff + 首个变更行号（给 UI 跳转用）。"""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    # splitlines() 按行边界切且不保留换行符
    diff = "\n".join(difflib.ndiff(old_lines, new_lines))
    first_changed_line: int | None = None
    new_line_number = 0
    for line in difflib.ndiff(old_lines, new_lines):
        if line.startswith("  "):
            new_line_number += 1
        elif line.startswith("+"):
            new_line_number += 1
            if first_changed_line is None:
                first_changed_line = new_line_number
        elif line.startswith("-") and first_changed_line is None:
            first_changed_line = max(new_line_number + 1, 1)
    return diff, first_changed_line


def generate_unified_patch(path: str, old: str, new: str) -> str:
    """标准 unified diff，可直接喂给 git apply。"""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )

def create_edit_tool(*, cwd: Path) -> AgentTool:
    """Create the edit tool for exact, validated text replacement in one file."""
    async def execute(arguments: Mapping[str, JSONValue]) -> AgentToolResult:
        # ① 容错归一：把模型的各种写法统一成 {path, edits: [{oldText, newText}]}
        prepared = _prepare_edit_arguments(arguments)
        # ② 路径解析 + 越界拦截：相对路径锚定 cwd，resolve() 后必须仍在 cwd 内
        # dict[str, JSONValue] 天然兼容 Mapping[str, JSONValue]，类型检查器自行推导，无需忽略
        path = _path_arg(prepared, "path", cwd=cwd)
        # ③ 严格校验：edits 必须是非空数组，且每项都是带字符串 oldText/newText 的对象
        edits = _edits_arg(prepared)

        # ④ 存在性检查放在加锁前：能早失败就别占着锁
        if not path.exists():
            raise ToolInputError(f"Could not edit file: {path}. File not found.")
        if path.is_dir():
            raise ToolInputError(f"Could not edit file: {path}. Path is a directory.")

        lock = _get_file_lock(path)
        # 异步上下文管理器：进入时 __aenter__ 取锁，退出时 __aexit__ 释放（异常也会释放）
        async with lock:
            # 读 → 剥 BOM → 记行尾风格 → 统一成 LF
            # 顺序不能变：所有匹配都在 LF 归一化后的文本上做。
            # 不归一化，Windows 文件每行尾挂 \r，oldText 会静默匹配失败
            raw_content = path.read_bytes().decode("utf-8")
            bom, content = _strip_bom(raw_content)
            original_ending = detect_line_ending(content)
            normalized = normalize_to_lf(content)

            # 核心：四道校验 + 倒序替换，返回 (改前文本, 改后文本)
            base_content, new_content = apply_edits_to_normalized_content(
                normalized, edits, str(path)
            )
            # 写回：BOM 原样贴回 + 行尾还原成原文件风格（CRLF 不能被改成 LF）
            final_content = bom + restore_line_endings(new_content, original_ending)
            path.write_bytes(final_content.encode("utf-8"))

        # diff 在锁外算：只依赖内存里的字符串，不占临界区
        diff_text, first_changed_line = generate_diff_string(base_content, new_content)
        patch = generate_unified_patch(str(path), base_content, new_content)
        return AgentToolResult(
            # content 进模型上下文（要计费），所以只报一句结果；细节全放 details
            content=f"Successfully replaced {len(edits)} block(s) in {path}.",
            details={
                "path": str(path),
                "edits": len(edits),
                "diff": diff_text,                            # ndiff 风格，给人看
                "patch": patch,                               # unified diff，可直接 git apply
                "first_changed_line": first_changed_line,     # UI 跳转到首个变更行
            },
        )

    # 下面是"工具说明书"，由 provider 转成厂商格式交给模型；不是给人看的注释
    return AgentTool(
        name="edit",
        prompt_snippet=(
            "Make precise file edits with exact text replacement, including multiple "
            "disjoint edits in one call"
        ),
        prompt_guidelines=(
            "Use edit for precise changes (edits[].oldText must match exactly)",
            "When changing multiple separate locations in one file, use one edit call "
            "with multiple entries in edits[] instead of multiple edit calls",
            "Each edits[].oldText is matched against the original file, not after "
            "earlier edits are applied. Do not emit overlapping or nested edits. "
            "Merge nearby changes into one edit.",
            "Keep edits[].oldText as small as possible while still being unique in "
            "the file. Do not include large unchanged regions just to connect "
            "distant changes.",
        ),
        # description 是写给模型的行为规范
        description=(
            "Edit a single file using exact text replacement. Every edits[].oldText must match "
            "a unique, non-overlapping region of the original file. If two changes affect "
            "nearby lines, merge them into one edit. Each oldText is matched against the "
            "original file, not after earlier edits are applied."
        ),
        # parameters 是 JSON Schema：声明参数形状，模型照着填
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit"},
                "edits": {
                    "type": "array",
                    "description": "One or more targeted replacements.",
                    "items": {                                # 数组每个元素的 schema
                        "type": "object",
                        "properties": {
                            "oldText": {"type": "string"},
                            "newText": {"type": "string"},
                        },
                        "required": ["oldText", "newText"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
        execute_fn=execute,    # 真正的执行体，签名必须匹配 ToolExecutor
    )

# ─── bash 工具 ─────

def format_size(bytes_count: int) -> str:
    if bytes_count < 1024:
        return f"{bytes_count}B"
    if bytes_count < 1024 ** 2:
        return f"{bytes_count / 1024:.1f} KB"
    return f"{bytes_count / 1024 ** 2:.1f} MB"

def append_status_block(text: str, status: str) -> str:
    """状态块跟在输出后，空输出时独占。"""
    return f"{text}\n\n{status}" if text else status

async def _wait_for_cancel(token: CancellationToken) -> None:
    """50ms 轮询取消令牌。轮询够用，且不依赖 token 的具体实现类。"""
    while not token.is_cancelled():
        await asyncio.sleep(0.05)


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """
    终止一个子进程及其衍生的后代进程
    POSIX：杀整个进程组（需要 start_new_session=True 配合）。
    Windows：通过 taskkill /T /F 清理 cmd 及其子进程。"""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)  # type: ignore[attr-defined]  # POSIX-only 常量，运行时由 os.name 分支保护
        except ProcessLookupError:
            return
    else:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            )
        try:
            if process.returncode is None:
                process.kill()
        except ProcessLookupError:
            return

def _write_temp_output(output: str) -> str:
    """
    把一段文本写进系统临时目录的新文件，返回该文件的完整路径
    截断掉的全文落临时文件——截断是降级不是销毁。
    """
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="minitau-bash-", suffix=".log", delete=False,
    ) as handle:
        handle.write(output)
        return handle.name
    # with ... as handle 保证文件一定被关闭
    # NamedTemporaryFile 默认 delete=True——close() 时文件立刻被删。那样 with 一退出文件就没了
    # 这里要让调用方之后还能读到完整输出，所以必须 delete=False
    # handle.name 是系统在临时目录下生成的完整路径

async def _communicate_with_cancellation(
    process: asyncio.subprocess.Process,
    *,
    timeout: float | None,
    token: CancellationToken | None,
) -> tuple[bytes, bool, bool]:
    """一个 asyncio.wait 统一三种结局：完成 / 超时 / 取消。

        返回 (输出字节, 是否超时, 是否取消)。
        """
    communicate = asyncio.create_task(process.communicate())
    cancel_watch: asyncio.Task[None] | None = None
    try:
        wait_for: set[asyncio.Task[object]] = {communicate}
        if token is not None:
            watch = asyncio.create_task(_wait_for_cancel(token))  # 局部：类型是干净的 Task[None]
            cancel_watch = watch  # 给 finally 用（保持 Optional）
            wait_for.add(watch)  # 给 wait 用（非 Optional）

        done, _pending = await asyncio.wait(
            wait_for,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if communicate in done:
            output_bytes, _stderr = communicate.result()
            return output_bytes, False, False

        cancelled = cancel_watch is not None and cancel_watch in done
        _kill_process_tree(process)          # 先杀
        try:
            output_bytes, _stderr = await asyncio.wait_for(communicate, timeout=5)
        except TimeoutError as exc:
            communicate.cancel()
            raise RuntimeError("Command process tree did not stop after termination") from exc
        except asyncio.CancelledError:
            output_bytes = b""
        return output_bytes, not cancelled, cancelled
    except asyncio.CancelledError:
        # 工具自身被外层取消：杀进程后继续向上传播，不吞
        _kill_process_tree(process)
        if not communicate.done():
            communicate.cancel()
        await asyncio.gather(communicate, return_exceptions=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise
    finally:
        if cancel_watch is not None:
            cancel_watch.cancel()
            await asyncio.gather(cancel_watch, return_exceptions=True)


def create_bash_tool(*, cwd: Path) -> AgentTool:
    """Create the bash tool for executing shell commands with captured output."""
    async def execute(
        arguments: Mapping[str, JSONValue], token: CancellationToken | None = None
    ) -> AgentToolResult:
        command = _str_arg(arguments, "command")
        timeout = _optional_float_arg(arguments, "timeout")
        if timeout is None:
            timeout = 120.0
        if timeout is not None and timeout <= 0:
            raise ToolInputError("timeout must be greater than 0")

        start = monotonic()
        if os.name == "posix":
            # start_new_session：脱离父进程组，超时时 killpg 才能团灭管道子进程
            process = await asyncio.create_subprocess_shell(
                command, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            # Windows：start_new_session 是 POSIX-only 参数，传了会 ValueError
            process = await asyncio.create_subprocess_shell(
                command, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        output_bytes, timed_out, cancelled = await _communicate_with_cancellation(
            process, timeout=timeout, token=token
        )

        output = output_bytes.decode(errors="replace")
        truncation = truncate_tail(
            output, max_lines=DEFAULT_MAX_OUTPUT_LINES, max_bytes=DEFAULT_MAX_OUTPUT_BYTES
        )
        full_output_path: str | None = None
        output_text = truncation.content or "(no output)"
        if truncation.truncated:
            full_output_path = _write_temp_output(output)
            start_line = truncation.total_lines - truncation.output_lines + 1
            end_line = truncation.total_lines
            output_text += (
                f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines}. "
                f"Full output: {full_output_path}]"
            )

        exit_code = process.returncode
        status: str | None = None
        if timed_out:
            status = (
                f"Command timed out after {timeout:g} seconds"
                if timeout
                else "Command timed out"
            )
        elif cancelled:
            status = "Command cancelled"
        elif exit_code not in (0, None):
            status = f"Command exited with code {exit_code}"
        if status:
            output_text = append_status_block(output_text, status)

        return AgentToolResult(
            content=output_text,
            details={
                "command": command,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "cancelled": cancelled,
                "duration_seconds": round(monotonic() - start, 3),
                "truncated": truncation.truncated,
                "full_output_path": full_output_path,
            },
        )

    return AgentTool(
        name="bash",
        prompt_snippet="Execute bash commands (ls, grep, find, etc.)",
        prompt_guidelines=(),  # 纪律已写入 description，prompt 层不重复
        description=(
            "Execute a shell command in the current working directory. Returns combined "
            "stdout and stderr. Output is truncated to the last "
            f"{DEFAULT_MAX_OUTPUT_LINES} lines or {DEFAULT_MAX_OUTPUT_BYTES // 1024}KB; "
            "if truncated, full output is saved to a temp file. Optionally provide a "
            "timeout in seconds (defaults to 120)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (defaults to 120)",
                },
            },
            "required": ["command"],
        },
        execute_fn=execute,
        execute_with_signal=execute,
    )


def create_coding_tools(*, cwd: str | Path | None = None) -> list[AgentTool]:
    """会话级工具集：cwd 只在工厂时刻解析一次。

    - 工具不是全局单例，每次会话/new 一组——路径边界跟着会话走
    - cli 是唯一调用方；测试想注入假工具时直接构造 AgentTool 列表即可
    """
    root = Path.cwd() if cwd is None else Path(cwd)
    return [
        create_read_tool(cwd=root),
        create_write_tool(cwd=root),
        create_edit_tool(cwd=root),
        create_bash_tool(cwd=root),
    ]
