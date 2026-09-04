-- HelixGrid durable storage design.
--
-- The first coordinator implementation intentionally uses an in-memory store so the
-- concurrency/state-machine logic stays easy to inspect. This schema documents a
-- production-shaped persistence model that preserves the same invariants under
-- transactions and multiple coordinator replicas.

BEGIN;

CREATE TYPE workflow_state AS ENUM (
    'PENDING',
    'RUNNING',
    'SUCCEEDED',
    'FAILED',
    'CANCELLED'
);

CREATE TYPE task_state AS ENUM (
    'PENDING',
    'READY',
    'LEASED',
    'RUNNING',
    'SUCCEEDED',
    'FAILED',
    'RETRY_WAIT',
    'CANCELLED'
);

CREATE TYPE event_type AS ENUM (
    'workflow.created',
    'workflow.started',
    'workflow.succeeded',
    'workflow.failed',
    'workflow.cancelled',
    'task.ready',
    'task.leased',
    'task.started',
    'task.log',
    'task.succeeded',
    'task.failed',
    'task.retry',
    'task.cancelled',
    'lease.expired',
    'worker.registered',
    'worker.heartbeat'
);

CREATE TABLE workflows (
    id              text PRIMARY KEY,
    name            text NOT NULL CHECK (length(btrim(name)) > 0),
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    state           workflow_state NOT NULL DEFAULT 'PENDING',
    idempotency_key text UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at     timestamptz,
    version         bigint NOT NULL DEFAULT 0,
    CONSTRAINT workflow_terminal_finished_ck CHECK (
        (state IN ('SUCCEEDED', 'FAILED', 'CANCELLED') AND finished_at IS NOT NULL)
        OR
        (state IN ('PENDING', 'RUNNING') AND finished_at IS NULL)
    )
);

CREATE TABLE tasks (
    workflow_id       text NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    task_id           text NOT NULL,
    ordinal           integer NOT NULL CHECK (ordinal >= 0),
    command           jsonb NOT NULL CHECK (
        jsonb_typeof(command) = 'array' AND jsonb_array_length(command) > 0
    ),
    env               jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(env) = 'object'),
    labels            jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(labels) = 'object'),
    timeout_seconds   integer NOT NULL DEFAULT 0 CHECK (timeout_seconds BETWEEN 0 AND 86400),
    max_attempts      integer NOT NULL DEFAULT 1 CHECK (max_attempts BETWEEN 1 AND 100),
    base_delay_ms     integer NOT NULL DEFAULT 250 CHECK (base_delay_ms BETWEEN 1 AND 3600000),
    max_delay_ms      integer NOT NULL DEFAULT 30000 CHECK (
        max_delay_ms BETWEEN base_delay_ms AND 86400000
    ),
    state             task_state NOT NULL DEFAULT 'PENDING',
    attempt           integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    next_retry_at     timestamptz,
    started_at        timestamptz,
    finished_at       timestamptz,
    exit_code         integer,
    error             text,
    output_bytes      bigint NOT NULL DEFAULT 0 CHECK (output_bytes >= 0),
    version           bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_id, task_id),
    UNIQUE (workflow_id, ordinal)
);

CREATE TABLE task_dependencies (
    workflow_id         text NOT NULL,
    task_id             text NOT NULL,
    depends_on_task_id  text NOT NULL,
    PRIMARY KEY (workflow_id, task_id, depends_on_task_id),
    FOREIGN KEY (workflow_id, task_id)
        REFERENCES tasks(workflow_id, task_id)
        ON DELETE CASCADE,
    FOREIGN KEY (workflow_id, depends_on_task_id)
        REFERENCES tasks(workflow_id, task_id)
        ON DELETE CASCADE,
    CHECK (task_id <> depends_on_task_id)
);

CREATE TABLE workers (
    id                  text PRIMARY KEY,
    name                text NOT NULL CHECK (length(btrim(name)) > 0),
    version             text NOT NULL CHECK (length(btrim(version)) > 0),
    labels              jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(labels) = 'object'),
    capacity            integer NOT NULL CHECK (capacity BETWEEN 1 AND 256),
    active_leases       integer NOT NULL DEFAULT 0 CHECK (active_leases >= 0),
    registered_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_heartbeat      timestamptz NOT NULL DEFAULT clock_timestamp(),
    generation          bigint NOT NULL DEFAULT 0
);

CREATE TABLE task_leases (
    token               text PRIMARY KEY,
    workflow_id         text NOT NULL,
    task_id             text NOT NULL,
    worker_id           text NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
    attempt             integer NOT NULL CHECK (attempt >= 1),
    acquired_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at          timestamptz NOT NULL,
    last_renewed_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (workflow_id, task_id)
        REFERENCES tasks(workflow_id, task_id)
        ON DELETE CASCADE,
    UNIQUE (workflow_id, task_id),
    CHECK (expires_at > acquired_at)
);

