"""终端配置向导：收集输入，校验，再保存；不向模型发送任何输入。"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from minitau_coding.provider_runtime import PROVIDER_NAMES, ProviderRuntime
from minitau_coding.provider_settings import ProviderSettings

type Ask = Callable[[str], str]
type Show = Callable[[str], None]


class ConfigurationCancelled(Exception):
    """q / Ctrl+C / EOF 结束向导，当前会话保持不变。"""


def configure_provider(
    runtime: ProviderRuntime,
    name: str | None,
    *,
    ask: Ask,
    ask_secret: Ask,
    show: Show,
) -> str:
    """阻塞输入应放在 idle 的 SIGINT 处理器下，不能冒充正在运行的模型请求。"""
    try:
        return _configure(runtime, name, ask=ask, ask_secret=ask_secret, show=show)
    except (EOFError, KeyboardInterrupt):
        raise ConfigurationCancelled from None


def _configure(
    runtime: ProviderRuntime, name: str | None, *, ask: Ask, ask_secret: Ask, show: Show
) -> str:
    show("配置供应商（q 取消；API key 隐藏输入）。")
    if name is None:
        for index, candidate in enumerate(PROVIDER_NAMES, 1):
            description = {
                "custom": "其他 OpenAI 兼容服务",
                "demo": "离线多轮体验，无需 key",
                "fake": "离线单次测试，无需 key",
            }.get(candidate, "")
            show(f"  {index}. {candidate}" + (f"（{description}）" if description else ""))
        while True:
            choice = ask("供应商名称或编号：").strip().lower()
            if choice == "q":
                raise ConfigurationCancelled
            if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(PROVIDER_NAMES):
                name = PROVIDER_NAMES[int(choice) - 1]
            elif choice in PROVIDER_NAMES:
                name = choice
            if name is not None:
                break
            show("请选择列表中的供应商。")
    runtime.validate_name(name)
    if name in {"demo", "fake"}:
        runtime.store.set_default(name)
        show(f"默认供应商已设为 {name}。")
        return name

    previous = runtime.settings_for(name)
    while True:
        url = ask(f"Base URL [{previous.base_url}]：").strip()
        if url.lower() == "q":
            raise ConfigurationCancelled
        model = ask(f"模型 ID [{previous.model}]：").strip()
        if model.lower() == "q":
            raise ConfigurationCancelled
        try:
            settings = ProviderSettings(
                base_url=url or previous.base_url, model=model or previous.model
            )
            break
        except ValidationError:
            show("端点或模型 ID 无效：请输入 HTTP(S) 地址及不含空格的模型 ID。")

    current_key = runtime.api_key(name)
    show("将设置和凭据保存到当前项目 .minitau/；凭据是本地明文文件，已被 Git 忽略。")
    while True:
        hint = "（回车保留已有 key）" if current_key else "（必填）"
        key = ask_secret(f"API key {hint}：").strip()
        if key.lower() == "q":
            raise ConfigurationCancelled
        key = key or current_key or ""
        if key and not any(char.isspace() for char in key):
            break
        show("请输入不含空白字符的 API key。")
    runtime.store.save_provider(name, settings, api_key=key)
    show(f"已保存 {name} / {settings.model}。配置未发出 API 请求，可通过下一条消息验证。")
    return name
