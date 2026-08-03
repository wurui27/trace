import { describe, expect, it, vi } from "vitest";

import {
  createPerfPilotClient,
  sha256Base64,
  submitTraceAnalysis,
  type AnalysisResponse,
} from "../app/lib/perfpilot-api";

const ANALYSIS_ID = "82000000-0000-4000-8000-000000000001";
const TEAM_ID = "81000000-0000-4000-8000-000000000001";

function analysis(state: AnalysisResponse["state"]): AnalysisResponse {
  return {
    schema_version: "1.0",
    analysis_id: ANALYSIS_ID,
    team_id: TEAM_ID,
    analysis_mode: "trace_upload",
    analysis_profile: "scroll",
    question: "为什么掉帧？",
    state,
    version: 1,
    report_available: false,
    input_uploads: [],
    failure: null,
  };
}

describe("PerfPilot browser API", () => {
  it("hashes streams incrementally without calling File.arrayBuffer", async () => {
    const file = new File([new Uint8Array([1, 2, 3])], "trace.pb");
    Object.defineProperty(file, "arrayBuffer", {
      value: () => {
        throw new Error("arrayBuffer must not be called");
      },
    });

    await expect(sha256Base64(file)).resolves.toBe(
      "A5BYxvLAy0ksUzsKTRTvd8wPeKvMztUofYShogEc+4E=",
    );
  });

  it("creates exact slots uploads directly finalizes and polls one Trace analysis", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    let statusReads = 0;
    const fetcher = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      calls.push({ url, init });
      if (url === "/api/v1/auth/csrf") {
        return Response.json({ schema_version: "1.0", csrf_token: "csrf-1" });
      }
      if (url === "/api/v1/me") {
        return Response.json({
          schema_version: "1.0",
          user: { id: "user-1", username: "ray_wu", is_platform_admin: true },
          memberships: [
            { id: "member-1", team: { id: TEAM_ID, name: "Ray" }, role: "owner" },
          ],
        });
      }
      if (url === `/api/v1/teams/${TEAM_ID}/analyses` && init.method === "POST") {
        return Response.json(analysis("created"), { status: 201 });
      }
      const slot = url.match(/\/analyses\/[^/]+\/uploads$/);
      if (slot) {
        const body = JSON.parse(String(init.body)) as {
          artifact_kind: string;
          sha256_b64: string;
        };
        return Response.json(
          {
            schema_version: "1.0",
            upload: {
              state: "pending",
              upload_id: `${body.artifact_kind}-upload-id`,
              artifact_kind: body.artifact_kind,
              mime: body.artifact_kind === "trace" ? "application/octet-stream" : "text/plain",
              size: body.artifact_kind === "trace" ? 3 : 2,
              sha256_b64: body.sha256_b64,
              expires_at: "2026-08-03T09:00:00Z",
              put_url: `https://objects.example/${body.artifact_kind}?private=secret`,
              required_headers: {
                "Content-Type":
                  body.artifact_kind === "trace" ? "application/octet-stream" : "text/plain",
                "x-amz-checksum-sha256": body.sha256_b64,
              },
            },
          },
          { status: 201 },
        );
      }
      if (url.startsWith("https://objects.example/")) {
        return new Response(null, { status: 200 });
      }
      if (url.endsWith("/finalize-upload")) {
        const body = JSON.parse(String(init.body)) as { upload_id: string };
        const kind = body.upload_id.startsWith("trace") ? "trace" : "mapping";
        return Response.json({
          schema_version: "1.0",
          upload: {
            state: "finalized",
            upload_id: body.upload_id,
            artifact_id: `${kind}-artifact-id`,
            artifact_kind: kind,
            mime: kind === "trace" ? "application/octet-stream" : "text/plain",
            size: kind === "trace" ? 3 : 2,
            sha256_b64: "ignored",
            finalized_at: "2026-08-03T08:01:00Z",
          },
        });
      }
      if (url.endsWith(`/analyses/${ANALYSIS_ID}`)) {
        statusReads += 1;
        return Response.json(analysis(statusReads === 1 ? "analyzing" : "completed"));
      }
      throw new Error(`undeclared request: ${url}`);
    });
    const client = createPerfPilotClient({ fetcher });
    const trace = new File([new Uint8Array([1, 2, 3])], "scroll.trace");
    const mapping = new File([new Uint8Array([4, 5])], "mapping.txt", {
      type: "text/plain",
    });

    const result = await submitTraceAnalysis(
      {
        profile: "scroll",
        question: "为什么掉帧？",
        files: [
          { kind: "trace", file: trace },
          { kind: "mapping", file: mapping },
        ],
      },
      {
        client,
        randomUUID: () => "trace-analysis-fixed",
        sleep: async () => undefined,
      },
    );

    expect(result.analysis.state).toBe("completed");
    expect(result.analysis.analysis_id).toBe(ANALYSIS_ID);
    const create = calls.find(
      (call) => call.url.endsWith("/analyses") && call.init.method === "POST",
    );
    expect(new Headers(create?.init.headers).get("idempotency-key")).toBe(
      "trace-analysis-fixed",
    );
    expect(JSON.parse(String(create?.init.body))).toMatchObject({
      schema_version: "1.0",
      analysis_mode: "trace_upload",
      analysis_profile: "scroll",
      question: "为什么掉帧？",
      inputs: [
        { kind: "trace", mime: "application/octet-stream", size: 3 },
        { kind: "mapping", mime: "text/plain", size: 2 },
      ],
    });
    const slots = calls.filter((call) => call.url.endsWith("/uploads"));
    expect(slots.map((call) => new Headers(call.init.headers).get("idempotency-key"))).toEqual([
      "input-trace",
      "input-mapping",
    ]);
    const puts = calls.filter((call) => call.url.startsWith("https://objects.example/"));
    expect(puts).toHaveLength(2);
    expect(new Headers(puts[0].init.headers)).toEqual(
      new Headers({
        "Content-Type": "application/octet-stream",
        "x-amz-checksum-sha256":
          "A5BYxvLAy0ksUzsKTRTvd8wPeKvMztUofYShogEc+4E=",
      }),
    );
    expect(puts[0].init.credentials).toBe("omit");
    expect(statusReads).toBe(2);
    const statusCalls = calls.filter((call) => call.url.endsWith(`/analyses/${ANALYSIS_ID}`));
    expect(
      statusCalls.every(
        (call) => new Headers(call.init.headers).get("x-csrf-token") === "csrf-1",
      ),
    ).toBe(true);
  });
});
