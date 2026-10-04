"""项目级供应商设置与凭据；参考 tau 的 provider_config / credentials 分层。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ProviderSettings(BaseModel):
    """可以公开展示的设置，刻意不包含 API key。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    base_url: str
    model: str

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() for char in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Base URL 必须是 HTTP(S) 地址，不能包含账号、密码或查询参数")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char.isspace() for char in value):
            raise ValueError("模型 ID 不能为空或包含空白字符")
        return value


class SavedSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    default_provider: str | None = None
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)


class SavedCredentials(BaseModel):
    # repr=False 防止调试输出顺手把凭据打印出来。
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_keys: dict[str, str] = Field(default_factory=dict, repr=False)


def _read(path: Path, *, credentials: bool = False) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else "{}"
    except OSError as exc:
        kind = "凭据" if credentials else "设置"
        raise ValueError(f"无法读取{kind}文件：{path}") from exc


def _write(path: Path, content: str) -> None:
    """同目录临时文件 + replace，写到一半失败不会破坏原配置。"""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            temporary.chmod(0o600)
            handle.write(content + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        raise ValueError(f"无法保存本地配置：{path}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ProviderSettingsStore:
    """与会话文件分开保存；.minitau/ 已被 Git 忽略。"""

    def __init__(self, cwd: Path) -> None:
        directory = cwd.resolve() / ".minitau"
        self.settings_path = directory / "settings.json"
        self.credentials_path = directory / "credentials.json"

    def load(self) -> SavedSettings:
        try:
            return SavedSettings.model_validate_json(_read(self.settings_path))
        except ValidationError:
            # 不转发 ValidationError：其中可能包含用户输入的原始值。
            raise ValueError(f"设置文件格式错误：{self.settings_path}") from None

    def api_key(self, name: str) -> str | None:
        return self._credentials().api_keys.get(name)

    def _credentials(self) -> SavedCredentials:
        try:
            return SavedCredentials.model_validate_json(
                _read(self.credentials_path, credentials=True)
            )
        except ValidationError:
            raise ValueError(f"凭据文件格式错误：{self.credentials_path}") from None

    def save_provider(self, name: str, settings: ProviderSettings, *, api_key: str) -> None:
        key = api_key.strip()
        if not key or any(char.isspace() for char in key):
            raise ValueError("API key 不能为空或包含空白字符")
        saved = self.load()
        keys = dict(self._credentials().api_keys)
        keys[name] = key
        # 先保存凭据，再发布设置。中途失败时不会出现“设置指向缺失凭据”。
        _write(self.credentials_path, SavedCredentials(api_keys=keys).model_dump_json(indent=2))
        updated = SavedSettings(
            default_provider=name, providers={**saved.providers, name: settings}
        )
        self._save(updated)

    def set_default(self, name: str) -> None:
        saved = self.load()
        self._save(saved.model_copy(update={"default_provider": name}))

    def save_model(self, name: str, model: str) -> None:
        saved = self.load()
        previous = saved.providers[name]
        updated = ProviderSettings(base_url=previous.base_url, model=model)
        self._save(saved.model_copy(update={"providers": {**saved.providers, name: updated}}))

    def _save(self, settings: SavedSettings) -> None:
        _write(self.settings_path, json.dumps(settings.model_dump(), indent=2, ensure_ascii=False))
