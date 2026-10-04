"""Coding tools 测试：read / write / edit / bash，对齐 tau-main 行为。

每个用例只测一个行为，用例名即需求陈述（沿用 tau 测试风格）。
直接用 await tool.execute({...}) 调工具本体，不跑整个 agent loop。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from minitau_coding.tools import (
    ToolInputError,
    _count_occurrences,
    create_bash_tool,
    create_edit_tool,
    create_read_tool,
    create_write_tool,
    truncate_head,
)

# ─── read ────────────────────────────────────────────────────────────────

async def test_read_with_offset_and_limit(tmp_path: Path) -> None:
    """基础分页：offset=10 limit=5 读到第 10-14 行，未读完给导航提示。"""
    file = tmp_path / "sample.py"
    file.write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute({"path": "sample.py", "offset": 10, "limit": 5})

    assert result.content.split("\n")[0] == "line 10"
    assert "line 14" in result.content
    assert result.details["total_lines"] == 100
    assert "more lines" in result.content  # 未截断但没读完也要提示
    assert "offset=15" in result.content  # 给出下一步的确切参数


async def test_zero_offset_reads_from_start(tmp_path: Path) -> None:
    """模型常猜 offset 从 0 开始——0 必须合法且等价于从头读。"""
    file = tmp_path / "a.txt"
    file.write_text("first\nsecond\n", encoding="utf-8")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute({"path": "a.txt", "offset": 0})

    assert result.content.startswith("first\nsecond")


async def test_truncation_hint_tells_model_how_to_continue(tmp_path: Path) -> None:
    """超行数截断：必须带 'Use offset=N to continue' 导航。"""
    file = tmp_path / "big.txt"
    file.write_text("\n".join(f"line {i}" for i in range(5000)), encoding="utf-8")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute({"path": "big.txt"})

    assert result.details["truncated"] is True
    assert result.details["truncated_by"] == "lines"
    assert "Use offset=" in result.content


async def test_crlf_lines_are_normalized(tmp_path: Path) -> None:
    """Windows CRLF 文件：返回内容不能带 \\r，否则 edit 匹配会悄悄失败。"""
    file = tmp_path / "win.txt"
    file.write_bytes(b"alpha\r\nbeta\r\n")
    tool = create_read_tool(cwd=tmp_path)

    result = await tool.execute({"path": "win.txt"})

    assert "\r" not in result.content
    assert result.content == "alpha\nbeta\n"


async def test_offset_beyond_end_raises_with_line_count(tmp_path: Path) -> None:
    """越尾显式报错，且错误信息带总行数——给模型一次纠偏的机会。"""
    file = tmp_path / "tiny.txt"
    file.write_text("one\ntwo", encoding="utf-8")
    tool = create_read_tool(cwd=tmp_path)

    with pytest.raises(ToolInputError, match="2 lines total"):
        await tool.execute({"path": "tiny.txt", "offset": 99})


def test_truncate_head_flags_oversized_first_line() -> None:
    """首行自己超字节上限：输出空但 first_line_exceeds_limit 必须为 True。"""
    result = truncate_head("x" * 100_000 + "\n" + "tail", max_lines=2000, max_bytes=1024)

    assert result.content == ""
    assert result.first_line_exceeds_limit is True
    assert result.truncated_by == "bytes"


# ─── write ───────────────────────────────────────────────────────────────

async def test_write_tool_creates_parent_directories(tmp_path: Path) -> None:
    """写进不存在的多级目录：自动建齐。"""
    tool = create_write_tool(cwd=tmp_path)

    result = await tool.execute({"path": "deep/nested/dir/file.txt", "content": "hi"})

    assert (tmp_path / "deep" / "nested" / "dir" / "file.txt").read_text(encoding="utf-8") == "hi"
    assert "deep" in result.details["path"]


async def test_write_tool_overwrites_existing_file(tmp_path: Path) -> None:
    """write 的定位是完整重写：已有文件直接覆盖。"""
    (tmp_path / "a.txt").write_text("old content", encoding="utf-8")
    tool = create_write_tool(cwd=tmp_path)

    await tool.execute({"path": "a.txt", "content": "new"})

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "new"


async def test_write_tool_preserves_crlf_style_of_existing_file(tmp_path: Path) -> None:
    """偏离锁定：已存在的 CRLF 文件整写后仍是 CRLF（tau 不这么做，我们有意保留）。"""
    (tmp_path / "win.txt").write_bytes(b"old\r\nlines\r\n")
    tool = create_write_tool(cwd=tmp_path)

    await tool.execute({"path": "win.txt", "content": "new\ncontent\n"})

    assert (tmp_path / "win.txt").read_bytes() == b"new\r\ncontent\r\n"


async def test_write_tool_rejects_oversized_content(tmp_path: Path) -> None:
    """10MB 上限：超限 ToolInputError，且不落盘。"""
    tool = create_write_tool(cwd=tmp_path)

    with pytest.raises(ToolInputError, match="too large"):
        await tool.execute({"path": "big.bin", "content": "x" * (10 * 1024 * 1024 + 1)})

    assert not (tmp_path / "big.bin").exists()


async def test_write_tool_new_file_defaults_to_lf(tmp_path: Path) -> None:
    """新文件没有'原风格'可循：默认 LF。"""
    tool = create_write_tool(cwd=tmp_path)

    await tool.execute({"path": "fresh.txt", "content": "a\nb\n"})

    assert (tmp_path / "fresh.txt").read_bytes() == b"a\nb\n"


# ─── edit ────────────────────────────────────────────────────────────────

async def test_edit_applies_multiple_exact_replacements(tmp_path: Path) -> None:
    """一次调用多个不重叠替换，全部生效。"""
    (tmp_path / "code.py").write_text(
        "def a():\n    pass\n\ndef b():\n    pass\n", encoding="utf-8"
    )
    tool = create_edit_tool(cwd=tmp_path)

    result = await tool.execute({
        "path": "code.py",
        "edits": [
            {"oldText": "def a():", "newText": "def alpha():"},
            {"oldText": "def b():", "newText": "def beta():"},
        ],
    })

    text = (tmp_path / "code.py").read_text(encoding="utf-8")
    assert "def alpha():" in text and "def beta():" in text
    assert result.details["edits"] == 2


async def test_edit_rolls_back_when_any_edit_fails(tmp_path: Path) -> None:
    """原子性：第二个替换失败时，文件必须保持原样——一个字节都不变。"""
    original = "keep_a\nkeep_b\n"
    (tmp_path / "f.txt").write_text(original, encoding="utf-8")
    tool = create_edit_tool(cwd=tmp_path)

    with pytest.raises(ToolInputError, match="Could not find"):
        await tool.execute({
            "path": "f.txt",
            "edits": [
                {"oldText": "keep_a", "newText": "changed"},
                {"oldText": "not exist", "newText": "x"},
            ],
        })

    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == original


async def test_edit_requires_unique_matches(tmp_path: Path) -> None:
    """两处命中：报错且提示'提供更多上下文'。"""
    (tmp_path / "f.txt").write_text("dup\ndup\n", encoding="utf-8")
    tool = create_edit_tool(cwd=tmp_path)

    with pytest.raises(ToolInputError, match="2 occurrences"):
        await tool.execute({"path": "f.txt", "edits": [{"oldText": "dup", "newText": "x"}]})


async def test_edit_overlapping_spans_rejected(tmp_path: Path) -> None:
    """交叠 span：两个 edit 抢同一片原文，直接拒。"""
    (tmp_path / "f.txt").write_text("abcdefgh", encoding="utf-8")
    tool = create_edit_tool(cwd=tmp_path)

    with pytest.raises(ToolInputError, match="overlap"):
        await tool.execute({
            "path": "f.txt",
            "edits": [
                {"oldText": "abcd", "newText": "X"},
                {"oldText": "cdef", "newText": "Y"},
            ],
        })


async def test_edit_normalizes_crlf_in_old_text(tmp_path: Path) -> None:
    """模型给 LF 的 oldText 也能命中 CRLF 文件；写回保持 CRLF。"""
    (tmp_path / "win.txt").write_bytes(b"alpha\r\nbeta\r\n")
    tool = create_edit_tool(cwd=tmp_path)

    await tool.execute({
        "path": "win.txt",
        "edits": [{"oldText": "alpha\nbeta", "newText": "ALPHA\nBETA"}],
    })

    assert (tmp_path / "win.txt").read_bytes() == b"ALPHA\r\nBETA\r\n"


async def test_edit_no_change_is_an_error(tmp_path: Path) -> None:
    """oldText == newText：假成功比失败更糟，必须报错。"""
    (tmp_path / "f.txt").write_text("same\n", encoding="utf-8")
    tool = create_edit_tool(cwd=tmp_path)

    with pytest.raises(ToolInputError, match="No changes"):
        await tool.execute({"path": "f.txt", "edits": [{"oldText": "same", "newText": "same"}]})


async def test_edit_accepts_json_string_edits(tmp_path: Path) -> None:
    """容错层：模型把 edits 序列化成 JSON 字符串也能正常工作。"""
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    tool = create_edit_tool(cwd=tmp_path)

    result = await tool.execute({
        "path": "f.txt",
        "edits": '[{"oldText": "hello", "newText": "hi"}]',
    })

    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "hi\n"
    assert result.details["edits"] == 1


def test_count_occurrences_rejects_empty_text() -> None:
    """空串会让 find 永远命中且 start 不前进——必须提前拒绝而不是死循环。"""
    with pytest.raises(ValueError, match="must not be empty"):
        _count_occurrences("abc", "")


# ─── bash ────────────────────────────────────────────────────────────────

async def test_bash_captures_stdout_and_exit_code(tmp_path: Path) -> None:
    """stdout 捕获 + 退出码入 details；退出码 0 不加 status 块。"""
    tool = create_bash_tool(cwd=tmp_path)
    quoted = str(sys.executable).replace("\\", "/")

    result = await tool.execute({"command": f'"{quoted}" -c "print(12345)"'})

    assert "12345" in result.content
    assert result.details["exit_code"] == 0
    assert result.details["timed_out"] is False
    assert "exited with code" not in result.content


async def test_bash_merges_stderr_into_output(tmp_path: Path) -> None:
    """stderr 合并进 stdout——按真实时间线交错。"""
    tool = create_bash_tool(cwd=tmp_path)
    quoted = str(sys.executable).replace("\\", "/")

    result = await tool.execute({
        "command": f'"{quoted}" -c "import sys; print(\'oops\', file=sys.stderr)"'
    })

    assert "oops" in result.content
    assert result.details["exit_code"] == 0


async def test_bash_nonzero_exit_appends_status_block(tmp_path: Path) -> None:
    """非零退出不报错：状态块追加在输出后，退出码既是数据。"""
    tool = create_bash_tool(cwd=tmp_path)
    quoted = str(sys.executable).replace("\\", "/")

    result = await tool.execute({"command": f'"{quoted}" -c "raise SystemExit(3)"'})

    assert "Command exited with code 3" in result.content
    assert result.details["exit_code"] == 3


async def test_bash_timeout_kills_process(tmp_path: Path) -> None:
    """超时：状态块标注、进程被杀、details.timed_out=True。"""
    tool = create_bash_tool(cwd=tmp_path)
    quoted = str(sys.executable).replace("\\", "/")

    result = await tool.execute({
        "command": f'"{quoted}" -c "import time; time.sleep(10)"',
        "timeout": 0.5,
    })

    assert result.details["timed_out"] is True
    assert "timed out" in result.content


async def test_bash_truncation_keeps_tail_and_saves_full_output(tmp_path: Path) -> None:
    """大输出：保留末尾（tail），全文落临时文件且路径报给模型。"""
    tool = create_bash_tool(cwd=tmp_path)
    quoted = str(sys.executable).replace("\\", "/")
    # 生成 3000 行，末尾有标记行——tail 截断必须保住它
    py_code = "print('\\n'.join(str(i) for i in range(3000))); print('THE END')"
    cmd = f'"{quoted}" -c "{py_code}"'

    result = await tool.execute({"command": cmd})

    assert result.details["truncated"] is True
    assert "THE END" in result.content
    full_path = result.details["full_output_path"]
    assert full_path is not None
    assert Path(full_path).read_text(encoding="utf-8").count("\n") >= 3000
