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
      screen.getByText("通常需要 3–8 分钟，复杂 Trace 可能更久"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看详情" })).toHaveAttribute(
      "href",
      "/analyses/analysis-active-1",
    );
    await user.click(screen.getByRole("button", { name: "取消分析" }));
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("derives AI and cancel stages only from authoritative analysis fields", () => {
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
