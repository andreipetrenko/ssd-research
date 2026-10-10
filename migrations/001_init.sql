-- Базовая схема Integration Service (раздел 5.1 технического проекта) с полями,
-- необходимыми для повторной обработки, трассировки и метрик.

CREATE SEQUENCE IF NOT EXISTS sdd_change_number_seq START WITH 1042;

CREATE TABLE sdd_change (
    change_id VARCHAR(64) PRIMARY KEY,
    source_issue VARCHAR(32) NOT NULL UNIQUE,
    title TEXT,
    stage VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sdd_change_stage_chk CHECK (stage IN (
        'idea', 'business_specification', 'system_analysis',
        'functional_specification', 'development', 'done'
    )),
    CONSTRAINT sdd_change_status_chk CHECK (status IN (
        'registered', 'in_progress', 'in_review', 'changes_requested', 'declined', 'done'
    ))
);

CREATE TABLE sdd_artifact (
    id BIGSERIAL PRIMARY KEY,
    change_id VARCHAR(64) NOT NULL REFERENCES sdd_change(change_id),
    artifact_type VARCHAR(32) NOT NULL,
    repository VARCHAR(255) NOT NULL,
    branch_name VARCHAR(255),
    pull_request_id BIGINT,
    pull_request_url TEXT,
    pull_request_state VARCHAR(16),
    commit_sha VARCHAR(64),
    merge_commit_sha VARCHAR(64),
    jira_key VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sdd_artifact_type_chk CHECK (artifact_type IN (
        'business_specification', 'functional_specification', 'implementation'
    )),
    CONSTRAINT sdd_artifact_pr_uq UNIQUE (repository, pull_request_id)
);
CREATE INDEX sdd_artifact_change_idx ON sdd_artifact (change_id);

CREATE TABLE sdd_issue_link (
    id BIGSERIAL PRIMARY KEY,
    change_id VARCHAR(64) NOT NULL REFERENCES sdd_change(change_id),
    issue_key VARCHAR(32) NOT NULL UNIQUE,
    issue_type VARCHAR(32) NOT NULL,
    parent_issue VARCHAR(32),
    stage VARCHAR(32) NOT NULL,
    operation_key VARCHAR(255) NOT NULL UNIQUE,
    spec_ref VARCHAR(64),
    summary TEXT,
    state VARCHAR(16) NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sdd_issue_link_state_chk CHECK (state IN ('open', 'done'))
);
CREATE INDEX sdd_issue_link_change_idx ON sdd_issue_link (change_id, stage);

CREATE TABLE sdd_event (
    id BIGSERIAL PRIMARY KEY,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    event_type VARCHAR(64) NOT NULL,
    change_id VARCHAR(64),
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,
    delivery_id VARCHAR(128),
    payload JSONB NOT NULL,
    result JSONB,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    source_event_at TIMESTAMPTZ,
    locked_at TIMESTAMPTZ,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    CONSTRAINT sdd_event_status_chk CHECK (status IN (
        'received', 'processing', 'done', 'failed', 'dead', 'rejected', 'ignored'
    ))
);
CREATE INDEX sdd_event_retry_idx ON sdd_event (status, next_retry_at);
CREATE INDEX sdd_event_change_idx ON sdd_event (change_id);
