from __future__ import annotations

from pathlib import Path
import re
from typing import Literal
from urllib.parse import quote_plus, urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain import LOCKED_CANDIDATE_COMMITMENT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESEARCHOPS_PILOT_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    environment: Literal["local", "staging", "supervised"] = "local"
    public_base_url: str = "http://127.0.0.1:8090"
    allowed_hosts: str = "127.0.0.1,localhost,testserver"
    secure_cookies: bool = False
    database_host: str = "127.0.0.1"
    database_port: int = 5433
    database_name: str = "researchops_pilot"
    database_user: str = "researchops_pilot"
    database_password_file: Path
    database_sslmode: str = "disable"
    admin_token_file: Path
    token_pepper_file: Path
    provider_api_key_file: Path | None = None
    registry_path: Path
    project_root: Path
    provider_execution_enabled: bool = False
    candidate_commitment_sha256: str = LOCKED_CANDIDATE_COMMITMENT
    provider_id: str = "deepseek"
    model_id: str = "deepseek-v4-flash"
    worker_poll_seconds: float = Field(default=1.0, gt=0, le=30)
    worker_lease_seconds: int = Field(default=300, ge=180, le=600)
    session_ttl_hours: int = Field(default=8, ge=1, le=72)
    retention_days: int = Field(default=90, ge=1, le=90)
    retention_schedule_confirmed: bool = False
    deployment_git_sha: str | None = None
    deployment_image_digest: str | None = None

    @model_validator(mode="after")
    def fail_closed(self) -> "Settings":
        if self.candidate_commitment_sha256 != LOCKED_CANDIDATE_COMMITMENT:
            raise ValueError("Pilot candidate commitment 必须等于已锁定值。")
        if self.provider_id != "deepseek" or self.model_id != "deepseek-v4-flash":
            raise ValueError("Pilot Provider/model 必须等于已锁定 candidate。")
        parsed = urlparse(self.public_base_url)
        if self.environment in {"staging", "supervised"}:
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError(
                    f"{self.environment} public_base_url 必须为 HTTPS。"
                )
            if not self.secure_cookies:
                raise ValueError(f"{self.environment} 必须启用 Secure cookie。")
            allowed_hosts = self.allowed_host_values()
            if "*" in allowed_hosts or (
                self.environment == "supervised"
                and any("*" in host for host in allowed_hosts)
            ):
                raise ValueError(f"{self.environment} 禁止 wildcard Host。")
            if not self.retention_schedule_confirmed:
                raise ValueError(
                    f"{self.environment} 必须确认外部 daily retention schedule。"
                )
            if self.deployment_git_sha is None or self.deployment_image_digest is None:
                raise ValueError(
                    f"{self.environment} 必须绑定 deployment Git SHA 与 image digest。"
                )
            if re.fullmatch(r"[0-9a-f]{40,64}", self.deployment_git_sha) is None:
                raise ValueError("deployment_git_sha 格式无效。")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", self.deployment_image_digest) is None:
                raise ValueError("deployment_image_digest 格式无效。")
        if self.environment == "staging" and self.database_sslmode != "verify-full":
            raise ValueError("staging PostgreSQL 必须使用 sslmode=verify-full。")
        if self.environment == "supervised" and not self.provider_execution_enabled:
            raise ValueError("supervised 必须启用 Provider execution。")
        if self.provider_execution_enabled and self.provider_api_key_file is None:
            raise ValueError("开启在线执行时必须配置 server-side Provider secret file。")
        return self

    def database_url(self) -> str:
        password = quote_plus(_read_secret(self.database_password_file, "database"))
        return (
            f"postgresql+psycopg://{quote_plus(self.database_user)}:{password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
            f"?sslmode={quote_plus(self.database_sslmode)}"
        )

    def admin_token(self) -> str:
        return _read_secret(self.admin_token_file, "admin token", minimum_length=24)

    def token_pepper(self) -> bytes:
        return _read_secret(
            self.token_pepper_file, "token pepper", minimum_length=32
        ).encode("utf-8")

    def provider_api_key(self) -> str:
        if self.provider_api_key_file is None:
            raise RuntimeError("Provider secret file 未配置。")
        return _read_secret(
            self.provider_api_key_file, "Provider API key", minimum_length=8
        )

    def allowed_host_values(self) -> tuple[str, ...]:
        values = tuple(item.strip() for item in self.allowed_hosts.split(",") if item.strip())
        if not values:
            raise ValueError("allowed_hosts 不能为空。")
        return values


def _read_secret(path: Path, label: str, *, minimum_length: int = 8) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取 {label} secret file。") from exc
    if len(value) < minimum_length or "\n" in value or "\r" in value:
        raise RuntimeError(f"{label} secret 无效。")
    return value
