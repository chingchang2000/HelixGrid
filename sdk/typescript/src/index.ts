const TASK_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;

export type WorkflowState =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCELLED";

export type TaskState =
  | "PENDING"
  | "READY"
  | "LEASED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "RETRY_WAIT"
  | "CANCELLED";

export interface RetryPolicy {
  max_attempts?: number;
  base_delay_ms?: number;
  max_delay_ms?: number;
}

export interface TaskSpec {
  id: string;
  depends_on?: string[];
  command: string[];
  env?: Record<string, string>;
  timeout_seconds?: number;
  retry?: RetryPolicy;
  labels?: Record<string, string>;
}

export interface WorkflowSpec {
  name: string;
  metadata?: Record<string, string>;
  tasks: TaskSpec[];
}

export interface TaskRuntime {
  state: TaskState;
  attempt: number;
  lease_token?: string;
  lease_owner?: string;
  lease_until?: string;
  started_at?: string;
  finished_at?: string;
  next_retry_at?: string;
  exit_code?: number;
  error?: string;
  output_bytes: number;
}

export interface Workflow {
  id: string;
  name: string;
  metadata?: Record<string, string>;
  state: WorkflowState;
  created_at: string;
  updated_at: string;
  finished_at?: string;
  tasks: Record<string, TaskSpec>;
  runtime: Record<string, TaskRuntime>;
  order: string[];
}

export interface Worker {
  id: string;
  name: string;
  version: string;
  labels?: Record<string, string>;
  capacity: number;
  active_leases: number;
  registered_at: string;
  last_heartbeat: string;
}

export interface HelixEvent {
  id: number;
  type: string;
  workflow_id?: string;
  task_id?: string;
  worker_id?: string;
  at: string;
  data?: Record<string, string>;
}

interface Envelope<T> {
  data: T;
}

export interface HelixClientOptions {
  baseUrl?: string;
  fetch?: typeof globalThis.fetch;
  defaultHeaders?: Record<string, string>;
}

export interface SubmitOptions {
  idempotencyKey?: string;
  signal?: AbortSignal;
}

export interface WaitOptions {
  pollIntervalMs?: number;
  timeoutMs?: number;
  signal?: AbortSignal;
}

export class HelixApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "HelixApiError";
    this.status = status;
    this.body = body;
  }
}

export class HelixClient {
  readonly baseUrl: string;
  private readonly fetchImpl: typeof globalThis.fetch;
  private readonly defaultHeaders: Record<string, string>;

  constructor(options: HelixClientOptions = {}) {
    const rawBaseUrl = options.baseUrl ?? "http://127.0.0.1:8080";
    let parsedBaseUrl: URL;
    try {
      parsedBaseUrl = new URL(rawBaseUrl);
    } catch {
      throw new Error("baseUrl must be an absolute http:// or https:// URL");
    }
    if (parsedBaseUrl.protocol !== "http:" && parsedBaseUrl.protocol !== "https:") {
      throw new Error("baseUrl must use http:// or https://");
    }
    this.baseUrl = rawBaseUrl.replace(/\/+$/, "");
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    if (!this.fetchImpl) {
      throw new Error("No fetch implementation available");
    }
    this.defaultHeaders = {
      Accept: "application/json",
      ...options.defaultHeaders,
    };
  }

