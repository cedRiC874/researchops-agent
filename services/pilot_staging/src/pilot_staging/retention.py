from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text

from .config import Settings


def purge_expired(settings: Settings | None = None) -> dict[str, int | bool]:
    config = settings or Settings()
    now = datetime.now(UTC)
    # The documented limits are maxima. A daily scheduler needs one full interval
    # of headroom so a record is deleted no later than day 7/day 90.
    withdrawal_before = now - timedelta(days=6)
    retention_due_by = now + timedelta(days=1)
    engine = create_engine(config.database_url(), pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            deleted_participants = connection.execute(
                text(
                    """
                    DELETE FROM pilot_participants
                    WHERE (delete_by<=:retention_due_by)
                       OR (withdrawn_at IS NOT NULL AND withdrawn_at<=:withdrawal_before)
                    RETURNING participant_id
                    """
                ),
                {
                    "retention_due_by": retention_due_by,
                    "withdrawal_before": withdrawal_before,
                },
            ).all()
            deleted_invites = connection.execute(
                text(
                    """
                    DELETE FROM pilot_invites
                    WHERE expires_at<:now AND (used_at IS NULL OR used_at<:retention_before)
                    RETURNING invite_id
                    """
                ),
                {
                    "now": now,
                    "retention_before": now - timedelta(days=config.retention_days),
                },
            ).all()
            connection.execute(
                text("DELETE FROM pilot_rate_limits WHERE window_id<:window"),
                {"window": int(now.timestamp()) // 60 - 1440},
            )
        return {
            "participant_records_deleted": len(deleted_participants),
            "invite_records_deleted": len(deleted_invites),
            "secret_values_printed": False,
        }
    finally:
        engine.dispose()


def main() -> None:
    result = purge_expired()
    print(
        "retention purge complete: "
        f"participants={result['participant_records_deleted']} "
        f"invites={result['invite_records_deleted']}"
    )


if __name__ == "__main__":
    main()
