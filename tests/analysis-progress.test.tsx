// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AnalysisProgress,
  AnalysisProgressView,
} from "../app/components/analysis-progress";
import type { AnalysisResponse, AnalysisState } from "../app/lib/perfpilot-api";

afterEach(cleanup);

function analysis(
  state: AnalysisState,
  analysisId = "analysis-live-1",
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
    failure:
      state === "failed"
        ? { code: "engine_failed", message: "任务未能完成", retryable: false }
        : null,
  };
}

describe("AnalysisProgress", () => {
  it.each([
    ["created", "等待上传 Trace"],
    ["uploading", "正在接收分析文件"],
    ["analyzing", "SmartPerfetto 正在分析"],
    ["completed", "分析已完成"],
    ["partially_completed", "分析完成，部分证据不足"],
    ["failed", "分析未能完成"],
    ["canceled", "分析已取消"],
  ] as const)("renders the real %s state", (state, label) => {
    render(<AnalysisProgressView analysis={analysis(state)} />);

    expect(screen.getByRole("heading", { name: label })).toBeInTheDocument();
    expect(screen.getByText("Trace")).toBeInTheDocument();
    expect(screen.getByText("analysis-live-1")).toBeInTheDocument();
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
    const loader = vi.fn(async (analysisId: string, _signal, onAnalysis) => {
      if (analysisId === "analysis-old") {
        onAnalysis(analysis("completed", analysisId));
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
});
