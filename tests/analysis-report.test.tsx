// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalysisReportView } from "../app/components/analysis-report";
import type { AnalysisReport, ReportFinding } from "../app/lib/perfpilot-api";

afterEach(cleanup);

const ANALYSIS_ID = "82000000-0000-4000-8000-000000000001";
const FINDING_IDS = Array.from(
  { length: 6 },
  (_, index) => `85000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
);
const EVIDENCE_IDS = Array.from(
  { length: 6 },
  (_, index) => `86000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
);
const METRIC_IDS = Array.from(
  { length: 10 },
  (_, index) => `84000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
);

function report(synthesisState: "completed" | "failed" = "completed"): AnalysisReport {
  const findings: ReportFinding[] = FINDING_IDS.map((findingId, index) => ({
    finding_id: findingId,
    title: `真实问题 ${index + 1}`,
    summary:
      index === 0
        ? "**描述**：主线程包含 `<em>同步等待</em>`。"
        : `来自内核的问题 ${index + 1}。`,
    severity: index === 0 ? "critical" : "warning",
    confidence: "high",
    evidence_ids: [EVIDENCE_IDS[index]],
  }));
  const evidence = EVIDENCE_IDS.map((evidenceId, index) => ({
    evidence_id: evidenceId,
    source: "perfetto.startup",
    query_id: `startup.query.${index + 1}`,
    interval_start_ns: index * 100,
    interval_end_ns: index * 100 + 50,
    fields: { blocked_duration_ms: 190 + index },
  }));
  return {
    schema_version: "1.1",
    analysis_id: ANALYSIS_ID,
    analysis_mode: "trace_upload",
    state: synthesisState === "completed" ? "completed" : "partially_completed",
    report_version: 3,
    generated_at: "2026-08-04T08:00:00Z",
    scenario_reports: [
      {
        scenario_job_id: "83000000-0000-4000-8000-000000000001",
        scenario_type: "startup",
        result_state: "completed",
        device_group_id: null,
        device_group_reason: "not_applicable",
        bundle: {
          metrics: METRIC_IDS.map((metricId, index) =>
            ({
              metric_id: metricId,
              name: "startup.time_to_initial_display_ms",
              status: "available",
              numeric_value: index === 0 ? 812.4 : 100 + index,
              unit: "ms",
              definition: index === 0 ? "首帧显示耗时" : `辅助指标 ${index + 1}`,
              threshold: { operator: "lte", value: 700, unit: "ms" },
            }),
          ),
          findings,
          evidence,
        },
        failure: null,
      },
    ],
    synthesis:
      synthesisState === "completed"
        ? {
            state: "completed",
            output: {
              schema_version: "1.0",
              executive_summary: "**启动耗时**超过现有阈值，主要证据指向主线程同步等待。",
              top_findings: FINDING_IDS.map((findingId, index) => ({
                finding_id: findingId,
                evidence_ids: [EVIDENCE_IDS[index]],
                user_impact: `用户影响 ${index + 1}`,
              })),
              recommendations: [
                {
                  priority: "p2",
                  title: "后续清理",
                  action: "清理非关键初始化。",
                  expected_effect: "减少后续占用。",
                  finding_ids: [FINDING_IDS[0]],
                  evidence_ids: [EVIDENCE_IDS[0]],
                },
                {
                  priority: "p0",
                  title: "立即移出主线程",
                  action: "把同步等待移出启动关键路径。",
                  expected_effect: "降低首帧显示耗时。",
                  finding_ids: [FINDING_IDS[0]],
                  evidence_ids: [EVIDENCE_IDS[0]],
                },
                {
                  priority: "p1",
                  title: "延迟初始化",
                  action: "首帧后再初始化非关键模块。",
                  expected_effect: "缩短启动关键路径。",
                  finding_ids: [FINDING_IDS[0]],
                  evidence_ids: [EVIDENCE_IDS[0]],
                },
              ],
              retest_plan: [
                {
                  mode: "verify_metric",
                  scenario_type: "startup",
                  metric_ids: ["84000000-0000-4000-8000-000000000001"],
                  limitation_ids: [],
                  steps: "使用相同设备重复五次冷启动。",
                  success_condition: "meet_existing_threshold",
                  failure_condition: "threshold_missed",
                },
              ],
              limitations: [
                {
                  limitation_id: "87000000-0000-4000-8000-000000000001",
                  summary: "当前 Trace 未包含网络端耗时。",
                },
              ],
            },
            synthesis_artifact_id: "88000000-0000-4000-8000-000000000001",
            failure_code: null,
            provenance: {
              provider_protocol: "chat-completions-json-schema-v1",
              provider_name: "approved-provider",
              model: "approved-model",
              prompt_template_version: "1.0.0",
              normalizer_version: "smartperfetto-normalizer-1",
              generated_at: "2026-08-04T08:00:00Z",
              prompt_tokens: 100,
              completion_tokens: 200,
              total_tokens: 300,
              generation: 2,
            },
          }
        : {
            state: "failed",
            output: null,
            synthesis_artifact_id: null,
            failure_code: "synthesis_unavailable",
            provenance: null,
          },
  };
}

function deviceMemoryReport(): AnalysisReport {
  const base = report();
  return {
    ...base,
    analysis_mode: "device",
    scenario_reports: [
      ...base.scenario_reports,
      {
        scenario_job_id: "83000000-0000-4000-8000-000000000002",
        scenario_type: "memory_cycle",
        result_state: "completed",
        device_group_id: null,
        device_group_reason: "not_applicable",
        bundle: {
          metrics: [
            ...[
              "stack.rss_kb",
              "stack.swap_pss_kb",
              "code.rss_kb",
              "code.pss_kb",
              "code.private_dirty_kb",
              "graphics.rss_kb",
              "graphics.pss_kb",
              "graphics.private_dirty_kb",
              "dalvik_heap.rss_kb",
            ].map((name, index) => ({
              metric_id: `84000000-0000-4000-8000-${String(index + 20).padStart(12, "0")}`,
              name: `memory.meminfo.${name}`,
              status: "available" as const,
              numeric_value: 1000 + index,
              unit: "kB",
              definition: `${name} reported by dumpsys meminfo.`,
              threshold: null,
            })),
            {
              metric_id: "84000000-0000-4000-8000-000000000011",
              name: "memory.meminfo.total.pss_kb",
              status: "available",
              numeric_value: 123456,
              unit: "kB",
              definition: "PSS reported by dumpsys meminfo for the TOTAL row.",
              threshold: null,
            },
            {
              metric_id: "84000000-0000-4000-8000-000000000012",
              name: "memory.meminfo.native_heap.private_dirty_kb",
              status: "available",
              numeric_value: 30000,
              unit: "kB",
              definition: "Private Dirty reported by dumpsys meminfo for the Native Heap row.",
              threshold: null,
            },
          ],
          findings: [],
          evidence: [
            {
              evidence_id: "86000000-0000-4000-8000-000000000007",
              source: "android_memory.context",
              query_id: "android_memory.context.v1_2",
              interval_start_ns: null,
              interval_end_ns: null,
              fields: {
                support_level: "strong",
                primary_intent_support_level: "supported",
                accounting_ledger_status: "available",
                coverage_available_count: 2,
              },
            },
          ],
        },
        failure: null,
      },
    ],
  };
}

describe("AnalysisReportView", () => {
  it("renders the concise completed report in evidence-first order", () => {
    const { container } = render(
      <AnalysisReportView report={report()} onRetrySynthesis={vi.fn()} retrying={false} />,
    );

    const headings = screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
    expect(headings).toEqual([
      "执行摘要",
      "重点问题",
      "优化建议",
      "复测计划",
      "限制与缺失证据",
    ]);
    expect(screen.getByText("812.4 ms")).toBeInTheDocument();
    expect(screen.getByText("另有 2 项原始指标").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("真实问题 1")).toBeInTheDocument();
    expect(screen.getByText("真实问题 5")).toBeInTheDocument();
    expect(screen.queryByText("真实问题 6")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("recommendation-priority").map((node) => node.textContent)).toEqual([
      "P0",
      "P1",
      "P2",
    ]);
    expect(screen.getByText("使用相同设备重复五次冷启动。")).toBeInTheDocument();
    expect(screen.getByText("当前 Trace 未包含网络端耗时。")).toBeInTheDocument();
    expect(screen.getByText("生成信息").closest("details")).not.toHaveAttribute("open");
    expect(document.getElementById(`finding-${FINDING_IDS[0]}`)).toBeInTheDocument();
    expect(document.getElementById(`evidence-${EVIDENCE_IDS[0]}`)).toBeInTheDocument();
    expect(container.querySelector(`a[href="#evidence-${EVIDENCE_IDS[0]}"]`)).toBeInTheDocument();
    expect(container.querySelector("em")).not.toBeInTheDocument();
    expect(screen.getByText("启动耗时超过现有阈值，主要证据指向主线程同步等待。")).toBeInTheDocument();
    expect(screen.getByText("描述：主线程包含 <em>同步等待</em>。")).toBeInTheDocument();
    expect(screen.getAllByText("查看原始字段")[0].closest("details")).not.toHaveAttribute("open");
  });

  it("keeps core evidence visible and offers one retry when synthesis failed", async () => {
    const user = userEvent.setup();
    const retry = vi.fn(async () => undefined);
    render(
      <AnalysisReportView report={report("failed")} onRetrySynthesis={retry} retrying={false} />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("内核分析已完成，AI 建议暂未生成");
    expect(screen.getByText("真实问题 1")).toBeInTheDocument();
    expect(document.getElementById(`evidence-${EVIDENCE_IDS[0]}`)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "优化建议" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新生成 AI 建议" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("disables retry while a synthesis request is being submitted", () => {
    render(
      <AnalysisReportView report={report("failed")} onRetrySynthesis={vi.fn()} retrying />,
    );

    expect(screen.getByRole("button", { name: "正在重新生成" })).toBeDisabled();
  });

  it("shows Android Memory facts in a dedicated evidence section", () => {
    render(
      <AnalysisReportView
        report={deviceMemoryReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    const memorySection = screen.getByRole("region", { name: "Android 内存分析" });
    expect(within(memorySection).getByText("123456 kB")).toBeVisible();
    expect(within(memorySection).getByText("总 PSS")).toBeVisible();
    expect(within(memorySection).getByText("Native Heap Private Dirty")).toBeVisible();
    expect(within(memorySection).getByText("证据充分")).toBeVisible();
    expect(within(memorySection).getByText("账本可用")).toBeVisible();
    expect(within(memorySection).getByText("2 类可用")).toBeVisible();
    expect(
      within(memorySection).getByText("这里只展示采集事实；单次内存值不会自动判定为泄漏。"),
    ).toBeVisible();
    expect(within(screen.getByLabelText("关键场景指标")).queryByText("123456 kB")).not.toBeInTheDocument();
    expect(screen.getByText("DUAL-KERNEL EVIDENCE")).toBeVisible();
  });

  it("keeps an explicit memory state when the kernel result is unavailable", () => {
    const complete = deviceMemoryReport();
    const unavailable: AnalysisReport = {
      ...complete,
      state: "partially_completed",
      scenario_reports: complete.scenario_reports.map((scenario) =>
        scenario.scenario_type === "memory_cycle"
          ? {
              ...scenario,
              result_state: "failed",
              bundle: null,
              failure: {
                code: "android_memory_execution_failed",
                message: "Android memory analysis did not produce a usable result.",
                retryable: false,
              },
            }
          : scenario,
      ),
    };

    render(
      <AnalysisReportView report={unavailable} onRetrySynthesis={vi.fn()} retrying={false} />,
    );

    const memorySection = screen.getByRole("region", { name: "Android 内存分析" });
    expect(within(memorySection).getByText("分析未完成")).toBeVisible();
    expect(
      within(memorySection).getByText(
        "Android Memory 未获得完整采集结果，已保留可验证的证据状态。",
      ),
    ).toBeVisible();
    expect(within(memorySection).getByText("未提供账本")).toBeVisible();
    expect(within(memorySection).queryByLabelText("Android 内存指标")).not.toBeInTheDocument();
  });
});
