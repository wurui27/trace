// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createLatestReportLoader,
  LatestAnalysisReportEntry,
  type LatestReportLoader,
  type LatestReportSnapshot,
} from "../app/components/latest-analysis-report-entry";
import type {
  AnalysisListItem,
  AnalysisReport,
  PerfPilotClient,
} from "../app/lib/perfpilot-api";

afterEach(cleanup);

const analysis: AnalysisListItem = {
  schema_version: "1.0",
  analysis_id: "analysis-real-42",
  team_id: "team-real-1",
  analysis_mode: "trace_upload",
  analysis_profile: "startup",
  question: "首帧前为什么慢？",
  state: "completed",
  version: 8,
  report_available: true,
  created_at: "2026-08-04T08:00:00Z",
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
    session_id: "session-real-1",
    run_id: "run-real-1",
  },
};

const report = {
  schema_version: "1.1",
  analysis_id: analysis.analysis_id,
  analysis_mode: "trace_upload",
  state: "completed",
  report_version: 2,
  generated_at: "2026-08-04T08:10:00Z",
  scenario_reports: [],
  synthesis: {
    state: "completed",
    output: {
      schema_version: "1.0",
      executive_summary: "启动阶段的主线程等待是当前最明确的优化目标。",
      top_findings: [],
      recommendations: [],
      retest_plan: [],
      limitations: [],
    },
    synthesis_artifact_id: "artifact-real-1",
    failure_code: null,
    provenance: {
      provider_protocol: "chat-completions-json-schema-v1",
      provider_name: "local-provider",
      model: "local-model",
      prompt_template_version: "1.0.0",
      normalizer_version: "smartperfetto-normalizer-1",
      generated_at: "2026-08-04T08:10:00Z",
      prompt_tokens: 10,
      completion_tokens: 20,
      total_tokens: 30,
      generation: 1,
    },
  },
} satisfies AnalysisReport;

describe("LatestAnalysisReportEntry", () => {
  it("loads the current team list and its final report", async () => {
    const client = {
      csrf: vi.fn().mockResolvedValue("csrf-report"),
      me: vi.fn().mockResolvedValue({
        schema_version: "1.0",
        memberships: [{ team: { id: "team-real-1", name: "Ray" }, role: "owner" }],
      }),
      analyses: vi.fn().mockResolvedValue({
        schema_version: "1.0",
        analyses: [analysis],
      }),
      report: vi.fn().mockResolvedValue(report),
    } as unknown as PerfPilotClient;
    const controller = new AbortController();

    await expect(createLatestReportLoader(client)(controller.signal)).resolves.toEqual({
      teamId: "team-real-1",
      analysis,
      report,
    });
    expect(client.csrf).toHaveBeenCalledWith(controller.signal);
    expect(client.analyses).toHaveBeenCalledWith("team-real-1", 1, controller.signal);
    expect(client.report).toHaveBeenCalledWith(
      "team-real-1",
      analysis.analysis_id,
      controller.signal,
    );
  });

  it("renders a stable loading shell", () => {
    const loader: LatestReportLoader = vi.fn(
      () => new Promise<LatestReportSnapshot | null>(() => undefined),
    );

    render(<LatestAnalysisReportEntry loader={loader} />);

    expect(screen.getByRole("status")).toHaveTextContent("正在读取最新报告");
    expect(screen.getByText("最新分析报告")).toBeInTheDocument();
  });

  it("opens the real final report and shows engine metadata", async () => {
    const loader: LatestReportLoader = vi.fn().mockResolvedValue({
      teamId: analysis.team_id,
      analysis,
      report,
    });
    const onSnapshot = vi.fn();

    render(
      <LatestAnalysisReportEntry loader={loader} onSnapshot={onSnapshot} />,
    );

    const link = await screen.findByRole("link", { name: "打开报告" });
    expect(link).toHaveAttribute("href", "/analyses/analysis-real-42/report");
    expect(screen.getByText("首帧前为什么慢？")).toBeInTheDocument();
    expect(screen.getByText("完整报告")).toBeInTheDocument();
    expect(screen.getByText("SmartPerfetto 53 轮")).toBeInTheDocument();
    expect(screen.getByText("PerfPilot AI 3/3")).toBeInTheDocument();
    expect(screen.getByText("报告 v2")).toBeInTheDocument();
    expect(onSnapshot).toHaveBeenCalledWith({
      teamId: analysis.team_id,
      analysis,
      report,
    });
  });

  it("prompts a new analysis when no history exists", async () => {
    const { rerender } = render(
      <LatestAnalysisReportEntry loader={vi.fn().mockResolvedValue(null)} />,
    );
    expect(await screen.findByText("还没有分析数据")).toBeInTheDocument();
    expect(
      screen.getByText("新建一次分析后，真实的性能结论和最终报告会显示在这里。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建分析" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "打开报告" })).not.toBeInTheDocument();

    rerender(
      <LatestAnalysisReportEntry
        loader={vi.fn().mockRejectedValue(new Error("offline"))}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "暂时无法读取最新报告",
    );
    expect(screen.queryByRole("link", { name: "打开报告" })).not.toBeInTheDocument();
  });

  it("reloads the report when the dashboard refresh token changes", async () => {
    const loader: LatestReportLoader = vi.fn().mockResolvedValue(null);
    const view = render(
      <LatestAnalysisReportEntry loader={loader} refreshToken={0} />,
    );
    await screen.findByText("还没有分析数据");

    view.rerender(
      <LatestAnalysisReportEntry loader={loader} refreshToken={1} />,
    );

    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("aborts the request when the dashboard leaves the page", () => {
    const requestSignals: AbortSignal[] = [];
    const loader: LatestReportLoader = vi.fn((signal: AbortSignal) => {
      requestSignals.push(signal);
      return new Promise<LatestReportSnapshot | null>(() => undefined);
    });

    const view = render(<LatestAnalysisReportEntry loader={loader} />);
    view.unmount();

    expect(requestSignals[0]?.aborted).toBe(true);
  });
});