  async submitWorkflow(spec: WorkflowSpec, options: SubmitOptions = {}): Promise<Workflow> {
    const headers: Record<string, string> = {};
    if (options.idempotencyKey) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }
    const response = await this.request<Envelope<Workflow>>("POST", "/v1/workflows", {
      body: spec,
      headers,
      signal: options.signal,
    });
    return response.data;
  }

  async listWorkflows(signal?: AbortSignal): Promise<Workflow[]> {
    const response = await this.request<Envelope<Workflow[]>>("GET", "/v1/workflows", { signal });
    return response.data;
  }

  async getWorkflow(id: string, signal?: AbortSignal): Promise<Workflow> {
    const response = await this.request<Envelope<Workflow>>(
      "GET",
      `/v1/workflows/${encodeURIComponent(id)}`,
      { signal },
    );
    return response.data;
  }

  async cancelWorkflow(id: string, signal?: AbortSignal): Promise<Workflow> {
    const response = await this.request<Envelope<Workflow>>(
      "POST",
      `/v1/workflows/${encodeURIComponent(id)}/cancel`,
      { signal },
    );
    return response.data;
  }

  async listWorkers(signal?: AbortSignal): Promise<Worker[]> {
    const response = await this.request<Envelope<Worker[]>>("GET", "/v1/workers", { signal });
    return response.data;
  }

  async waitForWorkflow(id: string, options: WaitOptions = {}): Promise<Workflow> {
    const pollInterval = options.pollIntervalMs ?? 500;
    if (!Number.isFinite(pollInterval) || pollInterval <= 0) {
      throw new Error("pollIntervalMs must be greater than zero");
    }
    if (
      options.timeoutMs !== undefined &&
      (!Number.isFinite(options.timeoutMs) || options.timeoutMs < 0)
    ) {
      throw new Error("timeoutMs may not be negative");
    }
    const started = Date.now();
    const terminal = new Set<WorkflowState>(["SUCCEEDED", "FAILED", "CANCELLED"]);

    for (;;) {
      options.signal?.throwIfAborted();
      const workflow = await this.getWorkflow(id, options.signal);
      if (terminal.has(workflow.state)) {
        return workflow;
      }
      if (options.timeoutMs !== undefined && Date.now() - started >= options.timeoutMs) {
        throw new Error(`Timed out waiting for workflow ${id}`);
      }
      await sleep(pollInterval, options.signal);
    }
  }

  async *events(
    id: string,
    options: { signal?: AbortSignal; lastEventId?: number } = {},
  ): AsyncGenerator<HelixEvent> {
    const headers = new Headers(this.defaultHeaders);
    headers.set("Accept", "text/event-stream");
    if (options.lastEventId !== undefined) {
      if (!Number.isSafeInteger(options.lastEventId) || options.lastEventId < 0) {
        throw new Error("lastEventId must be a non-negative safe integer");
      }
      headers.set("Last-Event-ID", String(options.lastEventId));
    }

    const init: RequestInit = { method: "GET", headers };
    if (options.signal !== undefined) {
      init.signal = options.signal;
    }
    const response = await this.fetchImpl(
      `${this.baseUrl}/v1/workflows/${encodeURIComponent(id)}/events`,
      init,
    );
    if (!response.ok) {
      await this.throwApiError(response);
    }
    if (!response.body) {
      throw new Error("Coordinator did not return an event stream body");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let eventName = "message";
    let dataLines: string[] = [];

    const emit = (): HelixEvent | undefined => {
      if (dataLines.length === 0) {
        eventName = "message";
        return undefined;
      }
      const raw = dataLines.join("\n");
      dataLines = [];
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        parsed = { type: eventName, data: { text: raw } };
      }
      eventName = "message";
      if (!isRecord(parsed)) {
        return undefined;
      }
      return parsed as unknown as HelixEvent;
    };

    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        for (;;) {
          const newline = buffer.indexOf("\n");
          if (newline < 0) break;
          let line = buffer.slice(0, newline);
          buffer = buffer.slice(newline + 1);
          if (line.endsWith("\r")) line = line.slice(0, -1);

          if (line === "") {
            const value = emit();
            if (value) yield value;
            continue;
          }
          if (line.startsWith(":")) continue;
          const colon = line.indexOf(":");
          const field = colon < 0 ? line : line.slice(0, colon);
          const rawValue = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
          switch (field) {
            case "event":
              eventName = rawValue;
              break;
            case "id":
              // Event IDs are already present in the JSON event body. The SSE id field is
              // intentionally accepted for wire compatibility and reconnect semantics.
              break;
            case "data":
              dataLines.push(rawValue);
              break;
          }
        }
      }
      const final = emit();
      if (final) yield final;
    } finally {
      reader.releaseLock();
    }
  }

  private async request<T>(
    method: string,
    path: string,
    options: {
      body?: unknown;
      headers?: Record<string, string>;
      signal?: AbortSignal | undefined;
    } = {},
  ): Promise<T> {
    const headers = new Headers(this.defaultHeaders);
    for (const [name, value] of Object.entries(options.headers ?? {})) {
      headers.set(name, value);
    }
    let body: string | undefined;
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(options.body);
    }

    const init: RequestInit = { method, headers };
    if (body !== undefined) init.body = body;
    if (options.signal !== undefined) init.signal = options.signal;

    const response = await this.fetchImpl(`${this.baseUrl}${path}`, init);
    if (!response.ok) {
      await this.throwApiError(response);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  private async throwApiError(response: Response): Promise<never> {
    const text = await response.text();
    let body: unknown = text;
    let message = text || `${response.status} ${response.statusText}`;
    try {
      body = JSON.parse(text);
      if (isRecord(body) && typeof body.error === "string") {
        message = body.error;
      }
    } catch {
      // Preserve the raw body when it is not JSON.
    }
    throw new HelixApiError(response.status, message, body);
  }
}

