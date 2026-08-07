// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FullAnalysisReport } from "../app/components/full-analysis-report";
import type {
  AnalysisReport,
  AnalysisResponse,
} from "../app/lib/perfpilot-api";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

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
    { round: 1, role: "report", state: "completed", attempts: 1 },
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
  it("offers an enabled PDF download after detecting print support", async () => {
    const printer = vi.fn(() => true);
    const loader = vi.fn(async (_id, _signal, onSnapshot) => {
      onSnapshot({ teamId: "team-1", analysis, report, reportLoadFailed: false });
    });
    const print = window.print;
    Object.defineProperty(window, "print", { configurable: true, value: vi.fn() });

    render(<FullAnalysisReport analysisId="analysis-live-1" loader={loader} printer={printer} />);
    const button = await screen.findByRole("button", { name: "下载 PDF" });

    expect(button).toBeEnabled();
    await userEvent.setup().click(button);
    expect(printer).toHaveBeenCalledTimes(1);
    expect(printer).toHaveBeenCalledWith("analysis-live-1");
    expect(window.print).not.toHaveBeenCalled();
    Object.defineProperty(window, "print", { configurable: true, value: print });
  });

  it("shows a disabled PDF action and guidance when printing is unavailable", async () => {
    const print = window.print;
    Object.defineProperty(window, "print", { configurable: true, value: undefined });
    const loader = vi.fn(async (_id, _signal, onSnapshot) => {
      onSnapshot({ teamId: "team-1", analysis, report, reportLoadFailed: false });
    });

    render(<FullAnalysisReport analysisId="analysis-live-1" loader={loader} />);

    expect(await screen.findByRole("button", { name: "下载 PDF" })).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "当前浏览器不支持打印，请使用浏览器菜单保存报告。",
    );
    Object.defineProperty(window, "print", { configurable: true, value: print });
  });

  it("does not offer PDF download while loading, failing, or without a report", async () => {
    const pendingLoader = vi.fn(() => new Promise<void>(() => {}));
    const { unmount } = render(<FullAnalysisReport analysisId="analysis-live-1" loader={pendingLoader} />);
    expect(screen.queryByRole("button", { name: "下载 PDF" })).not.toBeInTheDocument();
    unmount();

    const failedLoader = vi.fn(async () => {
      throw new Error("unavailable");
    });
    const { unmount: unmountFailed } = render(
      <FullAnalysisReport analysisId="analysis-live-1" loader={failedLoader} />,
    );
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(screen.queryByRole("button", { name: "下载 PDF" })).not.toBeInTheDocument();
    unmountFailed();

    const noReportLoader = vi.fn(async (_id, _signal, onSnapshot) => {
      onSnapshot({ teamId: "team-1", analysis, report: null, reportLoadFailed: false });
    });
    render(<FullAnalysisReport analysisId="analysis-live-1" loader={noReportLoader} />);
    expect(await screen.findByText("最终报告尚未生成")).toBeVisible();
    expect(screen.queryByRole("button", { name: "下载 PDF" })).not.toBeInTheDocument();
  });

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
    expect(screen.getByText("单轮 PerfPilot AI 深度分析已完成")).toBeVisible();
    expect(screen.getByText("证据核验、归因、建议与复测计划")).toBeVisible();
    expect(screen.getByRole("heading", { name: "优化建议" })).toBeVisible();
    expect(screen.getByRole("link", { name: "返回分析进度" })).toHaveAttribute(
      "href",
      "/analyses/analysis-live-1",
    );
  });

  it("preserves the completed legacy three-round process copy", async () => {
    const legacyAnalysis: AnalysisResponse = {
      ...analysis,
      ai_rounds: [
        { round: 1, role: "extract", state: "completed", attempts: 1 },
        { round: 2, role: "review", state: "completed", attempts: 1 },
        { round: 3, role: "finalize", state: "completed", attempts: 1 },
      ],
    };
    const loader = vi.fn(async (_id, _signal, onSnapshot) => {
      onSnapshot({
        teamId: "team-1",
        analysis: legacyAnalysis,
        report,
        reportLoadFailed: false,
      });
    });

    render(<FullAnalysisReport analysisId="analysis-live-1" loader={loader} />);

    expect(await screen.findByText("3 轮 PerfPilot AI 已完成")).toBeVisible();
    expect(screen.getByText("提取、复核、定稿")).toBeVisible();
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

  it("uses the LAN-compatible UUID factory for the default AI retry key", async () => {
    const user = userEvent.setup();
    const nativeRandomUuid = vi
      .spyOn(globalThis.crypto, "randomUUID")
      .mockReturnValue("00000000-0000-4000-8000-000000000000");
    const partialAnalysis: AnalysisResponse = {
      ...analysis,
      state: "partially_completed",
      stages: analysis.stages.map((stage) =>
        stage.stage === "perfpilot_ai"
          ? {
              ...stage,
              state: "failed",
              failure: {
                code: "synthesis_unavailable",
                message: "AI 建议生成失败",
                retryable: true,
              },
            }
          : stage,
      ),
    };
    const partialReport: AnalysisReport = {
      ...report,
      state: "partially_completed",
      synthesis: {
        state: "failed",
        output: null,
        synthesis_artifact_id: null,
        failure_code: "synthesis_unavailable",
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
    const rerunner = vi
      .fn<
        (
          teamId: string,
          analysisId: string,
          idempotencyKey: string,
          signal: AbortSignal,
        ) => Promise<void>
      >()
      .mockResolvedValue(undefined);

    render(
      <FullAnalysisReport
        analysisId="analysis-live-1"
        loader={loader}
        rerunner={rerunner}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "重新生成 AI 建议" }));

    expect(rerunner).toHaveBeenCalledOnce();
    expect(rerunner.mock.calls[0]?.[2]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(nativeRandomUuid).not.toHaveBeenCalled();
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
