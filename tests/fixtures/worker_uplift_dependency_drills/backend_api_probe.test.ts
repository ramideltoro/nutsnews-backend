import { writeFileSync } from "node:fs";
import { performance } from "node:perf_hooks";

import {
  describe,
  expect,
  it
} from "vitest";

import { HttpPersistenceBackendWorkerApiClient } from "../src/index.js";


describe("issue 161 backend API adapter outage drill", () => {
  it("detects an unavailable backend API and recovers through the exact production adapter", async () => {
    let unavailable = true;
    const client = new HttpPersistenceBackendWorkerApiClient({
      baseUrl: "http://127.0.0.1",
      token: "synthetic-not-used-by-health-probe",
      expectedVersion: "worker-api-v1",
      fetcher: () => unavailable
        ? Promise.reject(new Error("controlled backend API outage"))
        : Promise.resolve(new Response("{}", {
            status: 200,
            headers: {
              "content-type": "application/json"
            }
          }))
    });

    const detectionStarted = performance.now();
    const failedProbe = await client.probe();
    const detectionMs = performance.now() - detectionStarted;

    expect(failedProbe.status).toBe("unhealthy");
    expect(detectionMs).toBeLessThan(2_000);

    unavailable = false;
    const recoveryStarted = performance.now();
    const recoveredProbe = await client.probe();
    const recoveryMs = performance.now() - recoveryStarted;

    expect(recoveredProbe.status).toBe("ok");
    expect(recoveryMs).toBeLessThan(2_000);

    const outputPath = process.env.NUTSNEWS_DEPENDENCY_DRILL_OUTPUT;
    expect(outputPath).toBeTruthy();
    writeFileSync(outputPath!, JSON.stringify({
      schema_version: 1,
      scenario: "backend_api_unavailable",
      adapter: "HttpPersistenceBackendWorkerApiClient",
      status: "pass",
      failure_detected: failedProbe.status === "unhealthy",
      detection_ms: Math.ceil(detectionMs),
      recovered: recoveredProbe.status === "ok",
      recovery_ms: Math.ceil(recoveryMs),
      endpoint_recorded: false,
      credential_value_recorded: false
    }, null, 2));
  });
});
