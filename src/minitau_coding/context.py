"""Discover project-specific instruction files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# from minitau_coding.system_prompt import ProjectContextFile

@dataclass(frozen=True, slots=True)
class ProjectContextFile:
    """A project instruction file loaded into the system prompt."""

    path: Path
    content: str

PROJECT_ROOT_MARKERS = (
    ".git",
    "pyproject.toml",
    "uv.lock",
    "setup.py",
    "package.json",
)

def find_project_root(cwd: Path) -> Path:
    """Find the nearest project root at or above cwd."""
    current = cwd.expanduser().resolve()
    # 是当前工作目录的绝对、规范化的 Path 对象

    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in PROJECT_ROOT_MARKERS):
            return candidate

    return current


def discover_project_context(
        cwd:Path,
) -> tuple[ProjectContextFile, ...]:
    """
    找到所有AGENTS.md
    从项目最外层开始，一路走到你当前所在的文件夹，
    把沿途和特定隐藏文件夹里所有叫 AGENTS.md 的文件都找出来，读一遍内容，去重后全部带回来
    """

    resolved_cwd = cwd.expanduser().resolve()
    project_root = find_project_root(resolved_cwd) # 找到根目录

    candidates: list[Path] = [] # 创建空的地址清单

    relative = resolved_cwd.relative_to(project_root)
    #  计算“当前文件夹”相对于“项目根目录”的偏移量
    current = project_root
    candidates.append(current / "AGENTS.md") # 将根目录下的AGENT.md计入候选者名单

    for part in relative.parts:
        current = current / part
        candidates.append(current / "AGENTS.md")  # 把沿途的文件夹加上去

        # cwd 下的 minitau 专用规则。
    candidates.append(resolved_cwd / ".minitau" / "AGENTS.md")
    candidates.append(resolved_cwd / ".agents" / "AGENTS.md")

    discovered: list[ProjectContextFile] = [] # # 用来装最终找到的有效文件
    seen: set[Path] = set() #  # 用来记录“哪些地址我已经看过了”（避免重复）

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()

        if resolved in seen or not resolved.is_file():
            continue

        seen.add(resolved)
        discovered.append(
            ProjectContextFile(
                path=resolved,
                content=resolved.read_text(encoding="utf-8"),
            )
        )

    return tuple(discovered)