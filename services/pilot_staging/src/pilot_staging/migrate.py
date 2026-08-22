from __future__ import annotations

import hashlib
import re
from pathlib import Path

import psycopg

from .config import Settings


_MIGRATION = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_ID = 842018226


def run_migrations(settings: Settings | None = None) -> None:
    config = settings or Settings()
    migration_root = (
        config.migrations_path
        if config.migrations_path is not None
        else Path(__file__).resolve().parents[2] / "migrations"
    )
    files = sorted(migration_root.glob("*.sql"))
    if not files:
        raise RuntimeError("未找到 pilot migrations。")
    dsn = config.database_url().replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pilot_schema_migrations (
                    version integer PRIMARY KEY,
                    source_sha256 char(64) NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            for path in files:
                match = _MIGRATION.fullmatch(path.name)
                if match is None:
                    raise RuntimeError("Migration 文件名无效。")
                version = int(match.group(1))
                source = path.read_bytes()
                digest = hashlib.sha256(source).hexdigest()
                existing = connection.execute(
                    "SELECT source_sha256 FROM pilot_schema_migrations WHERE version=%s",
                    (version,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != digest:
                        raise RuntimeError("已应用 migration checksum drift。")
                    continue
                with connection.transaction():
                    connection.execute(source.decode("utf-8"))
                    connection.execute(
                        "INSERT INTO pilot_schema_migrations(version,source_sha256) VALUES (%s,%s)",
                        (version, digest),
                    )
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))


def main() -> None:
    run_migrations()


if __name__ == "__main__":
    main()
