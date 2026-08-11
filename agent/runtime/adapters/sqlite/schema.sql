-- The single current Runtime schema.  This project does not migrate databases:
-- a mismatching local database must be deleted and recreated by the operator.
CREATE TABLE schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version TEXT NOT NULL,
    schema_checksum TEXT NOT NULL,
    created_at INTEGER NOT NULL
) STRICT;

CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    next_turn_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_turn_seq >= 1),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE release_manifests (
    release_fingerprint TEXT PRIMARY KEY,
    engine TEXT NOT NULL CHECK (engine IN ('plan_execute','agent_loop','native_loop')),
    manifest_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at INTEGER NOT NULL
) STRICT;

CREATE TABLE active_releases (
    engine TEXT PRIMARY KEY CHECK (engine IN ('plan_execute','agent_loop','native_loop')),
    release_fingerprint TEXT NOT NULL REFERENCES release_manifests(release_fingerprint),
    activated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    client_request_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    turn_seq INTEGER NOT NULL CHECK (turn_seq >= 1),
    turn_id TEXT NOT NULL UNIQUE,
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    engine TEXT NOT NULL CHECK (engine IN ('plan_execute','agent_loop','native_loop')),
    deadline_at INTEGER NOT NULL,
    cancel_token_id TEXT NOT NULL UNIQUE,
    release_fingerprint TEXT NOT NULL REFERENCES release_manifests(release_fingerprint),
    input_event_id TEXT NOT NULL UNIQUE,
    attachment_refs_json TEXT NOT NULL,
    input_text TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
      'ACCEPTED','DISPATCH_PENDING','RUNNING','WAITING_RETRY','WAITING_INPUT',
      'CANCEL_REQUESTED','SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','REJECTED',
      'INCOMPATIBLE_RELEASE'
    )),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    next_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_seq >= 1),
    current_activity_id TEXT,
    terminal_status TEXT CHECK (terminal_status IS NULL OR terminal_status IN (
      'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','REJECTED','INCOMPATIBLE_RELEASE'
    )),
    terminal_payload_json TEXT,
    pending_input_json TEXT,
    trace_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    CHECK ((terminal_status IS NULL) = (terminal_payload_json IS NULL)),
    CHECK (terminal_status IS NULL OR state = terminal_status),
    UNIQUE (conversation_id, turn_seq)
) STRICT;

CREATE UNIQUE INDEX uq_active_run_per_conversation ON runs(conversation_id)
WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','REJECTED','INCOMPATIBLE_RELEASE');
CREATE INDEX ix_runs_status_created ON runs(state, created_at);

CREATE TABLE run_requests (
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
    created_at INTEGER NOT NULL,
    PRIMARY KEY (principal_id, agent_id, idempotency_key)
) WITHOUT ROWID, STRICT;

CREATE TABLE activities (
    activity_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    type TEXT NOT NULL CHECK (type IN (
      'ENGINE_RUN','MODEL_CALL','TOOL_CALL','RETRIEVAL','CHECKPOINT','WAIT_INPUT','FINALIZE'
    )),
    logical_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
      'PENDING','CLAIMED','RUNNING','SUCCEEDED','FAILED','CANCELLED',
      'WAITING_RETRY','WAITING_INPUT','RECONCILE','MANUAL'
    )),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    available_at INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at INTEGER,
    fencing_token INTEGER NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    result_json TEXT,
    error_json TEXT,
    pending_input_json TEXT,
    resume_payload_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (run_id, logical_key),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
) STRICT;
CREATE INDEX ix_activities_claim ON activities(state, available_at, created_at, activity_id);
CREATE INDEX ix_activities_lease ON activities(state, lease_expires_at);

CREATE TABLE run_events (
    event_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    turn_id TEXT NOT NULL,
    activity_id TEXT REFERENCES activities(activity_id),
    tool_execution_id TEXT,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    event_type TEXT NOT NULL,
    producer TEXT NOT NULL,
    payload_json TEXT,
    payload_ref TEXT,
    visibility TEXT NOT NULL CHECK (visibility IN ('PUBLIC','INTERNAL')),
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('PUBLIC','PRIVATE','SENSITIVE')),
    occurred_at INTEGER NOT NULL,
    terminal_status TEXT,
    release_fingerprint TEXT NOT NULL,
    UNIQUE (run_id, seq),
    CHECK (payload_json IS NULL OR payload_ref IS NULL)
) STRICT;
CREATE INDEX ix_run_events_replay ON run_events(run_id, seq);
CREATE UNIQUE INDEX uq_run_terminal_event ON run_events(run_id)
WHERE event_type = 'RUN_TERMINATED';

