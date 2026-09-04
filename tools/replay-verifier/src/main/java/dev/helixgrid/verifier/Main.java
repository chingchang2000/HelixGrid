package dev.helixgrid.verifier;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Deque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.TreeMap;

/**
 * HelixGrid event replay and invariant verifier.
 *
 * <p>Reads JSONL event streams exported from the coordinator and reconstructs workflow,
 * task, worker and lease state without trusting the final snapshot. It is useful for
 * debugging race conditions and for proving that important distributed-system invariants
 * were preserved by a historical execution.</p>
 */
public final class Main {
    private Main() {}

    enum Severity {
        INFO,
        WARNING,
        ERROR
    }

    enum WorkflowState {
        UNKNOWN,
        PENDING,
        RUNNING,
        SUCCEEDED,
        FAILED,
        CANCELLED;

        boolean terminal() {
            return this == SUCCEEDED || this == FAILED || this == CANCELLED;
        }
    }

    enum TaskState {
        UNKNOWN,
        PENDING,
        READY,
        LEASED,
        RUNNING,
        RETRY_WAIT,
        SUCCEEDED,
        FAILED,
        CANCELLED;

        boolean terminal() {
            return this == SUCCEEDED || this == FAILED || this == CANCELLED;
        }
    }

    record Finding(
            Severity severity,
            long sequence,
            String code,
            String workflowId,
            String taskId,
            String message) {}

    record Event(
            long sequence,
            String type,
            String workflowId,
            String taskId,
            String workerId,
            Instant at,
            Map<String, String> data,
            int sourceLine) {}

    static final class LeaseState {
        String workerId;
        int attempt;
        long acquiredSequence;
        boolean started;
    }

    static final class TaskReplay {
        final String id;
        TaskState state = TaskState.UNKNOWN;
        int attempt;
        long lastSequence;
        Instant firstSeen;
        Instant startedAt;
        Instant finishedAt;
        String lastWorkerId = "";
        LeaseState lease;
        int leaseCount;
        int expiredLeaseCount;
        int failures;
        int retries;
        int logEvents;
        long logBytes;
        final List<TaskState> history = new ArrayList<>();

        TaskReplay(String id) {
            this.id = id;
        }

        void transition(TaskState next, Event event, Verifier verifier) {
            if (state == next) {
                verifier.warn(event, "TASK_DUPLICATE_STATE", "task repeated state " + next);
                lastSequence = event.sequence();
                return;
            }
            if (!transitionAllowed(state, next)) {
                verifier.error(
                        event,
                        "TASK_INVALID_TRANSITION",
                        "invalid replay transition " + state + " -> " + next);
            }
            state = next;
            history.add(next);
            lastSequence = event.sequence();
            if (firstSeen == null) firstSeen = event.at();
            if (next == TaskState.RUNNING && startedAt == null) startedAt = event.at();
            if (next.terminal()) finishedAt = event.at();
        }

        private static boolean transitionAllowed(TaskState from, TaskState to) {
            if (from == TaskState.UNKNOWN) {
                return to == TaskState.PENDING
                        || to == TaskState.READY
                        || to == TaskState.LEASED
                        || to == TaskState.RUNNING
                        || to.terminal();
            }
            return switch (from) {
                case PENDING -> to == TaskState.READY || to == TaskState.CANCELLED;
                case READY -> to == TaskState.LEASED || to == TaskState.CANCELLED;
                case LEASED -> to == TaskState.RUNNING || to == TaskState.READY || to == TaskState.CANCELLED;
                case RUNNING -> to == TaskState.SUCCEEDED
                        || to == TaskState.FAILED
                        || to == TaskState.RETRY_WAIT
                        || to == TaskState.READY
                        || to == TaskState.CANCELLED;
                case RETRY_WAIT -> to == TaskState.READY || to == TaskState.CANCELLED;
                case SUCCEEDED, FAILED, CANCELLED -> false;
                case UNKNOWN -> true;
            };
        }
    }

    static final class WorkflowReplay {
        final String id;
        String name = "";
        WorkflowState state = WorkflowState.UNKNOWN;
        long createdSequence;
        long lastSequence;
        Instant createdAt;
        Instant finishedAt;
        final Map<String, TaskReplay> tasks = new LinkedHashMap<>();
        int events;
        int logEvents;
        long logBytes;

