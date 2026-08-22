from __future__ import annotations

def main() -> None:
    raise SystemExit(
        "Use migrations/0001_jobs.sql (or the Compose migrate service); "
        "metadata.create_all is intentionally not a production migration path."
    )


if __name__ == "__main__":
    main()
