// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AnalysisHistory } from "../app/components/analysis-history";
import type {
  AnalysisListItem,
  PerfPilotClient,
} from "../app/lib/perfpilot-api";

const TEAM_ID = "fe56f98a-84ef-4a7e-b6e7-83082505d5df";
const BASE_ITEM: AnalysisListItem = {
  schema_version: "1.3",
  analysis_id: "8e759ddc-4ca9-4677-831f-f8e3d8f7808a",
  team_id: TEAM_ID,
  analysis_mode: "trace_upload",
  analysis_profile: "startup",
  test_type: "cold_start",
  package_name: "com.rivotek.mediacenter",
  custom_test_name: null,
  custom_test_description: null,
  question: null,
  state: "completed",
  version: 4,
  created_at: "2026-08-21T08:00:00+00:00",
  completed_at: "2026-08-21T08:01:00+00:00",
  report_available: true,
  failure: null,
  stages: [],
  input_uploads: [],
};

function historyItem(
  overrides: Partial<AnalysisListItem>,
): AnalysisListItem {
  return { ...BASE_ITEM, ...overrides };
}

afterEach(() => cleanup());

it("shows the latest ten report-bearing analyses", async () => {
  const client = {
    analyses: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      analyses: [
        historyItem({
          state: "partially_completed",
          test_type: "cold_start",
          package_name: "com.rivotek.mediacenter",
        }),
        historyItem({
          analysis_id: "8e759ddc-4ca9-4677-831f-f8e3d8f7808b",
          state: "completed",
          test_type: "other",
          custom_test_name: "首页连续切换",
          created_at: "2026-08-20T08:00:00+00:00",
          completed_at: "2026-08-20T08:01:00+00:00",
        }),
      ],
    }),
  } as unknown as PerfPilotClient;

  render(<AnalysisHistory client={client} teamId={TEAM_ID} />);

  expect(await screen.findAllByText("分析完成")).toHaveLength(2);
  expect(screen.getByText("冷启动")).toBeVisible();
  expect(screen.getAllByText("com.rivotek.mediacenter")).toHaveLength(2);
  expect(screen.getByText("首页连续切换")).toBeVisible();
  expect(
    screen.getByText(
      "历史数据仅保留最近 10 份，超过后最旧数据将自动丢弃。",
    ),
  ).toBeVisible();
  expect(client.analyses).toHaveBeenCalledWith(
    TEAM_ID,
    10,
    expect.any(AbortSignal),
  );
  const links = screen.getAllByRole("link", { name: "查看报告" });
  expect(links[0]).toHaveAttribute(
    "href",
    "/analyses/8e759ddc-4ca9-4677-831f-f8e3d8f7808a/report",
  );
  expect(links[1]).toHaveAttribute(
    "href",
    "/analyses/8e759ddc-4ca9-4677-831f-f8e3d8f7808b/report",
  );
});

it("shows an honest empty state when no successful reports exist", async () => {
  const client = {
    analyses: vi.fn().mockResolvedValue({ schema_version: "1.0", analyses: [] }),
  } as unknown as PerfPilotClient;

  render(<AnalysisHistory client={client} teamId={TEAM_ID} />);

  expect(await screen.findByText("还没有成功的测试记录")).toBeVisible();
});

it("keeps stored reports honest when optional history fields are absent", async () => {
  const client = {
    analyses: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      analyses: [
        historyItem({
          test_type: undefined,
          package_name: undefined,
          application_metadata: null,
          completed_at: null,
        }),
      ],
    }),
  } as unknown as PerfPilotClient;

  render(<AnalysisHistory client={client} teamId={TEAM_ID} />);

  expect(await screen.findByText("未记录测试类型")).toBeVisible();
  expect(screen.getByText("未记录包名")).toBeVisible();
  expect(screen.getByText(/完成时间：未记录完成时间/)).toBeVisible();
});

it("shows a read error without demo history", async () => {
  const client = {
    analyses: vi.fn().mockRejectedValue(new Error("offline")),
  } as unknown as PerfPilotClient;

  render(<AnalysisHistory client={client} teamId={TEAM_ID} />);

  expect(await screen.findByText("暂时无法读取测试历史")).toBeVisible();
  expect(screen.queryByText("com.example")).not.toBeInTheDocument();
});