        WorkflowReplay(String id) {
            this.id = id;
        }

        TaskReplay task(String id) {
            return tasks.computeIfAbsent(id, TaskReplay::new);
        }
    }

    static final class WorkerReplay {
        final String id;
        String name = "";
        Instant registeredAt;
        Instant lastHeartbeat;
        int leasesAcquired;
        int leasesExpired;
        int starts;
        int completions;
        final Set<String> activeTasks = new LinkedHashSet<>();

        WorkerReplay(String id) {
            this.id = id;
        }
    }

    static final class Options {
        Path input;
        boolean json;
        boolean strict;
        boolean summaryOnly;
        boolean allowGaps;
        long afterSequence;
        String workflowFilter = "";
        int maxFindings = 10_000;
    }

    static final class Verifier {
        final Options options;
        final Map<String, WorkflowReplay> workflows = new LinkedHashMap<>();
        final Map<String, WorkerReplay> workers = new LinkedHashMap<>();
        final List<Finding> findings = new ArrayList<>();
        final Map<String, Long> counters = new TreeMap<>();
        final Set<Long> observedSequences = new HashSet<>();
        long previousSequence;
        Instant previousTimestamp;
        int inputLines;
        int parsedEvents;
        int ignoredEvents;

        Verifier(Options options) {
            this.options = options;
        }

        void accept(Event event) {
            parsedEvents++;
            increment("events.total");
            increment("events." + event.type());

            if (!observedSequences.add(event.sequence())) {
                error(event, "DUPLICATE_SEQUENCE", "event sequence appears more than once");
            }
            if (previousSequence != 0 && event.sequence() <= previousSequence) {
                error(event, "NON_MONOTONIC_SEQUENCE", "event sequence moved backwards or repeated");
            }
            if (!options.allowGaps && previousSequence != 0 && event.sequence() != previousSequence + 1) {
                warn(
                        event,
                        "SEQUENCE_GAP",
                        "expected sequence " + (previousSequence + 1) + " but got " + event.sequence());
            }
            if (previousTimestamp != null && event.at().isBefore(previousTimestamp.minus(Duration.ofSeconds(5)))) {
                warn(event, "CLOCK_MOVED_BACKWARDS", "event timestamp moved backwards by more than five seconds");
            }
            previousSequence = event.sequence();
            previousTimestamp = event.at();

            if (!options.workflowFilter.isEmpty()
                    && !options.workflowFilter.equals(event.workflowId())
                    && !event.workflowId().isEmpty()) {
                ignoredEvents++;
                return;
            }

            if (!event.workflowId().isEmpty()) {
                workflow(event.workflowId()).events++;
            }

            switch (event.type()) {
                case "workflow.created" -> workflowCreated(event);
                case "workflow.started" -> workflowStarted(event);
                case "workflow.succeeded" -> workflowTerminal(event, WorkflowState.SUCCEEDED);
                case "workflow.failed" -> workflowTerminal(event, WorkflowState.FAILED);
                case "workflow.cancelled" -> workflowTerminal(event, WorkflowState.CANCELLED);
                case "task.ready" -> taskState(event, TaskState.READY);
                case "task.leased" -> taskLeased(event);
                case "task.started" -> taskStarted(event);
                case "task.log" -> taskLog(event);
                case "task.succeeded" -> taskCompleted(event, TaskState.SUCCEEDED);
                case "task.failed" -> taskCompleted(event, TaskState.FAILED);
                case "task.retry" -> taskRetry(event);
                case "task.cancelled" -> taskCompleted(event, TaskState.CANCELLED);
                case "lease.expired" -> leaseExpired(event);
                case "worker.registered" -> workerRegistered(event);
                case "worker.heartbeat" -> workerHeartbeat(event);
                default -> info(event, "UNKNOWN_EVENT_TYPE", "unknown event type retained but not interpreted: " + event.type());
            }
        }

        private WorkflowReplay workflow(String id) {
            return workflows.computeIfAbsent(id, WorkflowReplay::new);
        }

        private WorkerReplay worker(String id) {
            return workers.computeIfAbsent(id, WorkerReplay::new);
        }

