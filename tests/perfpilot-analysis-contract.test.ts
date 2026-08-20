import { describe, expect, it, vi } from "vitest";

import {
  createPerfPilotClient,
  PerfPilotApiError,
  type AnalysisResponse,
} from "../app/lib/perfpilot-api";

const TEAM_ID = "81000000-0000-4000-8000-000000000001";
const ANALYSIS_ID = "82000000-0000-4000-8000-000000000001";

function traceResponse(version: "1.0" | "1.1" | "1.3"): Record<string, unknown> {
  const response: Record<string, unknown> = {
    schema_version: version,
    analysis_id: ANALYSIS_ID,
    team_id: TEAM_ID,
    analysis_mode: "trace_upload",
    analysis_profile: "startup",
    test_type: "cold_start",
    package_name: "com.rivotek.mediacenter",
    custom_test_name: null,
    custom_test_description: null,
    question: null,
    state: "analyzing",
    version: 2,
    created_at: "2026-08-20T08:00:00Z",
    cancel_requested_at: null,
    report_available: false,
    input_uploads: [],
    stages: [
      { stage: "input_validation", state: "completed", failure: null },
      { stage: "smartperfetto", state: "running", failure: null },
      { stage: "perfpilot_ai", state: "pending", failure: null },
      { stage: "report", state: "pending", failure: null },
    ],
    ai_rounds: [{ round: 1, role: "report", state: "pending", attempts: 0 }],
    source_analysis: {
      engine: "smartperfetto",
      rounds: null,
      verification: "unknown",
      session_id: null,
      run_id: null,
    },
    failure: null,
  };
  if (version !== "1.0") {
    response.source_code_analysis = {
      requested: version === "1.3",
      provider_kind: version === "1.3" ? "agent_workspace" : null,
      agent_id: version === "1.3" ? "71000000-0000-4000-8000-000000000001" : null,
      workspace_id: version === "1.3" ? "72000000-0000-4000-8000-000000000001" : null,
      snapshot_policy: version === "1.3" ? "tracked_worktree" : null,
      validation_profile_id: null,
      context_state: version === "1.3" ? "extracting" : "not_requested",
      match_summary: "none",
      verification_state: version === "1.3" ? "pending" : "not_requested",
      failure_code: null,
    };
  }
  if (version === "1.3") {
    response.runtime_status = {
      current_stage: "source_code",
      stage_state: "running",
      started_at: "2026-08-20T08:00:00Z",
      updated_at: "2026-08-20T08:01:00Z",
      last_progress_at: "2026-08-20T08:01:00Z",
      attempt: 1,
      max_attempts: 2,
      generation: 1,
      waiting_for: null,
      progress_summary: "正在读取并匹配源码",
      available_actions: ["cancel"],
    };
  }
  return response;
}

async function parse(payload: Record<string, unknown>): Promise<AnalysisResponse> {
  const fetcher = vi.fn(async () =>
    new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  return createPerfPilotClient({ fetcher }).analysis(TEAM_ID, ANALYSIS_ID);
}

describe("analysis contract characterization", () => {
  it.each(["1.0", "1.1", "1.3"] as const)("keeps schema %s readable", async (version) => {
    await expect(parse(traceResponse(version))).resolves.toMatchObject({
      schema_version: version,
      analysis_id: ANALYSIS_ID,
      team_id: TEAM_ID,
    });
  });

  it.each([
    ["unknown key", (value: Record<string, unknown>) => ({ ...value, private_path: "/tmp/x" })],
    ["missing runtime", (value: Record<string, unknown>) => {
      const copy = { ...value };
      delete copy.runtime_status;
      return copy;
    }],
    ["unknown action", (value: Record<string, unknown>) => ({
      ...value,
      runtime_status: {
        ...(value.runtime_status as Record<string, unknown>),
        available_actions: ["restart"],
      },
    })],
  ])("rejects %s with the stable client error", async (_name, mutate) => {
    await expect(parse(mutate(traceResponse("1.3")))).rejects.toMatchObject<PerfPilotApiError>({
      code: "invalid_api_response",
      message: "服务返回内容无效",
    });
  });
});
