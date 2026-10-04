"""Helpers for surfacing safe provider HTTP error details."""

from __future__ import annotations

from collections.abc import Mapping
from json import JSONDecodeError, loads
from typing import Any

_MAX_ERROR_DETAIL_LENGTH = 1000


def provider_http_error_message(
        *,
        provider_name: str,
        status_code: int,
        body: str,
        model: str | None = None,
) -> str:
    """Return an actionable, secret-free HTTP error message for a provider response."""
    prefix = f"{provider_name} request failed with status {status_code}"
    if model:
        prefix = f"{prefix} for model {model}"
    detail = provider_http_error_detail(body)
    if detail:
        return f"{prefix}: {detail}"
    return prefix

def provider_http_error_detail(body: str) -> str:
    """ Extract a concise provider-supplied error detail from an HTTP body """
    parsed = _loads_object(body)
    if parsed is not None:
        detail = provider_error_detail_from_mapping(parsed)
        if detail:
            return detail
    return body.strip()[:_MAX_ERROR_DETAIL_LENGTH]

def provider_error_detail_from_mapping(value: Mapping[str, Any]) -> str:
    """
    Return the most useful message/code from a provider error object
    不同厂商的错误响应结构五花八门（OpenAI 用 {"error": {"message": ...}}，有的用顶层 detail，
    有的嵌套好几层），这个函数统一把它们捞出来，凑成一句人能看懂的报错
    """
    error = value.get("error")
    # 嵌套的 error 对象
    if isinstance(error, Mapping):
        message = error.get("message")
        # 先找message
        if isinstance(message, str) and message:
            return message
        code = error.get("code")
        # 没有message找code
        if isinstance(code, str) and code:
            return code
    for key in ("message", "detail", "error"):
        detail = value.get(key)
        if isinstance(detail, str) and detail:
            return detail # # 是字符串，直接用
        if isinstance(detail, Mapping):
            nested = provider_error_detail_from_mapping(detail) # # 是对象，递归进去找
            if nested:
                return nested
    return ""

def _loads_object(value: str) -> Mapping[str, Any] | None:
    try:
        parsed = loads(value)
    except JSONDecodeError:
        # json.loads() 收到不符合 JSON 语法的字符串时抛出
        return None
    return parsed if isinstance(parsed, Mapping) else None
    #  Mapping 而不是 dict 判断，只要求"有映射行为"，不强制是 dict 实例