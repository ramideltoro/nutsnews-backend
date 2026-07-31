import { writeFileSync } from "node:fs";
import { performance } from "node:perf_hooks";

import {
  describe,
  expect,
  it
} from "vitest";

import {
  LocalAiApprovalQwenClient,
  ManualApprovalClock
} from "../src/index.js";


describe("issue 161 Qwen adapter outage drill", () => {
  it("detects an unavailable Qwen provider and recovers through the exact production adapter", async () => {
    let unavailable = true;
    const client = new LocalAiApprovalQwenClient({
      baseUrl: "http://127.0.0.1",
      apiKey: "synthetic-not-sent-to-a-network",
      clock: new ManualApprovalClock(),
      fetcher: () => unavailable
        ? Promise.reject(new Error("controlled Qwen outage"))
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
      scenario: "qwen_unavailable",
      adapter: "LocalAiApprovalQwenClient",
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
