// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ActiveAnalysisTaskCard,
  activeAnalysisStageLabel,
} from "../app/components/active-analysis-task-card";
import type { AnalysisResponse } from "../app/lib/perfpilot-api";

afterEach(cleanup);

function activeAnalysis(): AnalysisResponse {
  return {
    schema_version: "1.0",
    analysis_id: "analysis-active-1",
    team_id: "team-1",
    analysis_mode: "trace_upload",
    analysis_profile: "startup",
    question: "首帧为什么慢？",
    state: "analyzing",
    version: 4,
    created_at: "2026-08-04T08:00:00Z",
    cancel_requested_at: null,
    report_available: false,
    input_uploads: [],
    stages: [
      { stage: "input_validation", state: "completed", failure: null },
      { stage: "smartperfetto", state: "running", failure: null },
      { stage: "perfpilot_ai", state: "pending", failure: null },
      { stage: "report", state: "pending", failure: null },
    ],
    failure: null,
    ai_rounds: [
      { round: 1, role: "extract", state: "pending", attempts: 0 },
      { round: 2, role: "review", state: "pending", attempts: 0 },
      { round: 3, role: "finalize", state: "pending", attempts: 0 },
    ],
    source_analysis: {
      engine: "smartperfetto",
      rounds: null,
      verification: "unknown",
      session_id: "session-1",
      run_id: "run-1",
    },
  };
}

function activeDeviceAnalysis(): AnalysisResponse {
  return {
    schema_version: "1.0",
    analysis_id: "analysis-device-1",
    team_id: "team-1",
    analysis_mode: "device",
    device_id: "device-1",
    state: "queued",
    version: 4,
    application_version_id: null,
    application_metadata: null,
    apk_upload: null,
    scenarios: [
      {
        scenario_job_id: null,
        scenario_type: "cold_start",
        state: "queued",
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
      },
      {
        scenario_job_id: null,
        scenario_type: "scroll",
        state: "queued",
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
      },
      {
        scenario_job_id: null,
        scenario_type: "memory_cycle",
        state: "not_requested",
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
      },
    ],
    sample_verdict_counts: {
      valid: 0,
      invalid: 0,
      pending: 0,
      validation_error: 0,
      total: 0,
    },
    active_lease: null,
    report_available: false,
    created_at: "2026-08-04T08:00:00Z",
    started_at: null,
    completed_at: null,
    cancel_requested_at: null,
    failure: null,
    input_uploads: [],
    stages: [],
  };
}

describe("ActiveAnalysisTaskCard", () => {
  it("shows the real stage, elapsed time, honest estimate and actions", async () => {
    const user = userEvent.setup();
    const cancel = vi.fn();
    render(
      <ActiveAnalysisTaskCard
        analysis={activeAnalysis()}
        now={new Date("2026-08-04T08:02:00Z").valueOf()}
        canceling={false}
        stale={false}
        onCancel={cancel}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在分析");
    expect(screen.getByText("SmartPerfetto 正在解析 Trace")).toBeInTheDocument();
    expect(screen.getByText("已用时 2 分钟")).toBeInTheDocument();
    expect(
      screen.getByText("通常需要 3-8 分钟，复杂任务可能更久"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看详情" })).toHaveAttribute(
      "href",
      "/analyses/analysis-active-1",
    );
    await user.click(screen.getByRole("button", { name: "取消分析" }));
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("shows Agent dispatch and all fixed device scenarios", () => {
    render(
      <ActiveAnalysisTaskCard
        analysis={activeDeviceAnalysis()}
        now={new Date("2026-08-04T08:01:00Z").valueOf()}
        canceling={false}
        stale={false}
        onCancel={vi.fn()}
      />,
    );

    expect(screen.getByText("等待设备 Agent 接收任务")).toBeInTheDocument();
    expect(screen.getByText("冷启动采集")).toBeInTheDocument();
    expect(screen.getByText("滑动采集")).toBeInTheDocument();
    expect(screen.getByText("内存分析暂未执行")).toBeInTheDocument();
    expect(screen.getByText("内存分析暂未执行").closest("li")).toHaveClass(
      "is-not_requested",
    );
    expect(screen.getByText("内存分析暂未执行").closest("li")).not.toHaveClass(
      "is-failed",
    );
  });

  it("shows a truthful single-pass final-report label", () => {
    const ai: AnalysisResponse = {
      ...activeAnalysis(),
      stages: [
        { stage: "input_validation", state: "completed", failure: null },
        { stage: "smartperfetto", state: "completed", failure: null },
        { stage: "perfpilot_ai", state: "running", failure: null },
        { stage: "report", state: "pending", failure: null },
      ],
      ai_rounds: [
        { round: 1, role: "report", state: "running", attempts: 1 },
      ],
    };

    expect(activeAnalysisStageLabel(ai)).toBe(
      "PerfPilot AI 正在生成最终报告",
    );
  });

  it("shows the optional source context stage without affecting Trace-only work", () => {
    const sourceAware: AnalysisResponse = {
      ...activeAnalysis(),
      schema_version: "1.1",
      stages: [
        { stage: "input_validation", state: "completed", failure: null },
        { stage: "smartperfetto", state: "completed", failure: null },
        { stage: "perfpilot_ai", state: "pending", failure: null },
        { stage: "report", state: "pending", failure: null },
      ],
      source_code_analysis: {
        requested: true,
        provider_kind: "agent_workspace",
        agent_id: "73000000-0000-4000-8000-000000000001",
        workspace_id: "92000000-0000-4000-8000-000000000001",
        snapshot_policy: "tracked_worktree",
        validation_profile_id: null,
        context_state: "extracting",
        match_summary: "none",
        verification_state: "not_requested",
        failure_code: null,
      },
    };

    expect(activeAnalysisStageLabel(sourceAware)).toBe("正在读取源码上下文");
    expect(
      activeAnalysisStageLabel({
        ...sourceAware,
        source_code_analysis: {
          ...sourceAware.source_code_analysis!,
          context_state: "available",
          match_summary: "strong",
          verification_state: "validating",
        },
      }),
    ).toBe("正在验证源码修复");
    expect(
      activeAnalysisStageLabel({
        ...sourceAware,
        schema_version: "1.0",
        source_code_analysis: undefined,
      }),
    ).toBe("等待分析资源");
  });

  it("preserves the legacy running round and cancel labels", () => {
    const ai: AnalysisResponse = {
      ...activeAnalysis(),
      stages: [
        { stage: "input_validation", state: "completed", failure: null },
        { stage: "smartperfetto", state: "completed", failure: null },
        { stage: "perfpilot_ai", state: "running", failure: null },
        { stage: "report", state: "pending", failure: null },
      ],
      ai_rounds: [
        { round: 1, role: "extract", state: "completed", attempts: 1 },
        { round: 2, role: "review", state: "running", attempts: 1 },
        { round: 3, role: "finalize", state: "pending", attempts: 0 },
      ],
    };
    expect(activeAnalysisStageLabel(ai)).toBe("PerfPilot AI 第 2/3 轮");
    expect(
      activeAnalysisStageLabel({
        ...ai,
        cancel_requested_at: "2026-08-04T08:03:00Z",
      }),
    ).toBe("正在取消分析");
  });
});
