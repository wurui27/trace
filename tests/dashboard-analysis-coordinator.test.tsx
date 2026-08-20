// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ComponentType } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "../app/components/dashboard";
import type { LatestReportLoader } from "../app/components/latest-analysis-report-entry";
import type {
  AnalysisListItem,
  AnalysisReport,
  AnalysisResponse,
  PerfPilotClient,
} from "../app/lib/perfpilot-api";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const active: AnalysisListItem = {
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
};

const completed: AnalysisResponse = {
  ...active,
  state: "completed",
  version: 8,
  report_available: true,
  stages: active.stages.map((stage) => ({ ...stage, state: "completed" })),
};

const latestAnalysis: AnalysisListItem = {
  ...completed,
  created_at: active.created_at,
};

const latestReport: AnalysisReport = {
  schema_version: "1.1",
  analysis_id: latestAnalysis.analysis_id,
  analysis_mode: "trace_upload",
  state: "partially_completed",
  report_version: 1,
  generated_at: "2026-08-04T08:05:00Z",
  scenario_reports: [
    {
      scenario_job_id: "scenario-1",
      scenario_type: "startup",
      result_state: "completed",
      device_group_id: null,
      device_group_reason: null,
      bundle: {
        metrics: [
          {
            metric_id: "metric-startup",
            name: "startup.startup_analysis_get_startups.dur_ms",
            status: "available",
            numeric_value: 481.772461,
            unit: "ms",
            definition: "启动耗时",
            threshold: null,
          },
          {
            metric_id: "metric-main",
            name: "startup.startup_analysis_main_thread_slices.total_dur_ms",
            status: "available",
            numeric_value: 215.5005,
            unit: "ms",
            definition: "主线程总耗时",
            threshold: null,
          },
        ],
        findings: [
          {
            finding_id: "finding-1",
            title: "Compose 首帧重组过重",
            summary: "主线程存在 61ms 的重组热点。",
            severity: "critical",
            confidence: "high",
            evidence_ids: ["evidence-1"],
          },
        ],
        evidence: [
          {
            evidence_id: "evidence-1",
            source: "perfetto.startup",
            query_id: "query-1",
            interval_start_ns: 1,
            interval_end_ns: 2,
            fields: {},
          },
        ],
      },
      failure: null,
    },
  ],
  synthesis: {
    state: "failed",
    output: null,
    synthesis_artifact_id: null,
    failure_code: "ai_not_configured",
    provenance: null,
  },
};

interface TestDashboardProps {
  readonly client: PerfPilotClient;
  readonly pollDelay: (milliseconds: number, signal: AbortSignal) => Promise<void>;
  readonly latestReportLoader: LatestReportLoader;
  readonly confirmCancel?: () => boolean;
}

const TestDashboard = Dashboard as ComponentType<TestDashboardProps>;

function clientWithActive(
  overrides: Partial<PerfPilotClient> = {},
): PerfPilotClient {
  return {
    csrf: vi.fn().mockResolvedValue("csrf-1"),
    me: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      memberships: [{ team: { id: "team-1", name: "Ray" }, role: "owner" }],
    }),
    activeAnalyses: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      analyses: [active],
    }),
    analysis: vi.fn().mockResolvedValue(completed),
    cancelAnalysis: vi.fn(),
    ...overrides,
  } as unknown as PerfPilotClient;
}