        private TaskReplay requireTask(Event event) {
            if (event.workflowId().isEmpty()) {
                error(event, "TASK_EVENT_WITHOUT_WORKFLOW", "task event has no workflow_id");
            }
            if (event.taskId().isEmpty()) {
                error(event, "TASK_EVENT_WITHOUT_TASK", "task event has no task_id");
            }
            return workflow(event.workflowId()).task(event.taskId());
        }

        private void workflowCreated(Event event) {
            var workflow = workflow(event.workflowId());
            if (workflow.createdSequence != 0) {
                error(event, "WORKFLOW_CREATED_TWICE", "workflow already had a creation event");
                return;
            }
            workflow.createdSequence = event.sequence();
            workflow.createdAt = event.at();
            workflow.lastSequence = event.sequence();
            workflow.name = event.data().getOrDefault("name", "");
            workflow.state = WorkflowState.PENDING;
        }

        private void workflowStarted(Event event) {
            var workflow = workflow(event.workflowId());
            if (workflow.state.terminal()) {
                error(event, "WORKFLOW_RESTARTED_AFTER_TERMINAL", "terminal workflow emitted workflow.started");
            }
            if (workflow.state == WorkflowState.UNKNOWN) {
                warn(event, "WORKFLOW_STARTED_BEFORE_CREATED", "workflow.started observed before workflow.created");
            }
            workflow.state = WorkflowState.RUNNING;
            workflow.lastSequence = event.sequence();
        }

        private void workflowTerminal(Event event, WorkflowState terminal) {
            var workflow = workflow(event.workflowId());
            if (workflow.state.terminal() && workflow.state != terminal) {
                error(
                        event,
                        "WORKFLOW_TERMINAL_STATE_CHANGED",
                        "workflow changed terminal state from " + workflow.state + " to " + terminal);
            } else if (workflow.state == terminal) {
                warn(event, "WORKFLOW_TERMINAL_DUPLICATE", "duplicate terminal workflow event " + terminal);
            }

            // Coordinator cancellation is authoritative and releases all active leases.
            // Older event streams may not contain one task.cancelled event per task, so
            // replay cancellation semantics here as well to avoid false invariant errors.
            if (terminal == WorkflowState.CANCELLED) {
                for (var task : workflow.tasks.values()) {
                    if (task.lease != null) {
                        releaseLease(event, task, false);
                    }
                    if (!task.state.terminal()) {
                        task.transition(TaskState.CANCELLED, event, this);
                    }
                }
            }

            workflow.state = terminal;
            workflow.finishedAt = event.at();
            workflow.lastSequence = event.sequence();
        }

        private void taskState(Event event, TaskState state) {
            var task = requireTask(event);
            if (state == TaskState.READY && task.lease != null) {
                error(event, "TASK_READY_WITH_ACTIVE_LEASE", "task became READY while a lease was still active");
            }
            task.transition(state, event, this);
        }

        private void taskLeased(Event event) {
            var task = requireTask(event);
            if (task.lease != null) {
                error(
                        event,
                        "OVERLAPPING_TASK_LEASE",
                        "task received a new lease while worker " + task.lease.workerId + " still owns one");
            }
            if (event.workerId().isEmpty()) {
                error(event, "LEASE_WITHOUT_WORKER", "task.leased event has no worker_id");
            }
            int attempt = parseInt(event.data().get("attempt"), task.attempt + 1, event, "attempt");
            if (attempt <= task.attempt) {
                error(
                        event,
                        "ATTEMPT_NOT_INCREASING",
                        "lease attempt " + attempt + " is not greater than previous attempt " + task.attempt);
            }
            task.attempt = Math.max(task.attempt, attempt);
            task.leaseCount++;
            task.lastWorkerId = event.workerId();
            var lease = new LeaseState();
            lease.workerId = event.workerId();
            lease.attempt = attempt;
            lease.acquiredSequence = event.sequence();
            task.lease = lease;
            task.transition(TaskState.LEASED, event, this);

            if (!event.workerId().isEmpty()) {
                var worker = worker(event.workerId());
                worker.leasesAcquired++;
                String key = event.workflowId() + "/" + event.taskId();
                if (!worker.activeTasks.add(key)) {
                    error(event, "WORKER_DUPLICATE_ACTIVE_TASK", "worker already tracked task as active");
                }
            }
        }