CREATE TABLE workflow_events (
    sequence            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type          event_type NOT NULL,
    workflow_id         text,
    task_id             text,
    worker_id           text,
    occurred_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    data                jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE task_log_chunks (
    sequence            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_id         text NOT NULL,
    task_id             text NOT NULL,
    attempt             integer NOT NULL,
    stream              text NOT NULL CHECK (stream IN ('stdout', 'stderr')),
    payload             text NOT NULL,
    byte_length         integer NOT NULL CHECK (byte_length >= 0),
    occurred_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (workflow_id, task_id)
        REFERENCES tasks(workflow_id, task_id)
        ON DELETE CASCADE
);

CREATE INDEX workflows_state_created_idx
    ON workflows (state, created_at, id);

CREATE INDEX tasks_runnable_idx
    ON tasks (state, next_retry_at, workflow_id, ordinal)
    WHERE state IN ('READY', 'RETRY_WAIT');

CREATE INDEX workers_heartbeat_idx
    ON workers (last_heartbeat DESC);

CREATE INDEX leases_expiration_idx
    ON task_leases (expires_at);

CREATE INDEX events_workflow_sequence_idx
    ON workflow_events (workflow_id, sequence)
    WHERE workflow_id IS NOT NULL;

CREATE INDEX task_logs_lookup_idx
    ON task_log_chunks (workflow_id, task_id, attempt, sequence);

-- Notify SSE listeners after the transaction containing an event commits.
CREATE OR REPLACE FUNCTION notify_helix_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_notify(
        'helix_events',
        json_build_object(
            'sequence', NEW.sequence,
            'workflow_id', NEW.workflow_id,
            'event_type', NEW.event_type
        )::text
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER workflow_events_notify
AFTER INSERT ON workflow_events
FOR EACH ROW
EXECUTE FUNCTION notify_helix_event();

-- Optimistic locking helper. Storage adapters increment version on every mutation.
CREATE OR REPLACE FUNCTION touch_workflow_row()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    NEW.version := OLD.version + 1;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workflow_touch
BEFORE UPDATE ON workflows
FOR EACH ROW
EXECUTE FUNCTION touch_workflow_row();

-- Claim one READY task while allowing multiple scheduler replicas to compete without
-- serializing the whole table. Dependency satisfaction is rechecked under the same
-- transaction and the selected task row is locked with SKIP LOCKED.
CREATE OR REPLACE FUNCTION claim_runnable_task(
    p_worker_id text,
    p_lease_token text,
    p_lease_seconds integer DEFAULT 20
)
RETURNS TABLE (
    workflow_id text,
    task_id text,
    attempt integer,
    expires_at timestamptz
)
LANGUAGE plpgsql
AS $$
DECLARE
    candidate record;
    worker_capacity integer;
    worker_active integer;
    worker_labels jsonb;
BEGIN
    IF p_lease_seconds <= 0 THEN
        RAISE EXCEPTION 'lease duration must be positive';
    END IF;

    SELECT capacity, active_leases, labels
      INTO worker_capacity, worker_active, worker_labels
      FROM workers
     WHERE id = p_worker_id
       AND last_heartbeat > clock_timestamp() - interval '45 seconds'
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'worker not found or heartbeat expired';
    END IF;

    IF worker_active >= worker_capacity THEN
        RETURN;
    END IF;

    SELECT t.workflow_id, t.task_id, t.attempt
      INTO candidate
      FROM tasks t
      JOIN workflows w ON w.id = t.workflow_id
     WHERE t.state = 'READY'
       AND w.state IN ('PENDING', 'RUNNING')
       AND t.labels <@ worker_labels
       AND NOT EXISTS (
            SELECT 1
              FROM task_dependencies d
              JOIN tasks dep
                ON dep.workflow_id = d.workflow_id
               AND dep.task_id = d.depends_on_task_id
             WHERE d.workflow_id = t.workflow_id
               AND d.task_id = t.task_id
               AND dep.state <> 'SUCCEEDED'
       )
     ORDER BY w.created_at, t.ordinal
     FOR UPDATE OF t SKIP LOCKED
     LIMIT 1;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE tasks
       SET state = 'LEASED',
           attempt = attempt + 1,
           version = version + 1
     WHERE tasks.workflow_id = candidate.workflow_id
       AND tasks.task_id = candidate.task_id;

    INSERT INTO task_leases(
        token, workflow_id, task_id, worker_id, attempt, acquired_at, expires_at
    ) VALUES (
        p_lease_token,
        candidate.workflow_id,
        candidate.task_id,
        p_worker_id,
        candidate.attempt + 1,
        clock_timestamp(),
        clock_timestamp() + make_interval(secs => p_lease_seconds)
    );

    UPDATE workers
       SET active_leases = active_leases + 1
     WHERE id = p_worker_id;

    INSERT INTO workflow_events(event_type, workflow_id, task_id, worker_id, data)
    VALUES (
        'task.leased',
        candidate.workflow_id,
        candidate.task_id,
        p_worker_id,
        jsonb_build_object('attempt', candidate.attempt + 1)
    );

    RETURN QUERY
    SELECT l.workflow_id, l.task_id, l.attempt, l.expires_at
      FROM task_leases l
     WHERE l.token = p_lease_token;
END;
$$;

-- Expired leases are recoverable. This operation uses row locks and is idempotent,
-- allowing every coordinator replica to run the sweeper periodically.
CREATE OR REPLACE FUNCTION recover_expired_leases(p_limit integer DEFAULT 1000)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    lease_row record;
    recovered integer := 0;
BEGIN
    FOR lease_row IN
        SELECT l.token, l.workflow_id, l.task_id, l.worker_id
          FROM task_leases l
         WHERE l.expires_at <= clock_timestamp()
         ORDER BY l.expires_at
         FOR UPDATE SKIP LOCKED
         LIMIT p_limit
    LOOP
        DELETE FROM task_leases WHERE token = lease_row.token;

        UPDATE tasks
           SET state = 'READY',
               started_at = NULL,
               version = version + 1
         WHERE tasks.workflow_id = lease_row.workflow_id
           AND tasks.task_id = lease_row.task_id
           AND state IN ('LEASED', 'RUNNING');

        UPDATE workers
           SET active_leases = GREATEST(active_leases - 1, 0)
         WHERE id = lease_row.worker_id;

        INSERT INTO workflow_events(event_type, workflow_id, task_id, worker_id)
        VALUES ('lease.expired', lease_row.workflow_id, lease_row.task_id, lease_row.worker_id);

        recovered := recovered + 1;
    END LOOP;

    RETURN recovered;
END;
$$;

COMMIT;