CREATE TRIGGER run_events_no_update
BEFORE UPDATE ON run_events BEGIN
  SELECT RAISE(ABORT, 'RUN_EVENTS_APPEND_ONLY');
END;
CREATE TRIGGER run_events_no_delete
BEFORE DELETE ON run_events BEGIN
  SELECT RAISE(ABORT, 'RUN_EVENTS_APPEND_ONLY');
END;

CREATE TABLE checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    activity_id TEXT NOT NULL REFERENCES activities(activity_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    working_state_json TEXT NOT NULL,
    engine_state_json TEXT,
    engine_state_ref TEXT,
    release_fingerprint TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (run_id, revision),
    CHECK (engine_state_json IS NULL OR engine_state_ref IS NULL)
) STRICT;
CREATE INDEX ix_checkpoints_latest ON checkpoints(run_id, revision DESC);

CREATE TABLE tool_executions (
    tool_execution_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    activity_id TEXT NOT NULL REFERENCES activities(activity_id),
    logical_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    release_digest TEXT NOT NULL,
    effect_class TEXT NOT NULL CHECK (effect_class IN (
      'READ_ONLY','IDEMPOTENT_EFFECT','NON_IDEMPOTENT_EFFECT','UNKNOWN_EFFECT'
    )),
    supports_reconcile INTEGER NOT NULL CHECK (supports_reconcile IN (0,1)),
    effect_status TEXT NOT NULL CHECK (effect_status IN (
      'PREPARED','DISPATCHED','COMMITTED','FAILED','UNKNOWN','RECONCILING','MANUAL_REQUIRED'
    )),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    dispatch_fencing_token INTEGER CHECK (
      dispatch_fencing_token IS NULL OR dispatch_fencing_token >= 0
    ),
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json TEXT,
    result_ref TEXT,
    error_json TEXT,
    external_object_id TEXT,
    reconcile_state TEXT,
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (run_id, logical_key)
) STRICT;
CREATE INDEX ix_tool_effect_reconcile ON tool_executions(effect_status, updated_at);

CREATE TABLE artifact_metadata (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    media_type TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    verified_at INTEGER
) STRICT;

CREATE TABLE artifact_links (
    link_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifact_metadata(artifact_id),
    run_id TEXT REFERENCES runs(run_id),
    activity_id TEXT REFERENCES activities(activity_id),
    event_id TEXT,
    relation TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('PUBLIC','PRIVATE','SENSITIVE')),
    retention_until INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE (artifact_id, run_id, activity_id, event_id, relation)
) STRICT;

CREATE TABLE signals (
    signal_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    wait_activity_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RECORDED','CONSUMED','REJECTED_LATE')),
    result_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    consumed_at INTEGER,
    PRIMARY KEY (run_id, signal_id)
) WITHOUT ROWID, STRICT;
CREATE INDEX ix_signals_run ON signals(run_id, created_at);

CREATE TABLE cancellation_commands (
    command_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    reason TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, command_id)
) WITHOUT ROWID, STRICT;

CREATE TABLE timers (
    timer_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    activity_id TEXT NOT NULL REFERENCES activities(activity_id),
    kind TEXT NOT NULL CHECK (kind IN ('RETRY','DEADLINE','WAIT_TIMEOUT')),
    fire_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('SCHEDULED','FIRED','CANCELLED')),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at INTEGER NOT NULL,
    fired_at INTEGER,
    UNIQUE (activity_id, kind, fire_at)
) STRICT;
CREATE INDEX ix_timers_due ON timers(state, fire_at);

CREATE TABLE runtime_workers (
    worker_id TEXT PRIMARY KEY,
    release_map_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE','DRAINING','STOPPED')),
    started_at INTEGER NOT NULL,
    heartbeat_at INTEGER NOT NULL
) STRICT;