describe("Dashboard analysis coordinator", () => {
  it("shows degraded health without disabling available analysis entry", async () => {
    const client = clientWithActive({
      readiness: vi.fn().mockResolvedValue({
        schema_version: "1.0",
        state: "degraded",
        capabilities: [
          {
            name: "agent",
            state: "unavailable",
            message: "没有在线 Agent",
            last_checked_at: "2026-08-20T08:00:00+00:00",
          },
        ],
      }),
      activeAnalyses: vi.fn().mockResolvedValue({ schema_version: "1.0", analyses: [] }),
    });
    render(
      <TestDashboard
        client={client}
        pollDelay={() => new Promise<void>(() => undefined)}
        latestReportLoader={vi.fn().mockResolvedValue(null)}
      />,
    );

    expect(await screen.findByText("部分分析能力暂不可用")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "新建分析" })).toBeEnabled();
  });

  it("fills the dashboard result slots from the latest real report", async () => {
    const client = clientWithActive({
      activeAnalyses: vi.fn().mockResolvedValue({
        schema_version: "1.0",
        analyses: [],
      }),
    });
    const latestReportLoader: LatestReportLoader = vi.fn().mockResolvedValue({
      teamId: "team-1",
      analysis: latestAnalysis,
      report: latestReport,
    });

    render(
      <TestDashboard
        client={client}
        pollDelay={() => new Promise<void>(() => undefined)}
        latestReportLoader={latestReportLoader}
      />,
    );

    expect(
      (await screen.findAllByText("Compose 首帧重组过重")).length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("481.8 ms")).toBeInTheDocument();
    expect(screen.getByText("215.5 ms")).toBeInTheDocument();
    expect(screen.getByText("AI 最终报告 未完成")).toBeInTheDocument();
    expect(screen.queryByText("AI 建议 未完成")).not.toBeInTheDocument();
    expect(screen.getByText("PerfPilot AI 未完成")).toBeInTheDocument();
    expect(screen.queryByText("暂无分析结论")).not.toBeInTheDocument();
  });

  it("uses final-report language in the empty credibility slot", async () => {
    const client = clientWithActive({
      activeAnalyses: vi.fn().mockResolvedValue({
        schema_version: "1.0",
        analyses: [],
      }),
    });

    render(
      <TestDashboard
        client={client}
        pollDelay={() => new Promise<void>(() => undefined)}
        latestReportLoader={vi.fn().mockResolvedValue(null)}
      />,
    );

    expect(await screen.findByText("AI 最终报告 —")).toBeInTheDocument();
    expect(screen.queryByText("AI 建议 —")).not.toBeInTheDocument();
  });

  it("restores the active task and refreshes reports when polling reaches completion", async () => {
    let releasePoll: (() => void) | undefined;
    const pollDelay = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          releasePoll = resolve;
        }),
    );
    const client = clientWithActive();
    const latestReportLoader: LatestReportLoader = vi.fn().mockResolvedValue(null);

    render(
      <TestDashboard
        client={client}
        pollDelay={pollDelay}
        latestReportLoader={latestReportLoader}
      />,
    );

    expect(await screen.findByText("SmartPerfetto 正在解析 Trace")).toBeInTheDocument();
    expect(client.activeAnalyses).toHaveBeenCalledWith(
      "team-1",
      1,
      expect.any(AbortSignal),
    );
    releasePoll?.();

    expect(await screen.findByText("分析完成")).toHaveClass("is-success");
    await waitFor(() => expect(latestReportLoader).toHaveBeenCalledTimes(2));
  });

  it("confirms and displays cancellation only after the backend returns canceled", async () => {
    const canceled: AnalysisResponse = {
      ...active,
      state: "canceled",
      cancel_requested_at: "2026-08-04T08:03:00Z",
      stages: active.stages.map((stage) =>
        stage.state === "completed" ? stage : { ...stage, state: "canceled" },
      ),
    };
    const client = clientWithActive({
      cancelAnalysis: vi.fn().mockResolvedValue(canceled),
    });
    const confirmCancel = vi.fn(() => true);

    render(
      <TestDashboard
        client={client}
        pollDelay={() => new Promise<void>(() => undefined)}
        latestReportLoader={vi.fn().mockResolvedValue(null)}
        confirmCancel={confirmCancel}
      />,
    );

    const cancelButton = await screen.findByRole("button", { name: "取消分析" });
    vi.useFakeTimers();
    fireEvent.click(cancelButton);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(confirmCancel).toHaveBeenCalledOnce();
    expect(client.cancelAnalysis).toHaveBeenCalledWith(
      "team-1",
      "analysis-active-1",
      expect.any(AbortSignal),
    );
    expect(screen.getByText("分析已取消")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(3_000));
    expect(screen.getByText("分析已取消")).toBeInTheDocument();
    vi.useRealTimers();
  });
});
