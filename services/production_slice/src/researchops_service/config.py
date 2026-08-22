from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESEARCHOPS_",
        env_file=".env",
        extra="ignore",
    )

    environment: str = "development"
    database_host: str = "127.0.0.1"
    database_port: int = 5432
    database_name: str = "researchops"
    database_user: str = "researchops"
    database_password_file: Path
    object_endpoint: str = "http://127.0.0.1:9000"
    object_region: str = "us-east-1"
    object_bucket: str = "researchops-artifacts"
    object_server_side_encryption: bool = False
    object_access_key_file: Path
    object_secret_key_file: Path
    api_token_file: Path
    idempotency_hmac_key_file: Path
    registry_path: Path
    otlp_http_endpoint: str | None = None
    worker_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    worker_lease_seconds: int = Field(default=60, ge=10, le=3600)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)

    def database_url(self) -> str:
        password = quote_plus(_read_secret(self.database_password_file, "database"))
        return (
            f"postgresql+psycopg://{quote_plus(self.database_user)}:{password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    def object_credentials(self) -> tuple[str, str]:
        return (
            _read_secret(self.object_access_key_file, "object access key"),
            _read_secret(self.object_secret_key_file, "object secret key"),
        )

    def api_token(self) -> str:
        return _read_secret(self.api_token_file, "API token", minimum_length=24)

    def idempotency_hmac_key(self) -> bytes:
        return _read_secret(
            self.idempotency_hmac_key_file,
            "idempotency HMAC key",
            minimum_length=32,
        ).encode("utf-8")


def _read_secret(path: Path, label: str, *, minimum_length: int = 8) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取 {label} secret file。") from exc
    if len(value) < minimum_length or "\n" in value or "\r" in value:
        raise RuntimeError(f"{label} secret 无效。")
    return value
