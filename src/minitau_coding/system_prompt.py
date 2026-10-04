
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from minitau_agent.tools import AgentTool
from minitau_coding.context import ProjectContextFile

DEFAULT_IDENTITY = (
    "You are minitau, an expert coding assistant operating inside "
    "the user's project."
)

DEFAULT_GUIDELINES = (
    "Inspect relevant files before making changes.",
    "Prefer small and focused changes.",
    "Do not claim that commands succeeded unless their results were observed.",
    "Keep explanations concise and reference file paths clearly.",
)

@dataclass(frozen=True, slots=True)
class BuildSystemPromptOptions:
    cwd: Path
    tools: tuple[AgentTool, ...] = ()
    context_files: tuple[ProjectContextFile, ...] = ()
    custom_prompt: str | None = None
    append_system_prompt: str | None = None

def format_available_tools(
        tools: tuple[AgentTool, ...],
) -> str:
    lines = [f"- {tool.name}: {tool.prompt_snippet}" for tool in tools if tool.prompt_snippet]

    return "\n".join(lines) if lines else "(none)"

def collect_guidelines(
    tools: tuple[AgentTool, ...],
) -> tuple[str, ...]:
    """Combine and deduplicate tool and default guidelines."""
    candidates: list[str] = []

    for tool in tools:
        candidates.extend(tool.prompt_guidelines)

    candidates.extend(DEFAULT_GUIDELINES)

    result: list[str] = []
    seen: set[str] = set()

    for guideline in candidates:
        normalized = guideline.strip()

        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        result.append(normalized)

    return tuple(result)

def format_guidelines(
    tools: tuple[AgentTool, ...],
) -> str:
    """Format system-prompt guidelines."""
    return "\n".join(
        f"- {guideline}"
        for guideline in collect_guidelines(tools)
    )
def format_project_context(
    context_files: tuple[ProjectContextFile, ...],
) -> str:
    """将项目指令文件封装为 XML 格式的工具函数"""
    if not context_files:
        return ""
    sections: list[str] = [
        "<project_context>",
        "",
        "Project-specific instructions and guidelines:",
        "",
    ]
    for context_file in context_files:
        escaped_path = escape(str(context_file.path))
        sections.extend(
            [
                f'<project_instructions path="{escaped_path}">',
                context_file.content,
                "</project_instructions>",
                "",
            ]
        )

    sections.append("</project_context>")

    return "\n".join(sections)


def build_system_prompt(
        options: BuildSystemPromptOptions,
) -> str:
    """ Build system prompt """
    if options.custom_prompt is not None:
        prompt = options.custom_prompt
    else:
        prompt = (
            DEFAULT_IDENTITY
            + "\n\nAvailable tools:\n"
            + format_available_tools(options.tools)
            + "\n\nGuidelines:\n"
            + format_guidelines(options.tools)
        ) # 若不存在存在custom_prompt，给一个默认的身份和约束

    if options.append_system_prompt:
        prompt += f"\n\n{options.append_system_prompt.strip()}"# 附加提示词

    project_context = format_project_context(options.context_files)

    if project_context:
        prompt += f"\n\n{project_context}" # 项目上下文

    prompt += (
        f"\n\nCurrent date: {date.today().isoformat()}" # 日期
        f"\nCurrent working directory: {options.cwd.resolve()}" # 工作目录
    )

    return prompt