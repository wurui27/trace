// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AnalysisProgress,
  AnalysisProgressView,
  createAnalysisLoader,
} from "../app/components/analysis-progress";
import type {
  AnalysisReport,
  AnalysisResponse,
  AnalysisStage,
  AnalysisState,
  PerfPilotClient,
} from "../app/lib/perfpilot-api";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function analysis(
  state: AnalysisState,
  analysisId = "analysis-live-1",
  stages: readonly AnalysisStage[] = stageList(state),
): AnalysisResponse {
  return {
    schema_version: "1.0",
    analysis_id: analysisId,
    team_id: "team-1",
    analysis_mode: "trace_upload",
    analysis_profile: "auto",
    question: null,
    state,
    version: 3,
    report_available: state === "completed",
    input_uploads: [
      {
        state: state === "created" ? "awaiting_upload" : "finalized",
        artifact_kind: "trace",
        mime: "application/octet-stream",
        size: 4096,
        sha256_b64: "A".repeat(43) + "=",
      },
    ],
    stages,
    failure:
      state === "failed"
        ? { code: "engine_failed", message: "任务未能完成", retryable: false }
        : null,
  };
}

function stageList(state: AnalysisState): readonly AnalysisStage[] {
  const finished = ["completed", "partially_completed", "failed", "canceled"].includes(state);
  return [
    { stage: "input_validation", state: "completed", failure: null },
    { stage: "smartperfetto", state: finished ? "completed" : "running", failure: null },
    {
      stage: "perfpilot_ai",
      state: state === "completed" ? "completed" : finished ? "failed" : "pending",
      failure:
        state === "failed"
          ? { code: "synthesis_failed", message: "AI 建议生成失败", retryable: true }
          : null,
    },
    {
      stage: "report",
      state: state === "completed" ? "completed" : finished ? "completed" : "pending",
      failure: null,
    },
  ];
}

function failedReport(): AnalysisReport {
  return {
    schema_version: "1.1",
    analysis_id: "analysis-live-1",
    analysis_mode: "trace_upload",
    state: "partially_completed",
    report_version: 1,
    generated_at: "2026-08-04T08:00:00Z",
    scenario_reports: [],
    synthesis: {
      state: "failed",
      output: null,
      synthesis_artifact_id: null,
      failure_code: "synthesis_unavailable",
      provenance: null,
    },
  };
}

