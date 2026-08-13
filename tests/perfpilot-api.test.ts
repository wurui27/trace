import { describe, expect, it, vi } from "vitest";

import {
  createRandomUuid,
  createPerfPilotClient,
  validUploadUrl,
  enqueueDeviceAnalysis,
  sha256Base64,
  submitTraceAnalysis,
  type AnalysisResponse,
} from "../app/lib/perfpilot-api";

const ANALYSIS_ID = "82000000-0000-4000-8000-000000000001";
const TEAM_ID = "81000000-0000-4000-8000-000000000001";
const DEVICE_ID = "74000000-0000-4000-8000-000000000001";
const AGENT_ID = "73000000-0000-4000-8000-000000000001";

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

function sourceAwareReportPayload(): Record<string, unknown> {
  const report = structuredClone(reportPayload());
  report.schema_version = "1.2";
  report.source_code = {
    requested: false,
    provider_kind: null,
    agent_id: null,
    workspace_id: null,
    snapshot_policy: null,
    validation_profile_id: null,
    snapshot: null,
    context_state: "not_requested",
    match_summary: "none",
    source_refs: [],
    exclusions: [],
    fixes: [],
    limitations: [],
  };
  const synthesis = report.synthesis as Record<string, unknown>;
  synthesis.output = {
    schema_version: "2.0",
    verdict: "启动存在主线程等待",
    executive_summary: "先缩短主线程阻塞，再按相同场景复测。",
    key_metric_ids: [],
    top_findings: [],
    recommendations: [],
    source_fixes: [],
    retest_plan: [],
    limitations: [],
  };
  return report;
}

