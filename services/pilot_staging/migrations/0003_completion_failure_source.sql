ALTER TABLE pilot_attempts
    ADD COLUMN completion_failure_source varchar(64);

ALTER TABLE pilot_attempts
    ADD CONSTRAINT pilot_attempt_completion_failure_source_allowlist
    CHECK (
        completion_failure_source IS NULL
        OR completion_failure_source IN (
            'final_output_missing',
            'response_output_item_incomplete',
            'response_not_completed',
            'output_limit_suspected'
        )
    ) NOT VALID;

ALTER TABLE pilot_attempts
    ADD CONSTRAINT pilot_attempt_completion_failure_source_mapping
    CHECK (
        completion_failure_source IS NULL
        OR (
            error_code IS NOT NULL
            AND outcome = 'controlled_failure'
            AND (
                (
                    completion_failure_source IN (
                        'final_output_missing',
                        'response_output_item_incomplete'
                    )
                    AND error_code = 'provider_output_incomplete'
                )
                OR (
                    completion_failure_source = 'response_not_completed'
                    AND error_code = 'provider_output_not_completed'
                )
                OR (
                    completion_failure_source = 'output_limit_suspected'
                    AND error_code = 'output_limit_suspected'
                )
            )
        )
    ) NOT VALID;

ALTER TABLE pilot_attempts
    VALIDATE CONSTRAINT pilot_attempt_completion_failure_source_allowlist;
ALTER TABLE pilot_attempts
    VALIDATE CONSTRAINT pilot_attempt_completion_failure_source_mapping;