        private void taskStarted(Event event) {
            var task = requireTask(event);
            if (task.lease == null) {
                error(event, "TASK_STARTED_WITHOUT_LEASE", "task.started observed without an active lease");
            } else {
                if (!event.workerId().isEmpty() && !event.workerId().equals(task.lease.workerId)) {
                    error(
                            event,
                            "LEASE_OWNER_MISMATCH",
                            "task started by " + event.workerId() + " but lease belongs to " + task.lease.workerId);
                }
                task.lease.started = true;
            }
            task.lastWorkerId = event.workerId();
            task.transition(TaskState.RUNNING, event, this);
            if (!event.workerId().isEmpty()) worker(event.workerId()).starts++;
        }

        private void taskLog(Event event) {
            var task = requireTask(event);
            if (task.state != TaskState.RUNNING && task.state != TaskState.LEASED) {
                warn(event, "LOG_OUTSIDE_ACTIVE_TASK", "task log observed while task state is " + task.state);
            }
            String stream = event.data().getOrDefault("stream", "");
            if (!stream.equals("stdout") && !stream.equals("stderr")) {
                warn(event, "UNKNOWN_LOG_STREAM", "unexpected log stream: " + stream);
            }
            String text = event.data().getOrDefault("text", "");
            long bytes = text.getBytes(StandardCharsets.UTF_8).length;
            task.logEvents++;
            task.logBytes += bytes;
            var workflow = workflow(event.workflowId());
            workflow.logEvents++;
            workflow.logBytes += bytes;
        }

        private void taskCompleted(Event event, TaskState terminal) {
            var task = requireTask(event);
            if (terminal == TaskState.FAILED) task.failures++;
            if (task.lease == null) {
                error(event, "TASK_COMPLETED_WITHOUT_LEASE", "terminal task event has no active lease");
            } else if (!event.workerId().isEmpty() && !event.workerId().equals(task.lease.workerId)) {
                error(
                        event,
                        "COMPLETION_WORKER_MISMATCH",
                        "completion worker " + event.workerId() + " differs from lease owner " + task.lease.workerId);
            }
            releaseLease(event, task, false);
            task.transition(terminal, event, this);
            if (!event.workerId().isEmpty()) worker(event.workerId()).completions++;
        }

        private void taskRetry(Event event) {
            var task = requireTask(event);
            task.failures++;
            task.retries++;
            if (task.lease == null) {
                error(event, "RETRY_WITHOUT_ACTIVE_LEASE", "retry event observed without lease context");
            }
            releaseLease(event, task, false);
            task.transition(TaskState.RETRY_WAIT, event, this);
        }

        private void leaseExpired(Event event) {
            var task = requireTask(event);
            task.expiredLeaseCount++;
            if (task.lease == null) {
                warn(event, "EXPIRED_UNKNOWN_LEASE", "lease.expired observed without active replay lease");
            } else if (!event.workerId().isEmpty() && !event.workerId().equals(task.lease.workerId)) {
                error(
                        event,
                        "EXPIRED_LEASE_WORKER_MISMATCH",
                        "expiry worker " + event.workerId() + " differs from owner " + task.lease.workerId);
            }
            releaseLease(event, task, true);
            task.transition(TaskState.READY, event, this);
        }

        private void releaseLease(Event event, TaskReplay task, boolean expiry) {
            String workerId = task.lease != null ? task.lease.workerId : event.workerId();
            task.lease = null;
            if (!workerId.isEmpty()) {
                var worker = worker(workerId);
                worker.activeTasks.remove(event.workflowId() + "/" + event.taskId());
                if (expiry) worker.leasesExpired++;
            }
        }

        private void workerRegistered(Event event) {
            if (event.workerId().isEmpty()) {
                error(event, "WORKER_EVENT_WITHOUT_ID", "worker.registered has no worker_id");
                return;
            }
            var worker = worker(event.workerId());
            if (worker.registeredAt != null) {
                warn(event, "WORKER_REGISTERED_TWICE", "worker id was registered more than once");
            }
            worker.registeredAt = event.at();
            worker.lastHeartbeat = event.at();
            worker.name = event.data().getOrDefault("name", "");
        }

