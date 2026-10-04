"""HTTP client helpers shared by minitau network integrations.
Python 的现代网络库 httpx 非常严格，它只认具体的版本号（如 socks5:// 或 socks5h://），
只要看到通用的 socks:// 就会直接报错崩溃！
这段代码的目的就是：在不永久搞乱用户电脑配置的前提下，优雅地骗过 httpx

"""

# 让所有注解变成字符串、延迟求值。
# 本文件里 Iterator[None]、str | None 这些都不是运行时要求值的东西，
# 没有这行在旧版本上会报未定义/不支持
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

# 代理环境变量名。大小写两种都要：
# - 小写（http_proxy）是 Linux / curl / git 的惯例
# - 大写（HTTP_PROXY）是 Windows 和部分工具的惯例
# httpx 两个都认，所以两个都要归一化
_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def normalize_proxy_url(proxy_url: str) -> str:
    """Return an httpx-compatible proxy URL.
    如果发现开头是 socks://，切掉它，换上 socks5://；如果不是（比如原本就是 http://），原样返回
    有些环境用 ``socks://`` 当 SOCKS 代理的通用写法，但 httpx 只认
    明确版本（socks5:// / socks5h://），通用 scheme 会在建客户端时
    直接被拒。这里把通用形式当作 SOCKS5 处理。
    """

    # 前置条件：socks 支持需要额外依赖 httpx[socks]（底层是 socksio）。
    # 没装的话这里不报错，要等到真正发请求时才抛
    # "Using SOCKS proxy, but the 'socksio' package is not installed"

    # 用 lower() 判断：URL scheme 大小写不敏感（"SOCKS://" 也是合法的）
    if proxy_url.lower().startswith("socks://"):
        # 切片用的是原字符串（不是 lower 后的），所以 host:port 部分原样保留。
        # 只替换前 8 个字符 "socks://" -> "socks5://"
        #
        # 选 socks5 而不是 socks5h：
        #   socks5  = 本地解析 DNS，再把目标 IP 交给代理
        #   socks5h = 由代理服务器解析 DNS（远端解析）
        # 通用写法无法表达意图，默认按本地解析处理
        return f"socks5://{proxy_url[len('socks://') :]}"
    return proxy_url


@contextmanager
def normalized_proxy_environment() -> Iterator[None]:
    """Temporarily normalize proxy environment variables for httpx construction.
    巡查那 6 个代理环境变量（HTTP_PROXY, ALL_PROXY 等）；
    只要发现有不合规的 socks://，先把原始值存进 original 字典备份好，然后临时改成 socks5://；
    执行 yield：按暂停键，把舞台让出来；
    只要离开舞台（无论正常还是异常），finally 强制出马：按照备份字典，
    把所有改动的环境变量原封不动地还回去
    """

    # @contextmanager 把一个生成器函数变成上下文管理器：
    #  yield 之前的代码：在进入 with 代码块（即 __enter__）时执行；
    # yield 的值：会作为 with ... as target 赋给 target
    # （这里 yield 没有带值，默认为 None，因此返回类型注解为 Iterator[None]）；
    # yield 之后的代码：在离开 with 代码块（即 __exit__）时执行。

    # 记录被改过的变量的原值，用于退出时还原。
    # 注意：走到这里的 value 一定不是 None（下面 continue 掉了），
    # 类型写成 str | None 只是为了兼容下面 if value is None 的防御分支
    original: dict[str, str | None] = {}
    changed = False

    for name in _PROXY_ENV_VARS:
        value = os.environ.get(name) # os.environ 是操作系统环境变量的映射对象,读取指定名称name的值
        if value is None:          # 该变量没设置，跳过（也不需要还原）
            continue
        normalized = normalize_proxy_url(value)
        if normalized == value:    # 本来就是 httpx 认的格式，不用改
            continue
        original[name] = value     # 先记下原值
        os.environ[name] = normalized   # 再覆盖
        changed = True

    try:
        yield                       # ← 暂停在这里，with 块内的代码在此执行
    finally:
        # finally 保证即使 with 块里抛异常也一定会 <还原>，
        # 否则进程的环境变量会被永久改掉（影响后续所有网络请求）
        if changed:                 # 没改过就什么都不做，省掉无谓的写操作
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)   # 第二参数：键不存在也不抛 KeyError
                else:
                    os.environ[name] = value



# *args 传的是“单值”列表：
# 每次只传一个一个的孤立值：1, "hello", True
# 底层用 元组 (1, "hello", True) 把它们装起来。
# 就像排队报数，只认先后顺序，没有名字。

# **kwargs 传的是“键值对（Key-Value）”：
# 每次必须成对传：名字 = 值，比如 timeout=10, retry=3
# 底层用 字典 {"timeout": 10, "retry": 3} 把它们装起来。
# 就像填表格，每一项都必须有明确的表头（字段名）和对应的内容。

def create_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` with minitau's proxy normalization applied."""

    # **kwargs: Any 表示"任意关键字参数，值类型不限",支持传任意多个对
    # （参数名是 str，所以不用写类型；冒号后面标注的是值的类型）
    #
    # 为什么必须在 with 里构造：
    # httpx 读取环境变量的调用链是
    #   AsyncClient.__init__ → _get_proxy_map(proxy, allow_env_proxies)
    #                        → get_environment_proxies()
    # （见 httpx/_client.py:1400，allow_env_proxies = trust_env and transport is None）
    # 只在**构造那一刻**读，读完就固化进 client 对象，之后改环境变量对它无效。
    # 所以临时改环境变量的作用域，必须把"构造"这一步整个包住。
    with normalized_proxy_environment():
        return httpx.AsyncClient(**kwargs)
    # 解包参数（**kwargs）：把前面收到的所有“键值对配置”比如 timeout=30, headers={...}
    # 等自动打散、 还原成标准参数

    # 出了 with，环境变量已还原，但 client 内部已记住归一化后的代理地址——
    # 整个技巧能成立，靠的就是这个时间差。
    #
    # 注意：os.environ 是进程级全局状态。
    # 单线程事件循环下没有风险——with 块内只有同步的构造函数，没有 await 点，不会被打断。
    # 多线程并发调用时则会互相干扰（一个还原了，另一个还没构造完）。
    # 要彻底免掉全局副作用，可以改成显式传 proxies= 参数给 AsyncClient。