describe("AnalysisProgress", () => {
  it.each([
    ["created", "等待上传 Trace"],
    ["uploading", "正在接收分析文件"],
    ["analyzing", "SmartPerfetto 正在分析"],
    ["completed", "分析完成"],
    ["partially_completed", "分析完成"],
    ["failed", "分析未能完成"],
    ["canceled", "分析已取消"],
  ] as const)("renders the real %s state", (state, label) => {
    render(<AnalysisProgressView analysis={analysis(state)} />);

    expect(screen.getByRole("heading", { name: label })).toBeInTheDocument();
    expect(screen.getByText("Trace")).toBeInTheDocument();
    expect(screen.getByText("analysis-live-1")).toBeInTheDocument();
  });

  it("presents a partially completed report as a green completed analysis", () => {
    render(<AnalysisProgressView analysis={analysis("partially_completed")} />);

    expect(screen.getByRole("heading", { name: "分析完成" })).toHaveClass("is-success");
    expect(screen.queryByText(/部分证据不足|证据仍缺失/)).not.toBeInTheDocument();
  });

  it("shows an honest unavailable state without demo findings", async () => {
    const loader = vi.fn(async () => {
      throw new Error("offline");
    });
    render(<AnalysisProgress analysisId="analysis-live-1" loader={loader} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取分析状态");
    expect(screen.queryByText("首页启动慢")).not.toBeInTheDocument();
  });

  it("does not show the previous analysis while a new route is loading", async () => {
    const loader = vi.fn(async (analysisId: string, _signal, onSnapshot) => {
      if (analysisId === "analysis-old") {
        onSnapshot({
          teamId: "team-1",
          analysis: analysis("completed", analysisId),
          report: null,
          reportLoadFailed: false,
        });
        return;
      }
      await new Promise<void>(() => undefined);
    });
    const { rerender } = render(
      <AnalysisProgress analysisId="analysis-old" loader={loader} />,
    );

    expect(await screen.findByText("analysis-old")).toBeInTheDocument();
    rerender(<AnalysisProgress analysisId="analysis-new" loader={loader} />);

    expect(screen.getByRole("heading", { name: "正在读取分析状态" })).toBeInTheDocument();
    expect(screen.queryByText("analysis-old")).not.toBeInTheDocument();
  });

  it("renders the exact four server stages without inferring from the parent state", () => {
    const stages: readonly AnalysisStage[] = [
      { stage: "input_validation", state: "completed", failure: null },
      {
        stage: "smartperfetto",
        state: "failed",
        failure: { code: "trace_invalid", message: "Trace 数据不完整", retryable: false },
      },
      { stage: "perfpilot_ai", state: "not_requested", failure: null },
      { stage: "report", state: "pending", failure: null },
    ];

    render(<AnalysisProgressView analysis={analysis("completed", "analysis-live-1", stages)} />);

    expect(screen.getByText("文件校验")).toBeInTheDocument();
    expect(screen.getByText("SmartPerfetto")).toBeInTheDocument();
    expect(screen.getByText("PerfPilot AI")).toBeInTheDocument();
    expect(screen.getByText("报告完成")).toBeInTheDocument();
    expect(screen.getByText("Trace 数据不完整")).toBeInTheDocument();
    expect(screen.getByText("SmartPerfetto").closest("li")).toHaveClass("is-failed");
    expect(screen.getByText("报告完成").closest("li")).not.toHaveClass("is-complete");
  });

  it("polls until all server stages settle and retains the prior report during rerun", async () => {
    const running = analysis("completed", "analysis-live-1", [
      { stage: "input_validation", state: "completed", failure: null },
      { stage: "smartperfetto", state: "completed", failure: null },
      { stage: "perfpilot_ai", state: "running", failure: null },
      { stage: "report", state: "pending", failure: null },
    ]);
    const complete = analysis("completed");
    const oldReport = failedReport();
    const newReport = { ...failedReport(), report_version: 2 };
    const client = {
      csrf: vi.fn(async () => "csrf"),
      me: vi.fn(async () => ({
        schema_version: "1.0" as const,
        memberships: [{ team: { id: "team-1", name: "Ray" }, role: "owner" }],
      })),
      analysis: vi.fn().mockResolvedValueOnce(running).mockResolvedValueOnce(complete),
      report: vi.fn().mockResolvedValueOnce(oldReport).mockResolvedValueOnce(newReport),
    } as unknown as PerfPilotClient;
    const updates: Array<{ report: AnalysisReport | null; analysis: AnalysisResponse }> = [];

    await createAnalysisLoader(client, async () => undefined)(
      "analysis-live-1",
      new AbortController().signal,
      (snapshot) => updates.push(snapshot),
    );

    expect(client.analysis).toHaveBeenCalledTimes(2);
    expect(client.report).toHaveBeenCalledTimes(2);
    expect(updates.map((item) => item.report?.report_version)).toEqual([1, 2]);
  });

  it("does not substitute fixture findings when the real report cannot load", async () => {
    const loader = vi.fn(async (_analysisId: string, _signal, onSnapshot) => {
      onSnapshot({
        teamId: "team-1",
        analysis: analysis("completed"),
        report: null,
        reportLoadFailed: true,
      });
    });

    render(<AnalysisProgress analysisId="analysis-live-1" loader={loader} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("报告暂时无法读取");
    expect(screen.queryByText("首页启动慢")).not.toBeInTheDocument();
  });

  it("offers a direct entry to the dedicated final report", () => {
    render(
      <AnalysisProgressView
        analysis={analysis("completed", "analysis-live-1")}
        report={failedReport()}
      />,
    );

    expect(screen.getByRole("link", { name: "打开完整报告" })).toHaveAttribute(
      "href",
      "/analyses/analysis-live-1/report",
    );
  });

  it("uses a fresh idempotency key and resumes loading after an AI-only rerun", async () => {
    const user = userEvent.setup();
    let loadCount = 0;
    const loader = vi.fn(async (_analysisId: string, _signal, onSnapshot) => {
      loadCount += 1;
      onSnapshot({
        teamId: "team-1",
        analysis: analysis("partially_completed"),
        report: failedReport(),
        reportLoadFailed: false,
      });
    });
    const rerunner = vi.fn(async () => undefined);

    render(
      <AnalysisProgress
        analysisId="analysis-live-1"
        loader={loader}
        rerunner={rerunner}
        randomUUID={() => "rerun-uuid-1"}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "重新生成 AI 报告" }));

    expect(rerunner).toHaveBeenCalledWith(
      "team-1",
      "analysis-live-1",
      "rerun-uuid-1",
      expect.any(AbortSignal),
    );
    expect(loadCount).toBe(2);
  });

  it("uses the LAN-compatible UUID factory by default for an AI-only rerun", async () => {
    const user = userEvent.setup();
    const nativeRandomUuid = vi
      .spyOn(globalThis.crypto, "randomUUID")
      .mockReturnValue("00000000-0000-4000-8000-000000000000");
    const loader = vi.fn(async (_analysisId: string, _signal, onSnapshot) => {
      onSnapshot({
        teamId: "team-1",
        analysis: analysis("partially_completed"),
        report: failedReport(),
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
      <AnalysisProgress
        analysisId="analysis-live-1"
        loader={loader}
        rerunner={rerunner}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "重新生成 AI 报告" }));

    expect(rerunner).toHaveBeenCalledOnce();
    expect(rerunner.mock.calls[0]?.[2]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    expect(nativeRandomUuid).not.toHaveBeenCalled();
  });
});