        private void workerHeartbeat(Event event) {
            if (event.workerId().isEmpty()) {
                error(event, "HEARTBEAT_WITHOUT_WORKER", "worker.heartbeat has no worker_id");
                return;
            }
            var worker = worker(event.workerId());
            if (worker.registeredAt == null) {
                warn(event, "HEARTBEAT_BEFORE_REGISTRATION", "heartbeat observed before worker.registered");
            }
            if (worker.lastHeartbeat != null && event.at().isBefore(worker.lastHeartbeat)) {
                warn(event, "WORKER_HEARTBEAT_BACKWARDS", "worker heartbeat timestamp moved backwards");
            }
            worker.lastHeartbeat = event.at();
        }

        void finish() {
            for (var workflow : workflows.values()) {
                if (workflow.createdSequence == 0) {
                    synthetic(
                            Severity.WARNING,
                            workflow.lastSequence,
                            "WORKFLOW_NEVER_CREATED",
                            workflow.id,
                            "",
                            "workflow appeared in events but never emitted workflow.created");
                }
                if (workflow.state == WorkflowState.SUCCEEDED) {
                    for (var task : workflow.tasks.values()) {
                        if (task.state != TaskState.SUCCEEDED) {
                            synthetic(
                                    Severity.ERROR,
                                    workflow.lastSequence,
                                    "SUCCEEDED_WORKFLOW_HAS_INCOMPLETE_TASK",
                                    workflow.id,
                                    task.id,
                                    "workflow succeeded while replay task state is " + task.state);
                        }
                    }
                }
                if (workflow.state == WorkflowState.CANCELLED) {
                    for (var task : workflow.tasks.values()) {
                        if (task.lease != null) {
                            synthetic(
                                    Severity.ERROR,
                                    workflow.lastSequence,
                                    "CANCELLED_WORKFLOW_HAS_ACTIVE_LEASE",
                                    workflow.id,
                                    task.id,
                                    "cancelled workflow retained an active lease");
                        }
                    }
                }
                for (var task : workflow.tasks.values()) {
                    if (task.lease != null) {
                        synthetic(
                                Severity.WARNING,
                                task.lastSequence,
                                "TRACE_ENDS_WITH_ACTIVE_LEASE",
                                workflow.id,
                                task.id,
                                "event stream ended while lease owned by " + task.lease.workerId + " remained active");
                    }
                    if (task.startedAt != null && task.finishedAt != null && task.finishedAt.isBefore(task.startedAt)) {
                        synthetic(
                                Severity.ERROR,
                                task.lastSequence,
                                "TASK_FINISHED_BEFORE_STARTED",
                                workflow.id,
                                task.id,
                                "task finish timestamp is earlier than start timestamp");
                    }
                }
            }
            for (var worker : workers.values()) {
                if (!worker.activeTasks.isEmpty()) {
                    synthetic(
                            Severity.WARNING,
                            previousSequence,
                            "TRACE_ENDS_WITH_WORKER_ACTIVITY",
                            "",
                            "",
                            "worker " + worker.id + " still owns " + worker.activeTasks.size() + " replay task(s)");
                }
            }
        }

        private int parseInt(String raw, int fallback, Event event, String field) {
            if (raw == null || raw.isBlank()) return fallback;
            try {
                return Integer.parseInt(raw);
            } catch (NumberFormatException ex) {
                warn(event, "INVALID_EVENT_NUMBER", "cannot parse " + field + "=" + raw);
                return fallback;
            }
        }

        private void increment(String key) {
            counters.merge(key, 1L, Long::sum);
        }

        void info(Event event, String code, String message) {
            finding(Severity.INFO, event, code, message);
        }

        void warn(Event event, String code, String message) {
            finding(Severity.WARNING, event, code, message);
        }

        void error(Event event, String code, String message) {
            finding(Severity.ERROR, event, code, message);
        }

        private void finding(Severity severity, Event event, String code, String message) {
            synthetic(severity, event.sequence(), code, event.workflowId(), event.taskId(), message);
        }

