"""Provider-neutral tool definitions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from minitau_agent.cancellation import CancellationToken
from minitau_agent.types import JSONValue


@dataclass(frozen=True, slots=True)
# 创建后不能改 不用 __dict__ 存属性，每个实例省约 40-50% 内存，在大量工具调用结果的场景下有意义
class AgentToolResult:
    """ 工具执行结果的封装类 """
    content: str  #  工具返回的文本内容
    details: JSONValue = None #  结构化详情（可选）

type ToolExecutor = Callable[
    [Mapping[str, JSONValue]],
    Awaitable[AgentToolResult]
]
type CancellableToolExecutor = Callable[
    [Mapping[str, JSONValue], CancellationToken | None], Awaitable[AgentToolResult]
]
# 所有注册的工具都必须符合这个签名

@ dataclass(frozen=True, slots=True)
class AgentTool:
    """ 给Agent用的工具 """
    name: str
    description: str # 个工具什么时候用
    parameters: Mapping[str, JSONValue] # 参数定义
    execute_fn: ToolExecutor # 执行函数
    prompt_snippet: str | None = None # 提示片段
    prompt_guidelines: tuple[str, ...] = () # 提示指南
    execute_with_signal: CancellableToolExecutor | None = None

    @property
    def input_schema(self) -> Mapping[str, JSONValue]:
        return self.parameters

    async def execute(
        self,
        arguments: Mapping[str, JSONValue],
        *,
        signal: CancellationToken | None = None,
    ) -> AgentToolResult:
        if self.execute_with_signal is not None:
            return await self.execute_with_signal(arguments, signal)
        return await self.execute_fn(arguments)
    # 调用工具用异步

