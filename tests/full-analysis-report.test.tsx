// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FullAnalysisReport } from "../app/components/full-analysis-report";
import type {
  AnalysisReport,
  AnalysisResponse,
} from "../app/lib/perfpilot-api";

afterEach(cleanup);

const analysis: AnalysisResponse = {
  schema_version: "1.0",
  analysis_id: "analysis-live-1",
  team_id: "team-1",
  analysis_mode: "trace_upload",
  analysis_profile: "startup",
  question: "启动为什么慢？",
  state: "completed",
  version: 9,
  report_available: true,
  input_uploads: [],
  stages: [
    { stage: "input_validation", state: "completed", failure: null },
    { stage: "smartperfetto", state: "completed", failure: null },
    { stage: "perfpilot_ai", state: "completed", failure: null },
    { stage: "report", state: "completed", failure: null },
  ],
  failure: null,
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

const report: AnalysisReport = {
  schema_version: "1.1",
  analysis_id: "analysis-live-1",
  analysis_mode: "trace_upload",
  state: "completed",
  report_version: 1,
  generated_at: "2026-08-04T08:00:00Z",
  scenario_reports: [
    {
      scenario_job_id: "scenario-1",
      scenario_type: "startup",
      result_state: "completed",
      device_group_id: null,
      device_group_reason: "not_applicable",
      bundle: { metrics: [], findings: [], evidence: [] },
      failure: null,
    },
  ],
  synthesis: {
    state: "completed",
    output: {
      schema_version: "1.0",
      executive_summary: "启动关键路径存在可优化工作。",
      top_findings: [],
      recommendations: [
        {
          priority: "p1",
          title: "延迟非关键初始化",
          action: "把非关键初始化移到首帧之后。",
          expected_effect: "缩短启动关键路径。",
          finding_ids: [],
          evidence_ids: [],
        },
      ],
      retest_plan: [],
      limitations: [],
    },
    synthesis_artifact_id: "synthesis-1",
    failure_code: null,
    provenance: {
      provider_protocol: "openai-compatible",
      provider_name: "local-deepseek",
      model: "deepseek-v4-pro",
      prompt_template_version: "perfpilot-local-multiround-v1",
      normalizer_version: "smartperfetto-live-normalizer-1",
      generated_at: "2026-08-04T08:00:00Z",
      prompt_tokens: 10,
      completion_tokens: 20,
      total_tokens: 30,
      generation: 1,
    },
  },
};

describe("FullAnalysisReport", () => {
  it("renders the final report with source and AI process context", async () => {
    const loader = vi.fn(async (_id, _signal, onSnapshot) => {
      onSnapshot({
        teamId: "team-1",
        analysis,
        report,
        reportLoadFailed: false,
      });
    });

    render(<FullAnalysisReport analysisId="analysis-live-1" loader={loader} />);

    expect(await screen.findByRole("heading", { name: "最终性能报告" })).toBeVisible();
    expect(screen.getByText("53 轮 SmartPerfetto 分析")).toBeVisible();
    expect(screen.getByText("3 轮 PerfPilot AI 已完成")).toBeVisible();
    expect(screen.getByRole("heading", { name: "优化建议" })).toBeVisible();
    expect(screen.getByRole("link", { name: "返回分析进度" })).toHaveAttribute(
      "href",
      "/analyses/analysis-live-1",
    );
  });

  it("does not describe a failed AI stage as completed", async () => {
    const partialAnalysis: AnalysisResponse = {
      ...analysis,
      state: "partially_completed",
      stages: analysis.stages.map((stage) =>
        stage.stage === "perfpilot_ai"
          ? {
              ...stage,
              state: "failed",
              failure: {
                code: "ai_projection_private_data",
                message: "AI 投影已阻止",
                retryable: false,
              },
            }
          : stage,
      ),
      ai_rounds: analysis.ai_rounds?.map((round) => ({
        ...round,
        state: "pending",
        attempts: 0,
      })),
    };
    const partialReport: AnalysisReport = {
      ...report,
      state: "partially_completed",
      synthesis: {
        state: "failed",
        output: null,
        synthesis_artifact_id: null,
        failure_code: "ai_projection_private_data",
        provenance: null,
      },
    };
    const loader = vi.fn(async (_id, _signal, onSnapshot) => {
      onSnapshot({
        teamId: "team-1",
        analysis: partialAnalysis,
        report: partialReport,
        reportLoadFailed: false,
      });
    });

    render(<FullAnalysisReport analysisId="analysis-live-1" loader={loader} />);

    expect(await screen.findByText("PerfPilot AI 未完成")).toBeVisible();
    expect(screen.queryByText("0 轮 PerfPilot AI 已完成")).not.toBeInTheDocument();
  });

  it("describes the joined kernels for a device report", async () => {
    const deviceAnalysis: AnalysisResponse = {
      ...analysis,
      analysis_mode: "device",
    };
    const deviceReport: AnalysisReport = {
      ...report,
      analysis_mode: "device",
      scenario_reports: [
        ...report.scenario_reports,
        {
          scenario_job_id: "scenario-memory-1",
          scenario_type: "memory_cycle",
          result_state: "completed",
          device_group_id: null,
          device_group_reason: "not_applicable",
          bundle: {
            metrics: [],
            findings: [],
            evidence: [],
          },
          failure: null,
        },
      ],
    };
    const loader = vi.fn(async (_id, _signal, onSnapshot) => {
      onSnapshot({
        teamId: "team-1",
        analysis: deviceAnalysis,
        report: deviceReport,
        reportLoadFailed: false,
      });
    });

    render(<FullAnalysisReport analysisId="analysis-live-1" loader={loader} />);

    expect(
      await screen.findByText(
        "SmartPerfetto 提供 Trace 证据，Android Memory 提供内存采集事实，PerfPilot AI 负责复核、归纳并生成最终优化方案。",
      ),
    ).toBeVisible();
    expect(screen.getByText("SmartPerfetto 53 轮，Android Memory 已汇聚")).toBeVisible();
    expect(screen.getByText("2 个真机场景")).toBeVisible();
  });
});
