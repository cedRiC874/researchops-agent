BEGIN;

ALTER TABLE inspection_jobs
    ADD COLUMN IF NOT EXISTS lease_owner varchar(128),
    ADD COLUMN IF NOT EXISTS lease_token varchar(64),
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS expected_object_key text,
    ADD COLUMN IF NOT EXISTS expected_sha256 char(64),
    ADD COLUMN IF NOT EXISTS expected_bytes bigint,
    ADD COLUMN IF NOT EXISTS artifact_sha256 char(64),
    ADD COLUMN IF NOT EXISTS artifact_bytes bigint,
    ADD COLUMN IF NOT EXISTS error_code varchar(64),
    ADD COLUMN IF NOT EXISTS traceparent varchar(55);

ALTER TABLE inspection_jobs
    DROP CONSTRAINT IF EXISTS inspection_jobs_status_check;

ALTER TABLE inspection_jobs
    ADD CONSTRAINT inspection_jobs_status_check CHECK (
        status IN (
            'queued', 'retry_wait', 'running', 'publishing',
            'succeeded', 'failed', 'outcome_unknown'
        )
    );

ALTER TABLE inspection_jobs
    DROP CONSTRAINT IF EXISTS inspection_jobs_expected_bytes_check;

ALTER TABLE inspection_jobs
    ADD CONSTRAINT inspection_jobs_expected_bytes_check CHECK (
        expected_bytes IS NULL OR expected_bytes >= 0
    );

ALTER TABLE inspection_jobs
    DROP CONSTRAINT IF EXISTS inspection_jobs_artifact_bytes_check;

ALTER TABLE inspection_jobs
    ADD CONSTRAINT inspection_jobs_artifact_bytes_check CHECK (
        artifact_bytes IS NULL OR artifact_bytes >= 0
    );

INSERT INTO schema_migrations(version) VALUES (2)
ON CONFLICT (version) DO NOTHING;

COMMIT;
