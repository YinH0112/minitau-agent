from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol, runtime_checkable


@runtime_checkable
class SupportsAclose(Protocol):
    """运行时识别可关闭的异步流；普通 AsyncIterator 不保证有 aclose。"""

    async def aclose(self) -> None: ...


@asynccontextmanager
async def closing_async_iterator[T](
    stream: AsyncIterator[T],
) -> AsyncIterator[AsyncIterator[T]]:
    """将子流交给调用方迭代，并在离开代码块时等待它完成清理。"""

    try:
        # yield 的是同一个流；这里不读取事件，读取仍由调用方的 async for 完成。
        yield stream
    finally:
        # 外层提前 aclose()、task.cancel() 或正常走完都会经过这里。
        # Provider 协议目前只声明 AsyncIterator，因此仅对实际支持 aclose 的流调用它。
        if isinstance(stream, SupportsAclose):
            await stream.aclose()