        private void synthetic(
                Severity severity,
                long sequence,
                String code,
                String workflowId,
                String taskId,
                String message) {
            increment("findings." + severity.name().toLowerCase(Locale.ROOT));
            if (findings.size() < options.maxFindings) {
                findings.add(new Finding(severity, sequence, code, workflowId, taskId, message));
            }
        }

        long errors() {
            return counters.getOrDefault("findings.error", 0L);
        }

        long warnings() {
            return counters.getOrDefault("findings.warning", 0L);
        }
    }

    public static void main(String[] args) {
        Options options;
        try {
            options = parseArgs(args);
        } catch (IllegalArgumentException ex) {
            System.err.println("helix-replay-verifier: " + ex.getMessage());
            printUsage();
            System.exit(2);
            return;
        }

        var verifier = new Verifier(options);
        try {
            readEvents(options, verifier);
            verifier.finish();
        } catch (IOException | Json.JsonException ex) {
            System.err.println("helix-replay-verifier: " + ex.getMessage());
            System.exit(2);
            return;
        }

        if (options.json) printJson(verifier);
        else printHuman(verifier);

        boolean failed = verifier.errors() > 0 || (options.strict && verifier.warnings() > 0);
        System.exit(failed ? 1 : 0);
    }

    static Options parseArgs(String[] args) {
        var options = new Options();
        for (int i = 0; i < args.length; i++) {
            String arg = args[i];
            switch (arg) {
                case "--input", "-i" -> options.input = Path.of(requireValue(args, ++i, arg));
                case "--json" -> options.json = true;
                case "--strict" -> options.strict = true;
                case "--summary-only" -> options.summaryOnly = true;
                case "--allow-gaps" -> options.allowGaps = true;
                case "--after" -> options.afterSequence = parseLong(requireValue(args, ++i, arg), arg);
                case "--workflow" -> options.workflowFilter = requireValue(args, ++i, arg);
                case "--max-findings" -> {
                    long parsed = parseLong(requireValue(args, ++i, arg), arg);
                    if (parsed < 0 || parsed > 1_000_000) throw new IllegalArgumentException("--max-findings must be 0..1000000");
                    options.maxFindings = (int) parsed;
                }
                case "--help", "-h" -> {
                    printUsage();
                    System.exit(0);
                }
                default -> {
                    if (arg.startsWith("-")) throw new IllegalArgumentException("unknown option: " + arg);
                    if (options.input != null) throw new IllegalArgumentException("multiple input files supplied");
                    options.input = Path.of(arg);
                }
            }
        }
        return options;
    }

    static String requireValue(String[] args, int index, String flag) {
        if (index >= args.length) throw new IllegalArgumentException("missing value for " + flag);
        return args[index];
    }

    static long parseLong(String raw, String flag) {
        try {
            return Long.parseLong(raw);
        } catch (NumberFormatException ex) {
            throw new IllegalArgumentException("invalid integer for " + flag + ": " + raw);
        }
    }

    static void printUsage() {
        System.out.println("""
                HelixGrid replay verifier

                Usage:
                  java dev.helixgrid.verifier.Main [events.jsonl] [options]

                Options:
                  -i, --input FILE       Read JSONL from FILE instead of stdin
                      --workflow ID       Only interpret one workflow (global sequence checks remain)
                      --after N           Ignore events with sequence <= N
                      --allow-gaps        Do not warn about sequence gaps
                      --strict            Treat warnings as verification failure
                      --summary-only      Suppress individual finding rows in human output
                      --max-findings N    Retain at most N finding details (default 10000)
                      --json              Emit machine-readable JSON report
                  -h, --help              Show this help

                Input format:
                  One coordinator event JSON object per line. Blank lines and lines beginning
                  with # are ignored. Both `id` and `sequence` are accepted as sequence fields.
                """);
    }

