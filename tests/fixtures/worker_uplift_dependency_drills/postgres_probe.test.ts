import { writeFileSync } from "node:fs";
import { performance } from "node:perf_hooks";

import { Pool } from "pg";
import {
  describe,
  expect,
  it
} from "vitest";

import { PostgresPersistenceInboxStore } from "../src/index.js";


describe("issue 161 PostgreSQL adapter outage drill", () => {
  it("detects an unavailable PostgreSQL endpoint and recovers through the exact production adapter", async () => {
    const unavailablePool = new Pool({
      host: "127.0.0.1",
      port: 1,
      user: "postgres",
      database: "postgres",
      max: 1,
      connectionTimeoutMillis: 250
    });
    const unavailableStore = new PostgresPersistenceInboxStore(unavailablePool);
    const detectionStarted = performance.now();
    const unavailable = await unavailableStore.probe();
    const detectionMs = performance.now() - detectionStarted;
    await unavailablePool.end();

    expect(unavailable.status).toBe("unhealthy");
    expect(detectionMs).toBeLessThan(2_000);

    const recoveredPool = new Pool({
      host: "127.0.0.1",
      port: 5432,
      user: "postgres",
      database: "postgres",
      max: 1,
      connectionTimeoutMillis: 1_000
    });
    const recoveredStore = new PostgresPersistenceInboxStore(recoveredPool);
    const recoveryStarted = performance.now();
    const recovered = await recoveredStore.probe();
    const recoveryMs = performance.now() - recoveryStarted;
    await recoveredPool.end();

    expect(recovered.status).toBe("ok");
    expect(recoveryMs).toBeLessThan(2_000);

    const outputPath = process.env.NUTSNEWS_DEPENDENCY_DRILL_OUTPUT;
    expect(outputPath).toBeTruthy();
    writeFileSync(outputPath!, JSON.stringify({
      schema_version: 1,
      scenario: "postgresql_unavailable",
      adapter: "PostgresPersistenceInboxStore",
      status: "pass",
      failure_detected: unavailable.status === "unhealthy",
      detection_ms: Math.ceil(detectionMs),
      recovered: recovered.status === "ok",
      recovery_ms: Math.ceil(recoveryMs),
      endpoint_recorded: false,
      credential_value_recorded: false
    }, null, 2));
  });
});