export interface BuilderTaskOptions {
  command: string[];
  env?: Record<string, string>;
  timeoutSeconds?: number;
  retry?: RetryPolicy;
  labels?: Record<string, string>;
}

export class WorkflowBuilder {
  private readonly name: string;
  private readonly tasks = new Map<string, TaskSpec>();
  private readonly metadataValues: Record<string, string> = {};

  constructor(name: string) {
    if (!name.trim()) throw new Error("Workflow name may not be empty");
    if ([...name].length > 200) throw new Error("Workflow name may not exceed 200 characters");
    this.name = name;
  }

  metadata(values: Record<string, string>): this {
    Object.assign(this.metadataValues, values);
    return this;
  }

  task(id: string, options: BuilderTaskOptions): this {
    if (!id.trim()) throw new Error("Task id may not be empty");
    if ([...id].length > 200 || !TASK_ID_PATTERN.test(id)) {
      throw new Error(`Invalid task id: ${id}`);
    }
    if (this.tasks.has(id)) throw new Error(`Duplicate task id: ${id}`);
    if (options.command.length === 0 || !options.command[0]?.trim()) {
      throw new Error(`Task ${id} has an empty command`);
    }
    if (options.command.length > 4096) {
      throw new Error(`Task ${id} exceeds the 4096 command argument limit`);
    }
    if (
      options.timeoutSeconds !== undefined &&
      (!Number.isInteger(options.timeoutSeconds) ||
        options.timeoutSeconds < 0 ||
        options.timeoutSeconds > 86_400)
    ) {
      throw new Error("timeoutSeconds must be an integer between 0 and 86400");
    }
    if (options.retry !== undefined) validateRetryPolicy(options.retry);

    const task: TaskSpec = {
      id,
      command: [...options.command],
    };
    if (options.env !== undefined) task.env = { ...options.env };
    if (options.timeoutSeconds !== undefined) task.timeout_seconds = options.timeoutSeconds;
    if (options.retry !== undefined) task.retry = { ...options.retry };
    if (options.labels !== undefined) task.labels = { ...options.labels };
    this.tasks.set(id, task);
    return this;
  }

  dependsOn(taskId: string, ...dependencies: string[]): this {
    const task = this.tasks.get(taskId);
    if (!task) throw new Error(`Unknown task: ${taskId}`);
    const existing = new Set(task.depends_on ?? []);
    for (const dependency of dependencies) {
      if (!this.tasks.has(dependency)) throw new Error(`Unknown dependency: ${dependency}`);
      if (dependency === taskId) throw new Error(`Task ${taskId} cannot depend on itself`);
      existing.add(dependency);
    }
    task.depends_on = [...existing];
    return this;
  }

