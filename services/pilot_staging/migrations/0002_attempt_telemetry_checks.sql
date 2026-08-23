ALTER TABLE pilot_attempts
    ADD CONSTRAINT pilot_attempt_model_call_count_nonnegative
    CHECK (model_call_count IS NULL OR model_call_count >= 0) NOT VALID;

ALTER TABLE pilot_attempts
    ADD CONSTRAINT pilot_attempt_requested_tool_call_count_nonnegative
    CHECK (
        model_requested_tool_call_count IS NULL
        OR model_requested_tool_call_count >= 0
    ) NOT VALID;

ALTER TABLE pilot_attempts
    ADD CONSTRAINT pilot_attempt_backend_tool_call_count_nonnegative
    CHECK (
        backend_executed_tool_call_count IS NULL
        OR backend_executed_tool_call_count >= 0
    ) NOT VALID;

ALTER TABLE pilot_attempts
    VALIDATE CONSTRAINT pilot_attempt_model_call_count_nonnegative;
ALTER TABLE pilot_attempts
    VALIDATE CONSTRAINT pilot_attempt_requested_tool_call_count_nonnegative;
ALTER TABLE pilot_attempts
    VALIDATE CONSTRAINT pilot_attempt_backend_tool_call_count_nonnegative;
