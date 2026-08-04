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
    stages: [
      { stage: "input_validation", state: "completed", failure: null },
      { stage: "smartperfetto", state: "completed", failure: null },
      {
        stage: "perfpilot_ai",
        state: state === "completed" ? "completed" : "running",
        failure: null,
      },
      {
        stage: "report",
        state: state === "completed" ? "completed" : "pending",
        failure: null,
      },
    ],
    failure: null,
  };
}

function reportPayload(): Record<string, unknown> {
  return {
    schema_version: "1.1",
    analysis_id: ANALYSIS_ID,
    analysis_mode: "trace_upload",
    state: "completed",
    report_version: 2,
    generated_at: "2026-08-04T08:00:00Z",
    scenario_reports: [
      {
        scenario_job_id: "83000000-0000-4000-8000-000000000001",
        scenario_type: "startup",
        result_state: "completed",
        device_group_id: null,
        device_group_reason: "not_applicable",
        bundle: {
          schema_version: "1.0",
          bundle_id: "84000000-0000-4000-8000-000000000001",
          scenario_job_id: "83000000-0000-4000-8000-000000000001",
          scenario_type: "startup",
          bundle_state: "complete",
          valid_measurement: true,
          validity_reasons: [],
          sample_ids: [],
          generated_at: "2026-08-04T08:00:00Z",
          metrics: [],
          findings: [],
          evidence: [],
          artifacts: [],
          trace_health: {},
          trace_capabilities: [],
          provenance: {},
        },
        failure: null,
      },
    ],
    synthesis: {
      state: "completed",
      output: {
        schema_version: "1.0",
        executive_summary: "启动阶段存在可优化的主线程等待。",
        top_findings: [],
        recommendations: [],
        retest_plan: [],
        limitations: [],
      },
      synthesis_artifact_id: "88000000-0000-4000-8000-000000000001",
      failure_code: null,
      provenance: {
        provider_protocol: "chat-completions-json-schema-v1",
        provider_name: "approved-provider",
        model: "approved-model",
        prompt_template_version: "1.0.0",
        prompt_template_sha256_b64: "c".repeat(44),
        normalizer_version: "smartperfetto-normalizer-1",
        report_worker_image_digest: `sha256:${"1".repeat(64)}`,
        projection_artifact_id: "89000000-0000-4000-8000-000000000001",
        projection_sha256_b64: "c".repeat(44),
        generated_at: "2026-08-04T08:00:00Z",
        prompt_tokens: 10,
        completion_tokens: 20,
        total_tokens: 30,
        generation: 2,
      },
    },
  };
}