  build(): WorkflowSpec {
    if (this.tasks.size === 0) throw new Error("Workflow must contain at least one task");
    const order = this.topologicalOrder();
    const workflow: WorkflowSpec = {
      name: this.name,
      tasks: order.map((id) => cloneTask(this.requireTask(id))),
    };
    if (Object.keys(this.metadataValues).length > 0) {
      workflow.metadata = { ...this.metadataValues };
    }
    return workflow;
  }

  topologicalOrder(): string[] {
    const indegree = new Map<string, number>();
    const children = new Map<string, string[]>();
    for (const id of this.tasks.keys()) {
      indegree.set(id, 0);
      children.set(id, []);
    }
    for (const task of this.tasks.values()) {
      for (const dependency of task.depends_on ?? []) {
        indegree.set(task.id, (indegree.get(task.id) ?? 0) + 1);
        children.get(dependency)?.push(task.id);
      }
    }

    const ready = [...indegree.entries()]
      .filter(([, degree]) => degree === 0)
      .map(([id]) => id)
      .sort();
    const result: string[] = [];

    while (ready.length > 0) {
      const current = ready.shift();
      if (current === undefined) break;
      result.push(current);
      for (const child of [...(children.get(current) ?? [])].sort()) {
        const next = (indegree.get(child) ?? 0) - 1;
        indegree.set(child, next);
        if (next === 0) {
          ready.push(child);
          ready.sort();
        }
      }
    }

    if (result.length !== this.tasks.size) {
      throw new Error("Workflow graph contains a dependency cycle");
    }
    return result;
  }

  private requireTask(id: string): TaskSpec {
    const task = this.tasks.get(id);
    if (!task) throw new Error(`Unknown task: ${id}`);
    return task;
  }
}

function validateRetryPolicy(retry: RetryPolicy): void {
  if (
    retry.max_attempts !== undefined &&
    (!Number.isInteger(retry.max_attempts) || retry.max_attempts < 1 || retry.max_attempts > 100)
  ) {
    throw new Error("max_attempts must be an integer between 1 and 100");
  }
  if (
    retry.base_delay_ms !== undefined &&
    (!Number.isInteger(retry.base_delay_ms) ||
      retry.base_delay_ms < 1 ||
      retry.base_delay_ms > 3_600_000)
  ) {
    throw new Error("base_delay_ms must be an integer between 1 and 3600000");
  }
  if (
    retry.max_delay_ms !== undefined &&
    (!Number.isInteger(retry.max_delay_ms) ||
      retry.max_delay_ms < 1 ||
      retry.max_delay_ms > 86_400_000)
  ) {
    throw new Error("max_delay_ms must be an integer between 1 and 86400000");
  }
  if (
    retry.base_delay_ms !== undefined &&
    retry.max_delay_ms !== undefined &&
    retry.max_delay_ms < retry.base_delay_ms
  ) {
    throw new Error("max_delay_ms may not be smaller than base_delay_ms");
  }
}

function cloneTask(task: TaskSpec): TaskSpec {
  const copy: TaskSpec = {
    id: task.id,
    command: [...task.command],
  };
  if (task.depends_on !== undefined) copy.depends_on = [...task.depends_on];
  if (task.env !== undefined) copy.env = { ...task.env };
  if (task.timeout_seconds !== undefined) copy.timeout_seconds = task.timeout_seconds;
  if (task.retry !== undefined) copy.retry = { ...task.retry };
  if (task.labels !== undefined) copy.labels = { ...task.labels };
  return copy;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sleep(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const abort = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(signal?.reason ?? new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(finish, milliseconds);
    signal?.addEventListener("abort", abort, { once: true });
  });
}
