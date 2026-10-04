"""Shared retry helpers for provider adapters."""

from __future__ import annotations

from asyncio import sleep

from minitau_agent.provider import CancellationToken

RETRY_POLL_SECONDS = 0.05
RETRY_BASE_DELAY_SECONDS = 0.25

# 瞬时错误：重试有可能自愈的。408 超时、409 冲突、425 太早、429 限流、5xx 服务端抖
TRANSIENT_STATUS_CODES = frozenset({408, 409, 425, 429})
# 4xx = 客户端错误，重试没有意义：
# 400（请求本身坏）、401（key 错，重试一万次还是 401）、403、404。重试它们只会白白烧配额。
# 例外四个：408（客户端超时，网络抖动）、429（限流，等一会儿配额窗口就回来了）、
# 409/425（并发冲突/太早，重试可能撞上好时机）
# 5xx 全算瞬时：500 是对面炸了，502/503/504 是网关问题——服务端的事，跟请求无关

def is_transient_status(status_code: int) -> bool:
    """可重试的 HTTP 状态码分类：瞬时错误集合 + 一切 5xx。"""
    return status_code in TRANSIENT_STATUS_CODES or status_code >= 500


def retry_delay_seconds(attempt: int, *, max_delay_seconds: float) -> float:
    """
    Return an exponential retry delay capped by provider config.
    指数退避 base * 2^attempt，设置等待上限
    """
    if max_delay_seconds <= 0:
        return 0.0
    base_delay = min(RETRY_BASE_DELAY_SECONDS, max_delay_seconds)
    return float(min(max_delay_seconds, base_delay * (2**attempt)))


async def wait_for_retry(
    delay_seconds: float,
    *,
    signal: CancellationToken | None,
) -> bool:
    """Sleep before a retry while allowing cancellation to interrupt backoff.
    可随时中断的退避等待器
    返回 True：表示“睡够了，且没有被取消，可以继续重试”。
    返回 False：表示“在等待期间或一开始就被用户取消了，请放弃重试”。
    """
    if delay_seconds <= 0:
        return signal is None or not signal.is_cancelled()
    # 传入参数情况	            signal is None	signal.is_cancelled()	    最终返回值
    # 没传信号（signal = None）	True（触发短路）	(根本不会执行)	            True
    # 传了信号，但未被取消	        False	        False (not False -> True)	True
    # 传了信号，且已经被取消	    False	        True (not True -> False)	False

    remaining = delay_seconds
    while remaining > 0:
        if signal is not None and signal.is_cancelled():# 先看一眼信号来没来
            return False # 传入信号且被取消
        step = min(RETRY_POLL_SECONDS, remaining)
        await sleep(step)
        remaining -= step
    return signal is None or not signal.is_cancelled()