    static void readEvents(Options options, Verifier verifier) throws IOException {
        BufferedReader reader;
        boolean close;
        if (options.input != null) {
            reader = Files.newBufferedReader(options.input, StandardCharsets.UTF_8);
            close = true;
        } else {
            reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            close = false;
        }
        try {
            String line;
            int lineNumber = 0;
            while ((line = reader.readLine()) != null) {
                lineNumber++;
                verifier.inputLines++;
                String trimmed = line.trim();
                if (trimmed.isEmpty() || trimmed.startsWith("#")) continue;
                Json.Obj object;
                try {
                    object = Json.parse(trimmed).asObject();
                } catch (Json.JsonException ex) {
                    throw new Json.JsonException("line " + lineNumber + ": " + ex.getMessage(), ex);
                }
                Event event = decodeEvent(object, lineNumber);
                if (event.sequence() <= options.afterSequence) {
                    verifier.ignoredEvents++;
                    continue;
                }
                verifier.accept(event);
            }
        } finally {
            if (close) reader.close();
        }
    }

    static Event decodeEvent(Json.Obj object, int lineNumber) {
        long sequence = object.longValue("id", object.longValue("sequence", -1));
        if (sequence < 0) throw new Json.JsonException("line " + lineNumber + ": missing non-negative event id/sequence");
        String type = object.string("type", "");
        if (type.isBlank()) throw new Json.JsonException("line " + lineNumber + ": missing event type");
        String workflowId = object.string("workflow_id", "");
        String taskId = object.string("task_id", "");
        String workerId = object.string("worker_id", "");
        String rawAt = object.string("at", "");
        Instant at;
        if (rawAt.isBlank()) {
            at = Instant.EPOCH.plusMillis(sequence);
        } else {
            try {
                at = Instant.parse(rawAt);
            } catch (DateTimeParseException ex) {
                throw new Json.JsonException("line " + lineNumber + ": invalid RFC3339 timestamp: " + rawAt, ex);
            }
        }
        var data = new LinkedHashMap<String, String>();
        for (var entry : object.object("data").values().entrySet()) {
            Json.Value value = entry.getValue();
            String converted = switch (value) {
                case Json.Str string -> string.value();
                case Json.Num number -> number.value().toPlainString();
                case Json.Bool bool -> Boolean.toString(bool.value());
                case Json.Null ignored -> "";
                default -> Json.stringify(value);
            };
            data.put(entry.getKey(), converted);
        }
        return new Event(sequence, type, workflowId, taskId, workerId, at, Map.copyOf(data), lineNumber);
    }

    static void printHuman(Verifier verifier) {
        System.out.println("HelixGrid event replay verification");
        System.out.println("==========================================================================");
        System.out.printf("input lines       %d%n", verifier.inputLines);
        System.out.printf("parsed events     %d%n", verifier.parsedEvents);
        System.out.printf("ignored events    %d%n", verifier.ignoredEvents);
        System.out.printf("workflows         %d%n", verifier.workflows.size());
        System.out.printf("workers           %d%n", verifier.workers.size());
        System.out.printf("errors            %d%n", verifier.errors());
        System.out.printf("warnings          %d%n", verifier.warnings());
        System.out.println();

        if (!verifier.options.summaryOnly && !verifier.findings.isEmpty()) {
            System.out.println("Findings");
            System.out.println("--------------------------------------------------------------------------");
            for (var finding : verifier.findings) {
                String scope = "";
                if (!finding.workflowId().isEmpty()) scope += " workflow=" + finding.workflowId();
                if (!finding.taskId().isEmpty()) scope += " task=" + finding.taskId();
                System.out.printf(
                        "%-7s seq=%-8d %-34s%s  %s%n",
                        finding.severity(),
                        finding.sequence(),
                        finding.code(),
                        scope,
                        finding.message());
            }
            System.out.println();
        }

        if (!verifier.workflows.isEmpty()) {
            System.out.println("Workflow replay summary");
            System.out.println("--------------------------------------------------------------------------");
            var ordered = new ArrayList<>(verifier.workflows.values());
            ordered.sort(Comparator.comparingLong(w -> w.createdSequence == 0 ? Long.MAX_VALUE : w.createdSequence));
            for (var workflow : ordered) {
                long succeeded = workflow.tasks.values().stream().filter(t -> t.state == TaskState.SUCCEEDED).count();
                long failed = workflow.tasks.values().stream().filter(t -> t.state == TaskState.FAILED).count();
                long active = workflow.tasks.values().stream().filter(t -> !t.state.terminal()).count();
                System.out.printf(
                        "%-28s %-10s tasks=%-4d ok=%-4d failed=%-3d active=%-3d logs=%d/%s%n",
                        shorten(workflow.id, 28),
                        workflow.state,
                        workflow.tasks.size(),
                        succeeded,
                        failed,
                        active,
                        workflow.logEvents,
                        humanBytes(workflow.logBytes));
            }
            System.out.println();
        }

        if (!verifier.workers.isEmpty()) {
            System.out.println("Worker replay summary");
            System.out.println("--------------------------------------------------------------------------");
            for (var worker : verifier.workers.values()) {
                System.out.printf(
                        "%-28s leases=%-5d starts=%-5d completes=%-5d expired=%-4d active=%d%n",
                        shorten(worker.id, 28),
                        worker.leasesAcquired,
                        worker.starts,
                        worker.completions,
                        worker.leasesExpired,
                        worker.activeTasks.size());
            }
            System.out.println();
        }

        String result = verifier.errors() == 0 && (!verifier.options.strict || verifier.warnings() == 0)
                ? "PASS"
                : "FAIL";
        System.out.println("result: " + result);
    }