describe("PerfPilot browser API", () => {
  it("notifies subscribers when a runtime request loses authentication", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      Response.json(
        {
          schema_version: "1.0",
          error: {
            code: "authentication_required",
            message: "请先登录",
            retryable: false,
            request_id: "request-1",
          },
        },
        { status: 401 },
      ),
    );
    const client = createPerfPilotClient({ fetcher });
    const notified = vi.fn();
    client.subscribeAuthFailures?.(notified);

    await expect(client.devices(TEAM_ID)).rejects.toMatchObject({
      code: "authentication_required",
    });
    expect(notified).toHaveBeenCalledTimes(1);
  });

  it("authenticates with a closed login contract and validates the current user", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", csrf_token: "preauth-csrf" }),
      )
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", csrf_token: "session-csrf" }),
      )
      .mockResolvedValueOnce(
        Response.json({
          schema_version: "1.0",
          user: {
            id: "80000000-0000-4000-8000-000000000001",
            username: "user01",
            is_platform_admin: false,
            must_change_password: true,
          },
          memberships: [
            {
              id: TEAM_ID,
              team: { id: TEAM_ID, name: "user01 local team" },
              role: "owner",
            },
          ],
        }),
      );
    const client = createPerfPilotClient({ fetcher });

    await client.csrf();
    await expect((client as unknown as { login: (username: string, password: string) => Promise<string> }).login("user01", "initial user password")).resolves.toBe("session-csrf");
    await expect(client.me()).resolves.toMatchObject({
      user: { username: "user01", must_change_password: true },
    });
    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      "/api/v1/auth/login",
      expect.objectContaining({ method: "POST", credentials: "same-origin" }),
    );
  });
  it("accepts only closed public Agent source workspace records", async () => {
    const workspace = {
      provider_kind: "agent_workspace",
      agent_id: AGENT_ID,
      agent_name: "Mac Agent",
      workspace_id: "92000000-0000-4000-8000-000000000001",
      name: "Demo App",
      state: "ready",
      git_branch: "main",
      git_head: "a".repeat(40),
      tracked_dirty_count: 1,
      snapshot_policy: "tracked_worktree",
      validation_profiles: [
        {
          profile_id: "94000000-0000-4000-8000-000000000001",
          name: "Unit tests",
        },
      ],
    };
    const responses = [
      { schema_version: "1.0", workspaces: [workspace] },
      ...["path", "repo_url", "remote", "argv"].map((key) => ({
        schema_version: "1.0",
        workspaces: [{ ...workspace, [key]: "private" }],
      })),
      { schema_version: "1.0", workspaces: [workspace, workspace] },
      {
        schema_version: "1.0",
        workspaces: [{ ...workspace, git_head: "not-a-sha" }],
      },
    ];
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () =>
      Response.json(responses.shift()),
    );
    const client = createPerfPilotClient({ fetcher });

    await expect(client.sourceWorkspaces(TEAM_ID)).resolves.toEqual({
      schema_version: "1.0",
      workspaces: [workspace],
    });
    for (let index = 0; index < 6; index += 1) {
      await expect(client.sourceWorkspaces(TEAM_ID)).rejects.toMatchObject({
        code: "invalid_api_response",
      });
    }
  });

  it("generates an idempotency UUID when private-LAN HTTP hides crypto.randomUUID", () => {
    const source = {
      getRandomValues(target: Uint8Array): Uint8Array {
        target.set(Array.from({ length: 16 }, (_, index) => index));
        return target;
      },
    };

    expect(createRandomUuid(source)).toBe("00010203-0405-4607-8809-0a0b0c0d0e0f");
  });

  it("accepts a private-LAN upload URL only when it matches the web host", () => {
    const uploadUrl =
      "http://10.166.0.125:8000/local/v1/uploads/upload-1?token=opaque-token";

    expect(validUploadUrl(uploadUrl, "http://10.166.0.125:3000")).toBe(true);
    expect(validUploadUrl(uploadUrl, "http://10.166.0.126:3000")).toBe(false);
    expect(
      validUploadUrl(
        "http://public.example:8000/local/v1/uploads/upload-1?token=opaque-token",
        "http://public.example:3000",
      ),
    ).toBe(false);
  });

  it("validates remote Agent and device control-plane responses", async () => {
    const agent = {
      agent_id: AGENT_ID,
      name: "Ubuntu 实验室",
      platform: "linux",
      agent_version: "1.2.3",
      hostname: "rivotek",
      os_version: "Ubuntu 24.04",
      state: "online",
      last_heartbeat_at: "2026-08-05T08:00:00Z",
      created_at: "2026-08-05T07:00:00Z",
      updated_at: "2026-08-05T08:00:00Z",
    };
    const device = {
      device_id: DEVICE_ID,
      agent_id: AGENT_ID,
      agent_name: agent.name,
      serial_suffix: "7K2A",
      manufacturer: "UNISOC",
      model: "ums9620",
      android_release: "15",
      api_level: 35,
      connection_type: "usb",
      adb_state: "device",
      state: "ready",
      last_seen_at: "2026-08-05T08:00:00Z",
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", csrf_token: "csrf-agent" }),
      )
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", devices: [device] }),
      )
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", agents: [agent] }),
      )
      .mockResolvedValueOnce(
        Response.json({
          schema_version: "1.0",
          agent_id: AGENT_ID,
          registration_code: `ppreg_${"A".repeat(43)}`,
          expires_at: "2026-08-05T08:10:00Z",
        }),
      )
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", agent: { ...agent, name: "Mac Agent" } }),
      )
      .mockResolvedValueOnce(
        Response.json({ schema_version: "1.0", agent: { ...agent, state: "revoked" } }),
      );
    const client = createPerfPilotClient({ fetcher });

    await client.csrf();
    await expect(client.devices(TEAM_ID)).resolves.toEqual({
      schema_version: "1.0",
      devices: [device],
    });
    await expect(client.agents(TEAM_ID)).resolves.toEqual({
      schema_version: "1.0",
      agents: [agent],
    });
    await expect(
      client.createAgentRegistrationCode(TEAM_ID, "Ubuntu 实验室"),
    ).resolves.toMatchObject({ agent_id: AGENT_ID });
    await expect(client.renameAgent(TEAM_ID, AGENT_ID, "Mac Agent")).resolves.toMatchObject({
      name: "Mac Agent",
    });
    await expect(client.revokeAgent(TEAM_ID, AGENT_ID)).resolves.toMatchObject({
      state: "revoked",
    });

    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      `/api/v1/teams/${TEAM_ID}/devices`,
      expect.objectContaining({ credentials: "same-origin" }),
    );
    for (const call of fetcher.mock.calls.slice(1)) {
      expect(new Headers(call[1]?.headers).get("x-csrf-token")).toBe("csrf-agent");
    }
    expect(JSON.parse(String(fetcher.mock.calls[3]?.[1]?.body))).toEqual({
      schema_version: "1.0",
      name: "Ubuntu 实验室",
    });
  });

  it("creates a device analysis for the selected device and uploads its APK", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const checksum = "A5BYxvLAy0ksUzsKTRTvd8wPeKvMztUofYShogEc+4E=";
    const deviceAnalysis = {
      schema_version: "1.0",
      analysis_id: ANALYSIS_ID,
      team_id: TEAM_ID,
      analysis_mode: "device",
      device_id: DEVICE_ID,
      state: "created",
      version: 2,
      application_version_id: null,
      application_metadata: null,
      apk_upload: {
        state: "pending",
        upload_id: "85000000-0000-4000-8000-000000000001",
        artifact_kind: "apk",
        mime: "application/vnd.android.package-archive",
        size: 3,
        sha256_b64: checksum,
        expires_at: "2026-08-05T08:15:00Z",
        put_url: "https://objects.example/device-apk?signature=private",
        required_headers: {
          "Content-Type": "application/vnd.android.package-archive",
          "x-amz-checksum-sha256": checksum,
        },
      },
      scenarios: ["cold_start", "scroll", "memory_cycle"].map((scenario_type) => ({
        scenario_job_id: null,
        scenario_type,
        state: "awaiting_input",
        version: null,
        device_group_id: null,
        sample_verdict_counts: {
          valid: 0,
          invalid: 0,
          pending: 0,
          validation_error: 0,
          total: 0,
        },
        started_at: null,
        completed_at: null,
        failure: null,
      })),
      sample_verdict_counts: {
        valid: 0,
        invalid: 0,
        pending: 0,
        validation_error: 0,
        total: 0,
      },
      active_lease: null,
      report_available: false,
      created_at: "2026-08-05T08:00:00Z",
      started_at: null,
      completed_at: null,
      failure: null,
    };
    const fetcher = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      calls.push({ url, init });
      if (url === "/api/v1/auth/csrf") {
        return Response.json({ schema_version: "1.0", csrf_token: "csrf-device" });
      }
      if (url.endsWith("/analyses") && init.method === "POST") {
        return Response.json(deviceAnalysis, { status: 201 });
      }
      if (url.startsWith("https://objects.example/")) {
        return new Response(null, { status: 200 });
      }
      if (url.endsWith("/finalize-upload")) {
        return Response.json({
          schema_version: "1.0",
          upload: {
            state: "finalized",
            upload_id: deviceAnalysis.apk_upload.upload_id,
            artifact_id: "86000000-0000-4000-8000-000000000001",
            artifact_kind: "apk",
            mime: deviceAnalysis.apk_upload.mime,
            size: 3,
            sha256_b64: checksum,
            finalized_at: "2026-08-05T08:01:00Z",
          },
        });
      }
      if (url.endsWith(`/analyses/${ANALYSIS_ID}`)) {
        return Response.json({ ...deviceAnalysis, state: "queued", version: 4 });
      }
      throw new Error(`undeclared request: ${url}`);
    });
    const client = createPerfPilotClient({ fetcher });
    const apk = new File([new Uint8Array([1, 2, 3])], "demo.apk", {
      type: "application/vnd.android.package-archive",
    });

    const result = await enqueueDeviceAnalysis(
      { teamId: TEAM_ID, deviceId: DEVICE_ID, apk },
      { client, randomUUID: () => "device-analysis-fixed" },
    );

    expect(result.analysis).toMatchObject({ analysis_mode: "device", state: "queued" });
    const create = calls.find((call) => call.url.endsWith("/analyses"));
    expect(JSON.parse(String(create?.init.body))).toEqual({
      schema_version: "1.0",
      analysis_mode: "device",
      device_id: DEVICE_ID,
      scenarios: ["cold_start", "scroll", "memory_cycle"],
      apk: {
        artifact_kind: "apk",
        mime: "application/vnd.android.package-archive",
        size: 3,
        sha256_b64: checksum,
      },
    });
    expect(new Headers(create?.init.headers).get("idempotency-key")).toBe(
      "device-analysis-fixed",
    );
    expect(calls.some((call) => call.url.startsWith("https://objects.example/"))).toBe(true);
    expect(calls.some((call) => call.url.endsWith("/finalize-upload"))).toBe(true);
  });

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
    const sourceBinding = {
      provider_kind: "agent_workspace" as const,
      agent_id: AGENT_ID,
      workspace_id: "92000000-0000-4000-8000-000000000001",
      snapshot_policy: "tracked_worktree" as const,
      validation_profile_id: null,
    };
    const sourceAwareAnalysis = (state: AnalysisResponse["state"]): AnalysisResponse => ({
      ...analysis(state),
      schema_version: "1.1",
      source_code_analysis: {
        requested: true,
        ...sourceBinding,
        context_state: "waiting_for_agent",
        match_summary: "none",
        verification_state: "not_requested",
        failure_code: null,
      },
    });
    const fetcher = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      calls.push({ url, init });
      if (url === "/api/v1/auth/csrf") {
        return Response.json({ schema_version: "1.0", csrf_token: "csrf-1" });
      }
      if (url === "/api/v1/me") {
        return Response.json({
          schema_version: "1.0",
          user: {
            id: "80000000-0000-4000-8000-000000000001",
            username: "ray_wu",
            is_platform_admin: true,
            must_change_password: false,
          },
          memberships: [
            { id: TEAM_ID, team: { id: TEAM_ID, name: "Ray" }, role: "owner" },
          ],
        });
      }
      if (url === `/api/v1/teams/${TEAM_ID}/analyses` && init.method === "POST") {
        return Response.json(sourceAwareAnalysis("created"), { status: 201 });
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
        return Response.json(
          sourceAwareAnalysis(statusReads === 1 ? "analyzing" : "completed"),
        );
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
        sourceBinding,
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
      schema_version: "1.1",
      analysis_mode: "trace_upload",
      analysis_profile: "scroll",
      question: "为什么掉帧？",
      inputs: [
        { kind: "trace", mime: "application/octet-stream", size: 3 },
        { kind: "mapping", mime: "text/plain", size: 2 },
      ],
      source_binding: sourceBinding,
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
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(valid))
      .mockResolvedValueOnce(
        Response.json({ ...valid, stages: [...valid.stages].reverse() }),
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
  });

  it("accepts not_requested only for device 1.1 memory_cycle", async () => {
    const counts = { valid: 0, invalid: 0, pending: 0, validation_error: 0, total: 0 };
    const scenario = (scenario_type: string, state: string) => ({
      scenario_job_id: state === "not_requested" ? null : "83000000-0000-4000-8000-000000000001",
      scenario_type,
      state,
      version: state === "not_requested" ? null : 1,
      device_group_id: null,
      sample_verdict_counts: counts,
      started_at: null,
      completed_at: null,
      failure: null,
    });
    const remote = {
      schema_version: "1.1",
      analysis_id: ANALYSIS_ID,
      team_id: TEAM_ID,
      analysis_mode: "device",
      device_id: DEVICE_ID,
      state: "queued",
      version: 1,
      application_version_id: null,
      application_metadata: null,
      apk_upload: {
        state: "pending", upload_id: "85000000-0000-4000-8000-000000000001",
        artifact_kind: "apk", mime: "application/vnd.android.package-archive",
        size: 1, sha256_b64: "c".repeat(44), expires_at: "2026-08-11T08:15:00Z",
      },
      scenarios: [
        scenario("cold_start", "queued"),
        scenario("scroll", "queued"),
        scenario("memory_cycle", "not_requested"),
      ],
      sample_verdict_counts: counts,
      active_lease: null,
      report_available: false,
      started_at: null,
      completed_at: null,
      failure: null,
      source_code_analysis: {
        requested: true, provider_kind: "agent_workspace", agent_id: AGENT_ID,
        workspace_id: "92000000-0000-4000-8000-000000000001",
        snapshot_policy: "tracked_worktree", validation_profile_id: null,
        context_state: "waiting_for_agent", match_summary: "none",
        verification_state: "not_requested", failure_code: null,
      },
    };
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(remote))
      .mockResolvedValueOnce(Response.json({ ...remote, schema_version: "1.0", source_code_analysis: undefined }))
      .mockResolvedValueOnce(Response.json({ ...remote, scenarios: [scenario("cold_start", "not_requested"), ...remote.scenarios.slice(1)] }))
      .mockResolvedValueOnce(Response.json({ ...remote, scenarios: [...remote.scenarios.slice(0, 2), { ...remote.scenarios[2], progress: 0 }] }))
      .mockResolvedValueOnce(Response.json({ ...remote, scenarios: [...remote.scenarios.slice(0, 2), { ...remote.scenarios[2], state: "skipped" }] }));
    const client = createPerfPilotClient({ fetcher });

    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      scenarios: [{ state: "queued" }, { state: "queued" }, { state: "not_requested" }],
    });
    for (let index = 0; index < 4; index += 1) {
      await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({ code: "invalid_api_response" });
    }
  });

  it("rejects private top-level fields for every analysis mode and schema", async () => {
    const sourceCodeAnalysis = {
      requested: true,
      provider_kind: "agent_workspace",
      agent_id: AGENT_ID,
      workspace_id: "92000000-0000-4000-8000-000000000001",
      snapshot_policy: "tracked_worktree",
      validation_profile_id: null,
      context_state: "waiting_for_agent",
      match_summary: "none",
      verification_state: "not_requested",
      failure_code: null,
    };
    const metadata = {
      package_name: "com.example.app",
      version_name: "1.0",
      version_code: 1,
      launch_activity: null,
      min_sdk: 26,
      target_sdk: 35,
      supported_abis: [],
      has_native_libraries: false,
    };
    const counts = { valid: 0, invalid: 0, pending: 0, validation_error: 0, total: 0 };
    const device = {
      schema_version: "1.0",
      analysis_id: ANALYSIS_ID,
      team_id: TEAM_ID,
      analysis_mode: "device",
      device_id: DEVICE_ID,
      state: "created",
      version: 1,
      application_version_id: null,
      application_metadata: metadata,
      apk_upload: {
        state: "pending",
        upload_id: "85000000-0000-4000-8000-000000000001",
        artifact_kind: "apk",
        mime: "application/vnd.android.package-archive",
        size: 3,
        sha256_b64: "c".repeat(44),
        expires_at: "2026-08-11T08:15:00Z",
      },
      scenarios: ["cold_start", "scroll", "memory_cycle"].map((scenario_type) => ({
        scenario_job_id: null,
        scenario_type,
        state: "awaiting_input",
        version: null,
        device_group_id: null,
        sample_verdict_counts: counts,
        started_at: null,
        completed_at: null,
        failure: null,
      })),
      sample_verdict_counts: counts,
      active_lease: null,
      report_available: false,
      started_at: null,
      completed_at: null,
      failure: null,
    };
    const memory = {
      schema_version: "1.0",
      analysis_id: ANALYSIS_ID,
      team_id: TEAM_ID,
      analysis_mode: "memory_upload",
      application_version_id: null,
      application_metadata: metadata,
      question: null,
      state: "created",
      version: 1,
      report_available: false,
      failure: null,
    };
    const variants = [analysis("completed"), device, memory].flatMap((payload) => [
      { ...payload, private_path: "/private/repo" },
      {
        ...payload,
        schema_version: "1.1",
        source_code_analysis: sourceCodeAnalysis,
        argv: ["git", "status"],
      },
    ]);
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () =>
      Response.json(variants.shift()),
    );
    const client = createPerfPilotClient({ fetcher });

    for (let index = 0; index < 6; index += 1) {
      await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
        code: "invalid_api_response",
      });
    }
  });

  it("accepts the exact single-pass and legacy AI round layouts", async () => {
    const valid = analysis("completed");
    const sourceAnalysis = {
      engine: "smartperfetto",
      rounds: 53,
      verification: "passed",
      session_id: "agent-session-1",
      run_id: "run-session-1",
    };
    const single = {
      ...valid,
      ai_rounds: [
        { round: 1, role: "report", state: "completed", attempts: 1 },
      ],
      source_analysis: sourceAnalysis,
    };
    const legacy = {
      ...valid,
      ai_rounds: [
        { round: 1, role: "extract", state: "completed", attempts: 1 },
        { round: 2, role: "review", state: "completed", attempts: 1 },
        { round: 3, role: "finalize", state: "completed", attempts: 1 },
      ],
      source_analysis: sourceAnalysis,
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(single))
      .mockResolvedValueOnce(Response.json(legacy));
    const client = createPerfPilotClient({ fetcher });

    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      ai_rounds: [
        { round: 1, role: "report", state: "completed", attempts: 1 },
      ],
    });
    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      ai_rounds: [
        { round: 1, role: "extract", state: "completed" },
        { round: 2, role: "review", state: "completed" },
        { round: 3, role: "finalize", state: "completed" },
      ],
      source_analysis: { engine: "smartperfetto", rounds: 53 },
    });
  });

  it("rejects a one-item extract layout and a reversed legacy layout", async () => {
    const valid = analysis("completed");
    const sourceAnalysis = {
      engine: "smartperfetto",
      rounds: 53,
      verification: "passed",
      session_id: "agent-session-1",
      run_id: "run-session-1",
    };
    const legacyRounds = [
      { round: 1, role: "extract", state: "completed", attempts: 1 },
      { round: 2, role: "review", state: "completed", attempts: 1 },
      { round: 3, role: "finalize", state: "completed", attempts: 1 },
    ];
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        Response.json({
          ...valid,
          ai_rounds: [legacyRounds[0]],
          source_analysis: sourceAnalysis,
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          ...valid,
          ai_rounds: [...legacyRounds].reverse(),
          source_analysis: sourceAnalysis,
        }),
      );
    const client = createPerfPilotClient({ fetcher });

    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
    await expect(client.analysis(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
  });

  it.each([
    ["extra keys", { round: 1, role: "report", state: "completed", attempts: 1, extra: true }],
    ["nonsequential rounds", { round: 2, role: "report", state: "completed", attempts: 1 }],
    ["unknown states", { round: 1, role: "report", state: "done", attempts: 1 }],
    ["negative attempts", { round: 1, role: "report", state: "completed", attempts: -1 }],
    ["fractional attempts", { round: 1, role: "report", state: "completed", attempts: 1.5 }],
    [
      "unsafe attempts",
      {
        round: 1,
        role: "report",
        state: "completed",
        attempts: Number.MAX_SAFE_INTEGER + 1,
      },
    ],
  ])("rejects AI rounds with %s", async (_case, aiRound) => {
    const response = {
      ...analysis("completed"),
      ai_rounds: [aiRound],
      source_analysis: {
        engine: "smartperfetto",
        rounds: 53,
        verification: "passed",
        session_id: "agent-session-1",
        run_id: "run-session-1",
      },
    };
    const client = createPerfPilotClient({
      fetcher: vi.fn<typeof fetch>().mockResolvedValue(Response.json(response)),
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

  it("normalizes a three-scenario device report without inventing AI output", async () => {
    const legacy = reportPayload();
    legacy.schema_version = "1.0";
    legacy.analysis_mode = "device";
    legacy.scenario_reports = ["cold_start", "scroll", "memory_cycle"].map(
      (scenario_type, index) => ({
        ...(legacy.scenario_reports as Array<Record<string, unknown>>)[0],
        scenario_job_id: `83000000-0000-4000-8000-00000000000${index + 1}`,
        scenario_type,
      }),
    );
    delete legacy.synthesis;
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(legacy));
    const client = createPerfPilotClient({ fetcher });

    await expect(client.report(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      schema_version: "1.0",
      analysis_mode: "device",
      synthesis: { state: "not_requested", output: null },
      scenario_reports: [
        { scenario_type: "cold_start" },
        { scenario_type: "scroll" },
        { scenario_type: "memory_cycle" },
      ],
    });
  });

  it("accepts a device AnalysisReport 1.1 with PerfPilot AI synthesis", async () => {
    const deviceReport = reportPayload();
    deviceReport.analysis_mode = "device";
    deviceReport.state = "partially_completed";
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(deviceReport));
    const client = createPerfPilotClient({ fetcher });

    await expect(client.report(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      schema_version: "1.1",
      analysis_mode: "device",
      state: "partially_completed",
      synthesis: { state: "completed" },
      scenario_reports: [{ scenario_type: "startup" }],
    });
  });

  it("accepts only the closed public AnalysisReport 1.2 document", async () => {
    const valid = sourceAwareReportPayload();
    const privateRoot = { ...sourceAwareReportPayload(), repo_url: "ssh://private/repo" };
    const privateSource = sourceAwareReportPayload();
    (privateSource.source_code as Record<string, unknown>).private_path = "/private/repo";
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(valid))
      .mockResolvedValueOnce(Response.json(privateRoot))
      .mockResolvedValueOnce(Response.json(privateSource));
    const client = createPerfPilotClient({ fetcher });

    await expect(client.report(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      schema_version: "1.2",
      synthesis: { state: "completed", output: { schema_version: "2.0" } },
      source_code: { requested: false, context_state: "not_requested" },
    });
    await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
    await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
  });

  it("loads a bounded SmartPerfetto original document and builds its same-origin download URL", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(
      Response.json({ summary: { conclusion: "原始结论" }, findings: [] }),
    );
    const client = createPerfPilotClient({ fetcher });

    await expect(client.smartPerfettoOriginal(TEAM_ID, ANALYSIS_ID)).resolves.toMatchObject({
      summary: { conclusion: "原始结论" },
    });
    expect(client.smartPerfettoOriginalDownloadUrl(TEAM_ID, ANALYSIS_ID)).toBe(
      `/api/v1/teams/${TEAM_ID}/analyses/${ANALYSIS_ID}/smartperfetto-original?download=true`,
    );
  });

  it("rejects source refs for none and fixes for weak source matches", async () => {
    const sourceRef = {
      source_ref_id: "95000000-0000-4000-8000-000000000001",
      relative_path: "app/src/main/java/demo/Startup.kt",
      language: "kotlin",
      symbol: null,
      start_line: 1,
      end_line: 2,
      content_sha256: "a".repeat(64),
      snapshot_hash: "b".repeat(64),
      match_grade: "none",
      finding_ids: [],
      evidence_ids: [],
      rule_ids: [],
    };
    const sourceBase = {
      requested: true,
      provider_kind: "agent_workspace",
      agent_id: AGENT_ID,
      workspace_id: "92000000-0000-4000-8000-000000000001",
      snapshot_policy: "tracked_worktree",
      validation_profile_id: "94000000-0000-4000-8000-000000000001",
      snapshot: {
        snapshot_id: "93000000-0000-4000-8000-000000000001",
        snapshot_hash: "b".repeat(64),
        git_head: "c".repeat(40),
      },
      context_state: "available",
      exclusions: [],
      limitations: [],
    };
    const none = sourceAwareReportPayload();
    none.source_code = {
      ...sourceBase,
      match_summary: "none",
      source_refs: [sourceRef],
      fixes: [],
    };
    const weak = sourceAwareReportPayload();
    weak.source_code = {
      ...sourceBase,
      match_summary: "weak",
      source_refs: [{ ...sourceRef, match_grade: "weak" }],
      fixes: [{
        fix_id: "96000000-0000-4000-8000-000000000001",
        finding_id: "85000000-0000-4000-8000-000000000001",
        evidence_ids: ["86000000-0000-4000-8000-000000000001"],
        recommendation_priority: "p1",
        source_ref_ids: [sourceRef.source_ref_id],
        rule_id: "android.ui.blocking_wait",
        match_grade: "strong",
        relative_path: sourceRef.relative_path,
        symbol: null,
        diagnosis: "blocking wait",
        diff: "--- a/Startup.kt\n+++ b/Startup.kt",
        validation_profile_id: "94000000-0000-4000-8000-000000000001",
        retest_target: "startup",
        verification: {
          state: "not_configured",
          exit_code: null,
          duration_ms: null,
          profile_id: "94000000-0000-4000-8000-000000000001",
          patch_sha256: "d".repeat(64),
          log_summary: null,
          patch_artifact: null,
        },
      }],
    };
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(none))
      .mockResolvedValueOnce(Response.json(weak));
    const client = createPerfPilotClient({ fetcher });

    await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
    await expect(client.report(TEAM_ID, ANALYSIS_ID)).rejects.toMatchObject({
      code: "invalid_api_response",
    });
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
