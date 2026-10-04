""""
 Provider factory: translate a provider name into a wired instance.
 将“提供商的名字（字符串）”翻译成“已经连接好的实例对象”
"""
from __future__ import annotations

from minitau_agent.message import AssistantMessage, TextContent
from minitau_agent.provider import ModelProvider
from minitau_agent.provider_events import AssistantDoneEvent, AssistantStartEvent
from minitau_ai.demo import DemoProvider
from minitau_ai.env import openai_compatible_config_from_env
from minitau_ai.fake import FakeProvider
from minitau_ai.openai_compatible import OpenAICompatibleProvider

DEFAULT_FAKE_MODEL = "fake"
FAKE_GREETING = "Hello from the minitau fake provider!"

# name -> (api_key 环境变量, 默认 base_url, 默认 model)
PROVIDER_PRESETS = {
    # api_key 变量、base_url、默认模型
    "deepseek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-flash"),
    "kimi": ("MOONSHOT_API_KEY", "https://api.moonshot.cn/v1", "kimi-k2-0905-preview"),
    "openai": ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
}



def create_provider(name: str | None) -> tuple[ModelProvider, str]:
    """Return a provider and its default model for the given name."""
    if name == "demo":
        return DemoProvider(), "demo"
    if name is None or name == "fake":
        greeting = AssistantMessage(
            content=[TextContent(text=FAKE_GREETING)], model=DEFAULT_FAKE_MODEL
        )
        return FakeProvider([
            [AssistantStartEvent(partial=AssistantMessage(model=DEFAULT_FAKE_MODEL)),
             AssistantDoneEvent(reason="stop", message=greeting)]
        ]), DEFAULT_FAKE_MODEL
    if name in PROVIDER_PRESETS:
        api_key_var, default_base_url, default_model = PROVIDER_PRESETS[name]
        try:
            config = openai_compatible_config_from_env(
                api_key_var=api_key_var, default_base_url=default_base_url
            )
        except RuntimeError as exc:
            # cli 只捕 ValueError——错误类型在工厂边界统一（契约！）
            raise ValueError(str(exc)) from exc
        provider = OpenAICompatibleProvider(config)
        return provider, default_model
    raise ValueError(
        f"Unknown provider {name!r}; available providers: fake, {', '.join(PROVIDER_PRESETS)}"
    )
