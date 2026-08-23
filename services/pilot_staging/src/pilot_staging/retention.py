from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .config import Settings
from .postgres import PostgresPilotStore


def purge_expired(settings: Settings | None = None) -> dict[str, int | bool]:
    config = settings or Settings()
    now = datetime.now(UTC)
    # The documented limits are maxima. A daily scheduler needs one full interval
    # of headroom so a record is deleted no later than day 7/day 90.
    withdrawal_before = now - timedelta(days=6)
    retention_due_by = now + timedelta(days=1)
    store = PostgresPilotStore(config.database_url())
    try:
        return dict(
            store.purge_expired_records(
                now=now,
                retention_due_by=retention_due_by,
                withdrawal_before=withdrawal_before,
                invite_retention_before=now
                - timedelta(days=config.retention_days),
            )
        )
    finally:
        store.close()


def main() -> None:
    result = purge_expired()
    print(
        "retention purge complete: "
        f"participants={result['participant_records_deleted']} "
        f"invites={result['invite_records_deleted']}"
    )


if __name__ == "__main__":
    main()
