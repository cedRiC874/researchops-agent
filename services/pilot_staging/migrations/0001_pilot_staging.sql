CREATE TABLE IF NOT EXISTS pilot_schema_migrations (
    version integer PRIMARY KEY,
    source_sha256 char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot_campaigns (
    campaign_id varchar(32) PRIMARY KEY,
    title varchar(120) NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('draft','frozen','running','paused','complete','terminated')),
    execution_environment varchar(16) NOT NULL CHECK (execution_environment IN ('local','staging','supervised')),
    candidate_commitment_sha256 char(64) NOT NULL,
    protocol_sha256 char(64) NOT NULL,
    consent_sha256 char(64) NOT NULL,
    task_pack_sha256 char(64) NOT NULL,
    feedback_schema_sha256 char(64) NOT NULL,
    dataset_manifest_sha256 char(64) NOT NULL,
    deployment_git_sha varchar(64),
    deployment_image_digest varchar(71),
    provider_id varchar(32) NOT NULL,
    model_id varchar(128) NOT NULL,
    transport_id varchar(64) NOT NULL,
    max_provider_runs integer NOT NULL CHECK (max_provider_runs BETWEEN 1 AND 100),
    target_participants integer NOT NULL CHECK (
        (execution_environment = 'supervised' AND target_participants BETWEEN 1 AND 2)
        OR
        (execution_environment IN ('local','staging') AND target_participants BETWEEN 3 AND 5)
    ),
    created_at timestamptz NOT NULL,
    frozen_at timestamptz
);

CREATE TABLE IF NOT EXISTS pilot_tasks (
    campaign_id varchar(32) NOT NULL REFERENCES pilot_campaigns(campaign_id),
    task_id varchar(64) NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    source_task_id varchar(128) NOT NULL,
    dataset_id varchar(64) NOT NULL,
    scenario varchar(64) NOT NULL,
    prompt_en text NOT NULL CHECK (length(prompt_en) BETWEEN 1 AND 4000),
    prompt_zh text NOT NULL CHECK (length(prompt_zh) BETWEEN 1 AND 4000),
    context jsonb NOT NULL,
    clarification_expected boolean NOT NULL,
    PRIMARY KEY (campaign_id, task_id),
    UNIQUE (campaign_id, sequence)
);

