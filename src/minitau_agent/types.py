""" Shared JSON-compatible types"""

from __future__ import annotations

# 让所有注解变为字符串（延迟求值），避免前向引用问题，因为 JSONValue 在定义时就引用了自身

type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]

# 逐层细化了 JSON 数据的类型结构
# 提供统一类型