    static void printJson(Verifier verifier) {
        var root = new LinkedHashMap<String, Object>();
        root.put("input_lines", verifier.inputLines);
        root.put("parsed_events", verifier.parsedEvents);
        root.put("ignored_events", verifier.ignoredEvents);
        root.put("errors", verifier.errors());
        root.put("warnings", verifier.warnings());
        root.put("strict", verifier.options.strict);
        root.put("valid", verifier.errors() == 0 && (!verifier.options.strict || verifier.warnings() == 0));
        root.put("counters", verifier.counters);

        var findingValues = new ArrayList<Object>();
        for (var finding : verifier.findings) {
            var item = new LinkedHashMap<String, Object>();
            item.put("severity", finding.severity().name());
            item.put("sequence", finding.sequence());
            item.put("code", finding.code());
            item.put("workflow_id", finding.workflowId());
            item.put("task_id", finding.taskId());
            item.put("message", finding.message());
            findingValues.add(item);
        }
        root.put("findings", findingValues);

        var workflowValues = new ArrayList<Object>();
        for (var workflow : verifier.workflows.values()) {
            var item = new LinkedHashMap<String, Object>();
            item.put("id", workflow.id);
            item.put("name", workflow.name);
            item.put("state", workflow.state.name());
            item.put("events", workflow.events);
            item.put("task_count", workflow.tasks.size());
            item.put("log_events", workflow.logEvents);
            item.put("log_bytes", workflow.logBytes);
            long succeeded = workflow.tasks.values().stream().filter(t -> t.state == TaskState.SUCCEEDED).count();
            long failed = workflow.tasks.values().stream().filter(t -> t.state == TaskState.FAILED).count();
            item.put("succeeded_tasks", succeeded);
            item.put("failed_tasks", failed);
            workflowValues.add(item);
        }
        root.put("workflows", workflowValues);

        var workerValues = new ArrayList<Object>();
        for (var worker : verifier.workers.values()) {
            var item = new LinkedHashMap<String, Object>();
            item.put("id", worker.id);
            item.put("name", worker.name);
            item.put("leases", worker.leasesAcquired);
            item.put("starts", worker.starts);
            item.put("completions", worker.completions);
            item.put("expired_leases", worker.leasesExpired);
            item.put("active_tasks", worker.activeTasks.size());
            workerValues.add(item);
        }
        root.put("workers", workerValues);
        System.out.println(Json.stringify(Json.fromJava(root)));
    }

    static String shorten(String value, int max) {
        if (value.length() <= max) return value;
        if (max <= 3) return value.substring(0, max);
        return value.substring(0, max - 3) + "...";
    }

    static String humanBytes(long bytes) {
        if (bytes < 1024) return bytes + "B";
        double value = bytes;
        String[] units = {"KiB", "MiB", "GiB", "TiB"};
        for (String unit : units) {
            value /= 1024.0;
            if (value < 1024.0) return String.format(Locale.ROOT, "%.1f%s", value, unit);
        }
        return String.format(Locale.ROOT, "%.1fPiB", value / 1024.0);
    }
}
