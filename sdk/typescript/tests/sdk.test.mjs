import assert from "node:assert/strict";
import test from "node:test";

import { HelixClient, WorkflowBuilder } from "../dist/index.js";

test("builder rejects protocol-invalid task ids", () => {
  assert.throws(
    () => new WorkflowBuilder("x").task("bad id", { command: ["true"] }),
    /Invalid task id/,
  );
});

test("builder rejects invalid timeout and retry policy", () => {
  assert.throws(
    () => new WorkflowBuilder("x").task("a", { command: ["true"], timeoutSeconds: 100_000 }),
    /timeoutSeconds/,
  );
  assert.throws(
    () =>
      new WorkflowBuilder("x").task("a", {
        command: ["true"],
        retry: { max_attempts: 101 },
      }),
    /max_attempts/,
  );
  assert.throws(
    () =>
      new WorkflowBuilder("x").task("a", {
        command: ["true"],
        retry: { base_delay_ms: 2_000, max_delay_ms: 1_000 },
      }),
    /smaller/,
  );
});

test("client rejects invalid coordinator URLs", () => {
  assert.throws(() => new HelixClient({ baseUrl: "localhost:8080" }), /http|absolute/);
  assert.throws(() => new HelixClient({ baseUrl: "ftp://example.test" }), /http/);
});

test("wait validates timing before making a request", async () => {
  const fetch = async () => {
    throw new Error("fetch should not be called");
  };
  const client = new HelixClient({ fetch });
  await assert.rejects(
    client.waitForWorkflow("wf", { pollIntervalMs: 0 }),
    /pollIntervalMs/,
  );
  await assert.rejects(
    client.waitForWorkflow("wf", { timeoutMs: -1 }),
    /timeoutMs/,
  );
});