CREATE TABLE IF NOT EXISTS pilot_invites (
    invite_id uuid PRIMARY KEY,
    campaign_id varchar(32) NOT NULL REFERENCES pilot_campaigns(campaign_id),
    token_digest char(64) NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pilot_participants (
    participant_id varchar(32) PRIMARY KEY,
    campaign_id varchar(32) NOT NULL REFERENCES pilot_campaigns(campaign_id),
    status varchar(16) NOT NULL CHECK (status IN ('invited','consented','active','completed','withdrawn')),
    session_instance_id varchar(48) NOT NULL UNIQUE,
    consent_document_sha256 char(64),
    eligibility_confirmed boolean NOT NULL DEFAULT false,
    consented_at timestamptz,
    withdrawn_at timestamptz,
    delete_by timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    CHECK (delete_by >= created_at),
    CHECK (delete_by <= created_at + interval '90 days')
);

CREATE TABLE IF NOT EXISTS pilot_web_sessions (
    session_digest char(64) PRIMARY KEY,
    participant_id varchar(32) NOT NULL REFERENCES pilot_participants(participant_id) ON DELETE CASCADE,
    csrf_digest char(64) NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pilot_attempts (
    attempt_id uuid PRIMARY KEY,
    participant_id varchar(32) NOT NULL REFERENCES pilot_participants(participant_id) ON DELETE CASCADE,
    campaign_id varchar(32) NOT NULL REFERENCES pilot_campaigns(campaign_id),
    task_id varchar(64) NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('assigned','queued','running','succeeded','failed','withheld','excluded','completed')),
    queued_at timestamptz,
    started_at timestamptz,
    provider_completed_at timestamptz,
    revealed_at timestamptz,
    completed_at timestamptz,
    safe_output text,
    output_sha256 char(64),
    provider_latency_ms integer CHECK (provider_latency_ms IS NULL OR provider_latency_ms >= 0),
    outcome varchar(64),
    error_code varchar(64),
    model_call_count integer,
    model_requested_tool_call_count integer,
    backend_executed_tool_call_count integer,
    lease_owner varchar(128),
    lease_expires_at timestamptz,
    UNIQUE (participant_id, task_id),
    FOREIGN KEY (campaign_id, task_id) REFERENCES pilot_tasks(campaign_id, task_id)
);

CREATE INDEX IF NOT EXISTS ix_pilot_attempt_queue
    ON pilot_attempts (status, queued_at);
CREATE INDEX IF NOT EXISTS ix_pilot_attempt_participant
    ON pilot_attempts (participant_id, task_id);

CREATE TABLE IF NOT EXISTS pilot_feedback (
    attempt_id uuid PRIMARY KEY REFERENCES pilot_attempts(attempt_id) ON DELETE CASCADE,
    understandable boolean NOT NULL,
    useful_for_next_step boolean NOT NULL,
    confidence varchar(8) NOT NULL CHECK (confidence IN ('low','medium','high')),
    needs_expert_review boolean NOT NULL,
    obvious_problem boolean NOT NULL,
    missing_information boolean NOT NULL,
    safety_concern boolean NOT NULL,
    clarification_useful boolean,
    notes text NOT NULL CHECK (length(notes) <= 2000),
    evaluator_role varchar(32) NOT NULL DEFAULT 'external_researcher_user',
    professional_correctness_assessed boolean NOT NULL DEFAULT false CHECK (professional_correctness_assessed = false),
    human_review_seconds integer NOT NULL CHECK (human_review_seconds BETWEEN 0 AND 7200),
    delete_by timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    CHECK (delete_by >= created_at)
);

CREATE TABLE IF NOT EXISTS pilot_incidents (
    incident_id uuid PRIMARY KEY,
    campaign_id varchar(32) NOT NULL REFERENCES pilot_campaigns(campaign_id),
    participant_id varchar(32) REFERENCES pilot_participants(participant_id) ON DELETE SET NULL,
    attempt_id uuid REFERENCES pilot_attempts(attempt_id) ON DELETE SET NULL,
    incident_type varchar(64) NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('unresolved','dismissed','confirmed')),
    created_at timestamptz NOT NULL,
    resolved_at timestamptz
);

CREATE TABLE IF NOT EXISTS pilot_rate_limits (
    principal_key char(64) NOT NULL,
    route_key varchar(64) NOT NULL,
    window_id bigint NOT NULL,
    request_count integer NOT NULL CHECK (request_count > 0),
    PRIMARY KEY (principal_key, route_key, window_id)
);

CREATE TABLE IF NOT EXISTS pilot_worker_heartbeats (
    worker_id varchar(128) PRIMARY KEY,
    candidate_commitment_sha256 char(64) NOT NULL,
    execution_environment varchar(16) NOT NULL CHECK (execution_environment IN ('local','staging','supervised')),
    deployment_image_digest varchar(71),
    last_seen_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS pilot_events (
    campaign_id varchar(32) NOT NULL REFERENCES pilot_campaigns(campaign_id),
    sequence integer NOT NULL CHECK (sequence >= 0),
    event_type varchar(64) NOT NULL,
    payload jsonb NOT NULL,
    previous_hash char(64) NOT NULL,
    event_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (campaign_id, sequence)
);

CREATE OR REPLACE FUNCTION reject_pilot_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'pilot_events is append-only';
END;
$$;

DROP TRIGGER IF EXISTS pilot_events_no_update ON pilot_events;
CREATE TRIGGER pilot_events_no_update
    BEFORE UPDATE ON pilot_events
    FOR EACH ROW EXECUTE FUNCTION reject_pilot_event_mutation();

DROP TRIGGER IF EXISTS pilot_events_no_delete ON pilot_events;
CREATE TRIGGER pilot_events_no_delete
    BEFORE DELETE ON pilot_events
    FOR EACH ROW EXECUTE FUNCTION reject_pilot_event_mutation();
