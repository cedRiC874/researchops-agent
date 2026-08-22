BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inspection_jobs (
    job_id varchar(36) PRIMARY KEY,
    actor_hash char(64) NOT NULL,
    idempotency_digest char(64) NOT NULL UNIQUE,
    request_sha256 char(64) NOT NULL,
    dataset_id varchar(64) NOT NULL,
    status varchar(32) NOT NULL CHECK (
        status IN (
            'queued', 'retry_wait', 'running', 'publishing',
            'succeeded', 'failed', 'outcome_unknown'
        )
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts > 0),
    available_at timestamptz NOT NULL,
    lease_owner varchar(128),
    lease_token varchar(64),
    lease_expires_at timestamptz,
    expected_object_key text,
    expected_sha256 char(64),
    expected_bytes bigint CHECK (expected_bytes IS NULL OR expected_bytes >= 0),
    artifact_sha256 char(64),
    artifact_bytes bigint CHECK (artifact_bytes IS NULL OR artifact_bytes >= 0),
    error_code varchar(64),
    traceparent varchar(55),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    version integer NOT NULL DEFAULT 0 CHECK (version >= 0)
);

ALTER TABLE inspection_jobs
    ADD COLUMN IF NOT EXISTS traceparent varchar(55);

CREATE INDEX IF NOT EXISTS ix_inspection_jobs_queue
    ON inspection_jobs (status, available_at, created_at);

CREATE TABLE IF NOT EXISTS job_events (
    job_id varchar(36) NOT NULL REFERENCES inspection_jobs(job_id),
    sequence integer NOT NULL CHECK (sequence >= 0),
    event_type varchar(64) NOT NULL,
    payload jsonb NOT NULL,
    previous_hash char(64) NOT NULL,
    event_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (job_id, sequence)
);

CREATE OR REPLACE FUNCTION reject_job_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'job_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS job_events_no_update ON job_events;
CREATE TRIGGER job_events_no_update
    BEFORE UPDATE ON job_events
    FOR EACH ROW EXECUTE FUNCTION reject_job_event_mutation();

DROP TRIGGER IF EXISTS job_events_no_delete ON job_events;
CREATE TRIGGER job_events_no_delete
    BEFORE DELETE ON job_events
    FOR EACH ROW EXECUTE FUNCTION reject_job_event_mutation();

INSERT INTO schema_migrations(version) VALUES (1)
ON CONFLICT (version) DO NOTHING;

COMMIT;
