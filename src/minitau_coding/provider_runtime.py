"""把持久设置变成 HTTP Provider，并集中管理客户端的生命周期。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from minitau_agent.provider import ModelProvider
from minitau_ai.env import OpenAICompatibleConfig
from minitau_ai.factory import PROVIDER_PRESETS, create_provider
from minitau_ai.openai_compatible import OpenAICompatibleProvider
from minitau_coding.provider_settings import ProviderSettings, ProviderSettingsStore

type ProviderFactory = Callable[[str | None], tuple[ModelProvider, str]]
PROVIDER_NAMES = (*PROVIDER_PRESETS, "custom", "demo", "fake")


class MissingProviderCredentials(ValueError):
    """交互入口可以捕获它并打开配置向导；print 模式直接报错。"""


class ProviderRuntime:
    def __init__(self, cwd: Path, *, factory: ProviderFactory = create_provider) -> None:
        self.store = ProviderSettingsStore(cwd)
        self._factory = factory
        self._owned: list[ModelProvider] = []

    @property
    def default_provider(self) -> str | None:
        return self.store.load().default_provider

    def settings_for(self, name: str) -> ProviderSettings:
        self.validate_name(name)
        saved = self.store.load().providers.get(name)
        if saved is not None:
            return saved
        if name == "custom":
            return ProviderSettings(base_url="http://localhost:8000/v1", model="your-model-id")
        if name not in PROVIDER_PRESETS:
            raise ValueError(f"{name} 不需要 API 配置")
        _env, url, model = PROVIDER_PRESETS[name]
        return ProviderSettings(base_url=url, model=model)

    def api_key(self, name: str) -> str | None:
        # 用户在应用里保存的新 key 优先，避免旧环境变量继续覆盖它。
        saved = self.store.api_key(name)
        if saved:
            return saved
        env_var = PROVIDER_PRESETS[name][0] if name in PROVIDER_PRESETS else "MINITAU_API_KEY"
        return os.environ.get(env_var)

    @staticmethod
    def validate_name(name: str) -> None:
        if name not in PROVIDER_NAMES:
            raise ValueError(f"未知供应商 {name!r}；可选：{', '.join(PROVIDER_NAMES)}")

    def create(self, name: str | None) -> tuple[ModelProvider, str]:
        name = name or "fake"
        self.validate_name(name)
        if name in {"demo", "fake"}:
            provider, model = self._factory(name)
        else:
            settings = self.store.load().providers.get(name)
            if settings is None and name != "custom" and not self.store.api_key(name):
                # 兼容旧命令行和环境变量配置，同时保留工厂的注入测试接口。
                try:
                    provider, model = self._factory(name)
                except ValueError as exc:
                    if not self.api_key(name):
                        raise MissingProviderCredentials(
                            f"{name} 尚未配置 API key；请在交互模式使用 /config {name}"
                        ) from exc
                    raise
            else:
                key = self.api_key(name)
                if not key:
                    raise MissingProviderCredentials(
                        f"{name} 尚未配置 API key；请在交互模式使用 /config {name}"
                    )
                if settings is None:
                    if name == "custom":
                        raise MissingProviderCredentials(
                            "custom 尚未配置端点；请使用 /config custom"
                        )
                    # 凭据保存成功、设置保存失败后也优先使用已保存的 key。
                    settings = self.settings_for(name)
                provider = OpenAICompatibleProvider(
                    OpenAICompatibleConfig(api_key=key, base_url=settings.base_url)
                )
                model = settings.model
        self._owned.append(provider)
        return provider, model

    def describe(self, current: str) -> str:
        lines = [f"当前供应商：{current}"]
        for name in PROVIDER_NAMES:
            if name in {"fake", "demo"}:
                state = "离线，无需 API key"
            else:
                configured = name != "custom" or name in self.store.load().providers
                state = "已配置" if configured and self.api_key(name) else "未配置"
            lines.append(f"{'*' if name == current else ' '} {name}: {state}")
        return "\n".join(lines) + "\n/config [供应商] 配置；/provider <供应商> 切换。"

    def save_model(self, name: str, model: str) -> None:
        """会话的 /model 同时更新该供应商下一次启动使用的默认模型。"""
        saved = self.store.load().providers.get(name)
        if saved is not None:
            self.store.save_model(name, model)

    async def aclose(self) -> None:
        # 与 tau 的 owned_providers 一样，Session 借用实例，应用入口统一释放。
        providers, self._owned = self._owned, []
        errors: list[Exception] = []
        closed: set[int] = set()
        for provider in reversed(providers):
            if id(provider) in closed:
                continue
            closed.add(id(provider))
            try:
                await provider.aclose()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Failed to close providers", errors)
