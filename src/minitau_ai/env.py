"""Provider 配置与环境变量读取。密钥只在这里落地，之后以 Config 对象传递。"""

from __future__ import annotations

from dataclasses import dataclass
from os import environ

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_RETRY_DELAY_SECONDS = 8.0


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    max_retry_delay_seconds: float = DEFAULT_MAX_RETRY_DELAY_SECONDS


def openai_compatible_config_from_env(
    *,
    api_key_var: str = "OPENAI_API_KEY",
    base_url_var: str = "MINITAU_BASE_URL",
    default_base_url: str = "https://api.openai.com/v1",
) -> OpenAICompatibleConfig:
    """从环境变量装载配置。缺 key 直接 RuntimeError——宁可启动失败不带错跑。"""
    api_key = environ.get(api_key_var)
    if not api_key:
        raise RuntimeError(f"Missing required environment variable: {api_key_var}")
    return OpenAICompatibleConfig(
        api_key=api_key,
        base_url=environ.get(base_url_var, default_base_url).rstrip("/"),
    )