describe("PerfPilot browser API", () => {
  it("lists only validated report-bearing analyses for the requested team", async () => {
    const latest = {
      ...analysis("completed"),
      report_available: true,
      created_at: "2026-08-04T08:00:00+00:00",
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", analyses: [latest] }),
      )
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", analyses: [] }),
      )
      .mockResolvedValueOnce(
        Response.json({
          schema_version: "1.0",
          analyses: [{ ...latest, created_at: undefined }],
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          schema_version: "1.0",
          analyses: [{ ...latest, team_id: "another-team" }],
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          schema_version: "1.0",
          analyses: [{ ...latest, report_available: false }],
        }),
      );
    const client = createPerfPilotClient({ fetcher });

    await expect(client.analyses(TEAM_ID, 1)).resolves.toEqual({
      schema_version: "1.0",
      analyses: [latest],
    });
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      `/api/v1/teams/${TEAM_ID}/analyses?report_available=true&limit=1`,
      expect.objectContaining({ credentials: "same-origin", redirect: "error" }),
    );
    await expect(client.analyses(TEAM_ID, 1)).resolves.toEqual({
      schema_version: "1.0",
      analyses: [],
    });
    await expect(client.analyses(TEAM_ID, 1)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
    await expect(client.analyses(TEAM_ID, 1)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
    await expect(client.analyses(TEAM_ID, 1)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
  });

  it("reads the current local ADB device instead of dashboard fixture data", async () => {
    const payload = {
      schema_version: "1.0",
      state: "connected",
      device: {
        serial: "0123456789ABCDEF",
        manufacturer: "UNISOC",
        model: "uis7870_2h10_car_c200_6",
        name: "UNISOC uis7870_2h10_car_c200_6",
        os: "Android 13",
        api_level: 33,
      },
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(payload));

    await expect(createPerfPilotClient({ fetcher }).device()).resolves.toEqual(payload);
    expect(fetcher).toHaveBeenCalledWith(
      "/api/v1/device",
      expect.objectContaining({ credentials: "same-origin", redirect: "error" }),
    );
  });

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

  it("returns after upload acceptance without polling the Trace to a terminal state", async () => {
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

    const sleep = vi.fn(async () => undefined);
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
        sleep,
      },
    );

    expect(result.analysis.state).toBe("analyzing");
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
    expect(statusReads).toBe(1);
    expect(sleep).not.toHaveBeenCalled();
    const statusCalls = calls.filter((call) => call.url.endsWith(`/analyses/${ANALYSIS_ID}`));
    expect(
      statusCalls.every(
        (call) => new Headers(call.init.headers).get("x-csrf-token") === "csrf-1",
      ),
    ).toBe(true);
  });

  it("queries the active analysis and sends an authenticated cancel request", async () => {
    const active = {
      ...analysis("analyzing"),
      created_at: "2026-08-04T08:00:00+00:00",
      cancel_requested_at: null,
    };
    const canceled = {
      ...active,
      state: "canceled",
      cancel_requested_at: "2026-08-04T08:01:00+00:00",
      stages: active.stages.map((stage) =>
        stage.state === "completed" ? stage : { ...stage, state: "canceled" },
      ),
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", analyses: [active] }),
      )
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", csrf_token: "csrf-cancel" }),
      )
      .mockResolvedValueOnce(Response.json(canceled, { status: 202 }));
    const client = createPerfPilotClient({ fetcher });
    const activeClient = client as typeof client & {
      activeAnalyses(
        teamId: string,
        limit?: number,
      ): Promise<{ schema_version: "1.0"; analyses: readonly AnalysisResponse[] }>;
      cancelAnalysis(teamId: string, analysisId: string): Promise<AnalysisResponse>;
    };

    await expect(activeClient.activeAnalyses(TEAM_ID, 1)).resolves.toEqual({
      schema_version: "1.0",
      analyses: [active],
    });
    await client.csrf();
    await expect(
      activeClient.cancelAnalysis(TEAM_ID, ANALYSIS_ID),
    ).resolves.toMatchObject({ state: "canceled" });

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      `/api/v1/teams/${TEAM_ID}/analyses?status=active&limit=1`,
      expect.objectContaining({ credentials: "same-origin", redirect: "error" }),
    );
    const cancelCall = fetcher.mock.calls[2];
    expect(cancelCall?.[0]).toBe(
      `/api/v1/teams/${TEAM_ID}/analyses/${ANALYSIS_ID}/cancel`,
    );
    expect(cancelCall?.[1]?.method).toBe("POST");
    expect(new Headers(cancelCall?.[1]?.headers).get("x-csrf-token")).toBe(
      "csrf-cancel",
    );
  });

  it("accepts local loopback upload authorization without allowing remote plain HTTP", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(null, { status: 200 }),
    );
    const client = createPerfPilotClient({ fetcher });
    const input = {
      kind: "trace" as const,
      file: new File([new Uint8Array([1, 2, 3])], "local.trace"),
      mime: "application/octet-stream",
      size: 3,
      sha256_b64: "A5BYxvLAy0ksUzsKTRTvd8wPeKvMztUofYShogEc+4E=",
    };
    const upload = {
      schema_version: "1.0" as const,
      upload: {
        state: "pending" as const,
        upload_id: "local-upload",
        artifact_kind: "trace" as const,
        mime: input.mime,
        size: input.size,
        sha256_b64: input.sha256_b64,
        put_url: "http://localhost:8000/local/v1/uploads/local-upload?token=local-token",
        required_headers: {
          "Content-Type": input.mime,
          "x-amz-checksum-sha256": input.sha256_b64,
        },
      },
    };

    await expect(client.putInput(upload, input)).resolves.toBeUndefined();
    expect(fetcher).toHaveBeenCalledTimes(1);

    const remote = structuredClone(upload);
    remote.upload.put_url =
      "http://192.0.2.10:8000/local/v1/uploads/local-upload?token=local-token";
    await expect(client.putInput(remote, input)).rejects.toMatchObject({
      code: "invalid_upload_authorization",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("reads the exact four server stages and rejects a reordered stage list", async () => {
    const valid = analysis("completed");
    const local = {
      ...valid,
      ai_rounds: [
        { round: 1, role: "extract", state: "completed", attempts: 1 },
        { round: 2, role: "review", state: "completed", attempts: 1 },
        { round: 3, role: "finalize", state: "completed", attempts: 1 },
      ],
      source_analysis: {
        engine: "smartperfetto",
        rounds: 53,
        verification: "passed",
        session_id: "agent-session-1",
        run_id: "run-session-1",
      },
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(valid))
      .mockResolvedValueOnce(
        Response.json({ ...valid, stages: [...valid.stages].reverse() }),
      )
      .mockResolvedValueOnce(Response.json(local))
      .mockResolvedValueOnce(
        Response.json({ ...local, ai_rounds: [...local.ai_rounds].reverse() }),
      );
    const client = createPerfPilotClient({ fetcher });

    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      stages: [
        { stage: "input_validation", state: "completed" },
        { stage: "smartperfetto", state: "completed" },
        { stage: "perfpilot_ai", state: "completed" },
        { stage: "report", state: "completed" },
      ],
    });
    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      ai_rounds: [
        { round: 1, role: "extract", state: "completed" },
        { round: 2, role: "review", state: "completed" },
        { round: 3, role: "finalize", state: "completed" },
      ],
      source_analysis: { engine: "smartperfetto", rounds: 53 },
    });
    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
  });

  it("reads an AnalysisReport 1.1 and creates an idempotent AI-only rerun", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const fetcher = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      calls.push({ url, init });
      if (url === "/api/v1/auth/csrf") {
        return Response.json({ schema_version: "1.0", csrf_token: "csrf-report" });
      }
      if (url.endsWith("/report")) return Response.json(reportPayload());
      if (url.endsWith("/synthesis-runs")) {
        return Response.json(
          {
            schema_version: "1.0",
            analysis_id: ANALYSIS_ID,
            generation: 3,
            state: "queued",
          },
          { status: 201 },
        );
      }
      throw new Error(`undeclared request: ${url}`);
    });
    const client = createPerfPilotClient({ fetcher });

    await client.csrf();
    await expect(client.report(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      schema_version: "1.1",
      report_version: 2,
      synthesis: { state: "completed" },
    });
    await expect(
      client.createSynthesisRun(TEAM_ID, ANALYSIS_ID, "ai-rerun-fixed"),
    ).resolves.toEqual({
      schema_version: "1.0",
      analysis_id: ANALYSIS_ID,
      generation: 3,
      state: "queued",
    });

    const reportCall = calls.find((call) => call.url.endsWith("/report"));
    expect(reportCall?.url).toBe(
      `/api/v1/teams/${TEAM_ID}/analyses/${ANALYSIS_ID}/report`,
    );
    expect(reportCall?.init.credentials).toBe("same-origin");
    expect(reportCall?.init.redirect).toBe("error");
    const rerunCall = calls.find((call) => call.url.endsWith("/synthesis-runs"));
    expect(rerunCall?.init.method).toBe("POST");
    expect(rerunCall?.init.body).toBeUndefined();
    expect(rerunCall?.init.credentials).toBe("same-origin");
    expect(rerunCall?.init.redirect).toBe("error");
    expect(new Headers(rerunCall?.init.headers).get("x-csrf-token")).toBe("csrf-report");
    expect(new Headers(rerunCall?.init.headers).get("idempotency-key")).toBe(
      "ai-rerun-fixed",
    );
  });

  it("rejects unknown or transport-private report fields", async () => {
    const unknown = { ...reportPayload(), unexpected: true };
    const privateReport = structuredClone(reportPayload());
    const synthesis = privateReport.synthesis as Record<string, unknown>;
    synthesis.provider_endpoint = "https://provider.example/v1/chat/completions";
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(unknown))
      .mockResolvedValueOnce(Response.json(privateReport));
    const client = createPerfPilotClient({ fetcher });

    await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
    await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
  });

  it("rejects a report or rerun response for a different analysis", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json({ ...reportPayload(), analysis_id: "other-analysis" }),
      )
      .mockResolvedValueOnce(Response.json({ schema_version: "1.0", csrf_token: "csrf" }))
      .mockResolvedValueOnce(
        Response.json({
          schema_version: "1.0",
          analysis_id: "other-analysis",
          generation: 2,
          state: "queued",
        }),
      );
    const client = createPerfPilotClient({ fetcher });

    await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
    await client.csrf();
    await expect(
      client.createSynthesisRun(TEAM_ID, ANALYSIS_ID, "rerun-identity"),
    ).rejects.toMatchObject({ code: "invalid_api_response" });
  });

  it("rejects oversized report responses before parsing", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("{}", {
        headers: { "content-length": String(10 * 1024 * 1024 + 1) },
      }),
    );

    await expect(
      createPerfPilotClient({ fetcher }).report(TEAM_ID, ANALYSIS_ID),
    ).rejects.toMatchObject({ code: "invalid_api_response" });
  });
});
