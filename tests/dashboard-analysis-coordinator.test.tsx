// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentType } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Dashboard } from "../app/components/dashboard";
import type { LatestReportLoader } from "../app/components/latest-analysis-report-entry";
import type {
  AnalysisListItem,
  AnalysisResponse,
  PerfPilotClient,
} from "../app/lib/perfpilot-api";

afterEach(cleanup);

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

    expect(await screen.findByText("分析已完成")).toBeInTheDocument();
    await waitFor(() => expect(latestReportLoader).toHaveBeenCalledTimes(2));
  });

  it("confirms and displays cancellation only after the backend returns canceled", async () => {
    const user = userEvent.setup();
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

    await user.click(await screen.findByRole("button", { name: "取消分析" }));

    expect(confirmCancel).toHaveBeenCalledOnce();
    expect(client.cancelAnalysis).toHaveBeenCalledWith(
      "team-1",
      "analysis-active-1",
      expect.any(AbortSignal),
    );
    expect(await screen.findByText("分析已取消")).toBeInTheDocument();
  });
});
