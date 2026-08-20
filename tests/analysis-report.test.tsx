// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import analysisReportV13Example from "../contracts/v1/examples/analysis-report-v1.3.valid.json";
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

function sourceAwareReport(): AnalysisReport {
  const legacy = report();
  const output = legacy.synthesis.state === "completed" ? legacy.synthesis.output : null;
  if (output === null) throw new Error("fixture requires synthesis");
  return {
    ...legacy,
    schema_version: "1.2",
    synthesis: {
      ...legacy.synthesis,
      output: {
        ...output,
        schema_version: "2.0",
        verdict: "启动关键路径被主线程同步等待阻塞。",
        key_metric_ids: METRIC_IDS.slice(0, 3),
        conclusions: FINDING_IDS.map((findingId, index) => ({
          finding_id: findingId,
          evidence_ids: [EVIDENCE_IDS[index]],
          source_ref_ids: [],
          problem: `问题点 ${index + 1}`,
          cause: `SmartPerfetto 证据确认原因 ${index + 1}`,
          source_root_cause: `源码根因判断 ${index + 1}`,
          recommendation: `修改建议 ${index + 1}`,
        })),
        top_findings: output.top_findings.slice(0, 3),
        recommendations: output.recommendations.slice(0, 3),
        source_fixes: [],
      },
    },
    source_code: {
      requested: false,
      provider_kind: null,
      agent_id: null,
      workspace_id: null,
      snapshot_policy: null,
      validation_profile_id: null,
      snapshot: null,
      context_state: "not_requested",
      match_summary: "none",
      source_refs: [],
      exclusions: [],
      fixes: [],
      limitations: [],
    },
  } as unknown as AnalysisReport;
}

function sourceActionReport(): AnalysisReport {
  const base = sourceAwareReport();
  if (base.schema_version !== "1.2" || base.synthesis.state !== "completed") {
    throw new Error("fixture requires source-aware synthesis");
  }
  const makeFix = (index: number, findingIndex: number, path: string) => ({
    fix_id: `96000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    finding_id: FINDING_IDS[findingIndex],
    evidence_ids: [EVIDENCE_IDS[findingIndex]],
    recommendation_priority: "p0" as const,
    source_ref_ids: [
      `97000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    ],
    rule_id: "startup.main_thread_work",
    match_grade: "strong" as const,
    relative_path: path,
    symbol: `demo.Startup.step${index}`,
    diagnosis: `源码诊断 ${index}`,
    diff: `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1,1 +1,1 @@\n-old\n+new${index}\n`,
    validation_profile_id: null,
    retest_target: "重复相同的冷启动场景。",
    verification: {
      state: "not_configured" as const,
      exit_code: null,
      duration_ms: null,
      profile_id: null,
      patch_sha256: String(index).repeat(64).slice(0, 64),
      log_summary: null,
      patch_artifact: null,
    },
  });
  const fixes = [
    makeFix(1, 0, "app/src/main/java/demo/StartupOne.kt"),
    makeFix(2, 0, "app/src/main/java/demo/StartupTwo.kt"),
    makeFix(3, 1, "app/src/main/java/demo/StartupThree.kt"),
  ];
  return {
    ...base,
    synthesis: {
      ...base.synthesis,
      output: {
        ...base.synthesis.output,
        source_fixes: fixes.map((sourceFix) => {
          const { verification, ...fix } = sourceFix;
          void verification;
          return fix;
        }),
      },
    },
    source_code: {
      ...base.source_code,
      requested: true,
      provider_kind: "agent_workspace",
      agent_id: "71000000-0000-4000-8000-000000000001",
      workspace_id: "92000000-0000-4000-8000-000000000001",
      snapshot_policy: "tracked_worktree",
      context_state: "available",
      match_summary: "strong",
      snapshot: {
        snapshot_id: "93000000-0000-4000-8000-000000000001",
        snapshot_hash: "b".repeat(64),
        git_head: "c".repeat(40),
      },
      fixes,
    },
  };
}

function findingWorkbenchReport(): Extract<AnalysisReport, { readonly schema_version: "1.3" }> {
  return structuredClone(analysisReportV13Example) as unknown as Extract<
    AnalysisReport,
    { readonly schema_version: "1.3" }
  >;
}

function findingWorkbenchReportWithSixFindings(): Extract<AnalysisReport, { readonly schema_version: "1.3" }> {
  const base = structuredClone(analysisReportV13Example);
  const template = base.workbench.findings[0];
  const conclusion = base.synthesis.output.conclusions[0];
  const retest = base.workbench.retest_plans[0];
  base.workbench.findings = Array.from({ length: 6 }, (_, index) => ({
    ...structuredClone(template),
    finding_id: FINDING_IDS[index],
    title: `问题 ${index + 1}`,
    priority: index < 2 ? "p0" : index === 2 ? "p1" : "p2",
    priority_score: 96 - index,
    retest_plan_id: `89000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
  }));
  base.workbench.primary_finding_ids = FINDING_IDS.slice(0, 3);
  base.workbench.retest_plans = base.workbench.findings.map((finding: Record<string, unknown>) => ({
    ...structuredClone(retest),
    retest_plan_id: finding.retest_plan_id,
    finding_id: finding.finding_id,
  }));
  base.synthesis.output.conclusions = base.workbench.findings.map((finding: Record<string, unknown>, index: number) => ({
    ...structuredClone(conclusion),
    finding_id: finding.finding_id,
    problem: `问题点 ${index + 1}`,
    cause: `问题原因 ${index + 1}`,
    source_root_cause: `源码判断 ${index + 1}`,
    recommendation: `修改建议 ${index + 1}`,
  }));
  base.synthesis.output.top_findings = base.synthesis.output.top_findings.concat(
    FINDING_IDS.slice(1, 3).map((findingId, index) => ({
      ...base.synthesis.output.top_findings[0],
      finding_id: findingId,
      user_impact: `主要影响 ${index + 2}`,
    })),
  );
  return base as unknown as Extract<AnalysisReport, { readonly schema_version: "1.3" }>;
}

describe("AnalysisReportView", () => {
  it("dispatches AnalysisReport 1.3 to the six-region Finding workbench", () => {
    render(
      <AnalysisReportView
        report={findingWorkbenchReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    expect(screen.getByRole("tablist", { name: "Finding 工作台" })).toBeVisible();
    for (const label of [
      "概览",
      "问题清单",
      "证据与指标",
      "源码与优化",
      "SmartPerfetto 原始报告",
      "复测计划",
    ]) {
      expect(screen.getByRole("tab", { name: label })).toBeVisible();
    }
    expect(screen.getByText("分析完成")).toHaveClass("is-completed");
    expect(screen.queryByRole("tab", { name: "技术附录" })).not.toBeInTheDocument();
  });

  it("renders the server-owned critical path and four-part Finding diagnosis", () => {
    render(
      <AnalysisReportView
        report={findingWorkbenchReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    expect(screen.getByText("Application 初始化")).toBeVisible();
    expect(screen.getByText("190 ms")).toBeVisible();
    const overview = screen.getByRole("tabpanel");
    for (const label of [
      "1. 问题点",
      "2. 为什么会有这个问题",
      "3. 结合源码判断的根因是什么",
      "4. 修改建议",
    ]) {
      expect(within(overview).getByText(label)).toBeVisible();
    }
  });

  it("opens only a validated Trace evidence identifier", async () => {
    const user = userEvent.setup();
    const openEvidence = vi.fn();
    render(
      <AnalysisReportView
        report={findingWorkbenchReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
        openEvidence={openEvidence}
      />,
    );

    await user.click(screen.getByRole("tab", { name: "证据与指标" }));
    await user.click(screen.getByRole("button", { name: "在 Trace 中打开证据" }));
    expect(openEvidence).toHaveBeenCalledWith({
      analysisId: ANALYSIS_ID,
      evidenceId: "86000000-0000-4000-8000-000000000001",
    });
  });

  it("keeps AnalysisReport 1.2 on the existing report tabs", () => {
    render(
      <AnalysisReportView
        report={sourceAwareReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    expect(screen.getByRole("tab", { name: "结论" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "问题清单" })).not.toBeInTheDocument();
  });

  it("shows three primary findings and folds every additional finding", async () => {
    const user = userEvent.setup();
    render(
      <AnalysisReportView
        report={findingWorkbenchReportWithSixFindings()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    expect(screen.getAllByTestId("primary-finding")).toHaveLength(3);
    const details = screen.getByText("展开其余 3 项").closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getAllByTestId("additional-finding")).toHaveLength(3);
    await user.click(screen.getByText("展开其余 3 项"));
    expect(details).toHaveAttribute("open");
  });

  it("filters the stable server Finding order", async () => {
    const user = userEvent.setup();
    render(
      <AnalysisReportView
        report={findingWorkbenchReportWithSixFindings()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    await user.click(screen.getByRole("tab", { name: "问题清单" }));
    await user.selectOptions(screen.getByLabelText("优先级"), "p0");
    expect(screen.getAllByTestId("finding-list-item").map((node) => node.dataset.priority)).toEqual(["p0", "p0"]);
  });

  it("never renders source locations or Diff without a strong match", async () => {
    const user = userEvent.setup();
    const example = structuredClone(analysisReportV13Example);
    const report = {
      ...example,
      capabilities: { ...example.capabilities, source: "mismatch" },
      quality: { ...example.quality, source_correlation_state: "available_weak" },
      source_code: {
        requested: true,
        match_summary: "weak",
        source_refs: [{ relative_path: "private/Startup.kt", symbol: "demo.Startup.run" }],
        fixes: [],
      },
    };
    render(
      <AnalysisReportView
        report={report as AnalysisReport}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    await user.click(screen.getByRole("tab", { name: "源码与优化" }));
    const sourcePanel = screen.getByRole("tabpanel");
    expect(within(sourcePanel).getByRole("heading", { name: "源码不匹配" })).toBeVisible();
    expect(within(sourcePanel).getByText("修改仅供参考")).toBeVisible();
    expect(within(sourcePanel).queryByText(/private\/Startup\.kt/)).not.toBeInTheDocument();
    expect(within(sourcePanel).queryByText(/demo\.Startup\.run/)).not.toBeInTheDocument();
    expect(within(sourcePanel).queryByLabelText("建议代码 Diff")).not.toBeInTheDocument();
  });

  it("renders an explicit package, duration, metric and environment fingerprint for retest", async () => {
    const user = userEvent.setup();
    render(
      <AnalysisReportView
        report={findingWorkbenchReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    await user.click(screen.getByRole("tab", { name: "复测计划" }));
    const retestPanel = screen.getByRole("tabpanel");
    expect(within(retestPanel).getByText("com.rivotek.mediacenter")).toBeVisible();
    expect(within(retestPanel).getByText("15 秒")).toBeVisible();
    expect(within(retestPanel).getByText("startup.time_to_initial_display_ms")).toBeVisible();
    expect(within(retestPanel).getByText(/^sha256:/)).toBeVisible();
  });

  it("shows three primary four-part conclusions and keeps every remaining conclusion collapsible", async () => {
    const user = userEvent.setup();
    render(
      <AnalysisReportView
        report={sourceAwareReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    expect(screen.getByRole("tab", { name: "结论" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "源码修复" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "技术附录" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "SmartPerfetto 原始报告" })).toBeInTheDocument();
    expect(screen.getAllByTestId("key-metric")).toHaveLength(3);
    const primary = screen.getAllByTestId("primary-conclusion");
    expect(primary).toHaveLength(3);
    for (const conclusion of primary) {
      expect(within(conclusion).getByText("1. 问题点")).toBeVisible();
      expect(within(conclusion).getByText("2. 为什么会有这个问题")).toBeVisible();
      expect(within(conclusion).getByText("3. 结合源码判断的根因是什么")).toBeVisible();
      expect(within(conclusion).getByText("4. 修改建议")).toBeVisible();
    }
    const remainder = screen.getByText("展开其余 3 条问题与优化方案").closest("details");
    expect(remainder).not.toHaveAttribute("open");
    expect(screen.getAllByTestId("additional-conclusion")).toHaveLength(3);
    for (const recommendation of screen.getAllByText("修改建议 4")) {
      expect(recommendation).not.toBeVisible();
    }

    await user.click(screen.getByText("展开其余 3 条问题与优化方案"));
    const fourth = screen.getAllByTestId("additional-conclusion")[0];
    await user.click(within(fourth).getByText("问题点 4", { selector: "summary" }));
    expect(within(fourth).getByText("修改建议 4")).toBeVisible();
  });

  it("shows every source action, groups multiple diffs, and folds only actions after three", async () => {
    const user = userEvent.setup();
    render(
      <AnalysisReportView
        report={sourceActionReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
      />,
    );

    await user.click(screen.getByRole("tab", { name: "源码修复" }));

    expect(
      screen.getByText("修改建议仅供参考，请结合实际业务逻辑审查后再应用。"),
    ).toBeVisible();
    const primary = screen.getAllByTestId("source-action-group");
    expect(primary).toHaveLength(6);
    expect(within(primary[0]).getAllByLabelText("建议代码 Diff")).toHaveLength(2);
    expect(within(primary[1]).getAllByLabelText("建议代码 Diff")).toHaveLength(1);
    expect(within(primary[2]).getByText("修改方案")).toBeVisible();
    expect(within(primary[2]).getByText("修改建议 3")).toBeVisible();
    expect(within(primary[2]).queryByLabelText("建议代码 Diff")).not.toBeInTheDocument();
    const remainder = screen.getByText("展开其余 3 项源码修改").closest("details");
    expect(remainder).not.toHaveAttribute("open");
    expect(within(primary[5]).getByText("修改建议 6")).not.toBeVisible();
    expect(screen.queryByText("没有可安全生成的源码修复")).not.toBeInTheDocument();

    await user.click(screen.getByText("展开其余 3 项源码修改"));
    expect(within(primary[5]).getByText("修改建议 6")).toBeVisible();
  });

  it("embeds the byte-faithful SmartPerfetto HTML without fetching or rendering JSON", async () => {
    const user = userEvent.setup();
    const originalUrl = vi.fn(() => "/original.html");
    const downloadUrl = vi.fn(() => "/original.html?download=true");
    const client = {
      smartPerfettoOriginalUrl: originalUrl,
      smartPerfettoOriginalDownloadUrl: downloadUrl,
    } as unknown as import("../app/lib/perfpilot-api").PerfPilotClient;
    render(
      <AnalysisReportView
        report={sourceAwareReport()}
        onRetrySynthesis={vi.fn()}
        retrying={false}
        teamId="team-1"
        client={client}
      />,
    );

    expect(originalUrl).not.toHaveBeenCalled();
    await user.click(screen.getByRole("tab", { name: "SmartPerfetto 原始报告" }));
    expect(originalUrl).toHaveBeenCalledWith("team-1", ANALYSIS_ID);
    expect(screen.getByTitle("SmartPerfetto 原始 HTML 报告")).toHaveAttribute(
      "src",
      "/original.html",
    );
    expect(screen.getByRole("link", { name: "下载原始 HTML" })).toHaveAttribute(
      "href",
      "/original.html?download=true",
    );
    expect(screen.queryByText("查看完整 JSON")).not.toBeInTheDocument();
  });

  it("does not render source paths for weak source matches", async () => {
    const user = userEvent.setup();
    const base = sourceAwareReport();
    if (base.schema_version !== "1.2") throw new Error("expected report 1.2");
    const weak = {
      ...base,
      synthesis: {
        ...base.synthesis,
        output: base.synthesis.state === "completed" ? {
          ...base.synthesis.output,
          verdict: "private/Startup.kt 存在启动等待。",
          executive_summary: "需要调整 DEMO.STARTUP.RUN 的执行时机。",
          conclusions: base.synthesis.output.conclusions.map((conclusion, index) =>
            index === 0 ? {
              ...conclusion,
              problem: "private/Startup.kt 的启动实现存在等待。",
              recommendation: "调整 demo.Startup.run 后重新采集启动场景。",
            } : conclusion,
          ),
          limitations: base.synthesis.output.limitations.map((limitation, index) =>
            index === 0 ? {
              ...limitation,
              summary: "private/Startup.kt 的证据仍不完整。",
            } : limitation,
          ),
        } : null,
      },
      source_code: {
        ...base.source_code,
        requested: true,
        provider_kind: "agent_workspace" as const,
        agent_id: "71000000-0000-4000-8000-000000000001",
        workspace_id: "92000000-0000-4000-8000-000000000001",
        snapshot_policy: "tracked_worktree" as const,
        context_state: "available" as const,
        match_summary: "weak" as const,
        source_refs: [
          {
            source_ref_id: "95000000-0000-4000-8000-000000000001",
            relative_path: "private/Startup.kt",
            language: "kotlin" as const,
            symbol: "demo.Startup.run",
            start_line: 1,
            end_line: 2,
            content_sha256: "a".repeat(64),
            snapshot_hash: "b".repeat(64),
            match_grade: "weak" as const,
            finding_ids: [],
            evidence_ids: [],
            rule_ids: [],
          },
        ],
      },
    };
    render(<AnalysisReportView report={weak} onRetrySynthesis={vi.fn()} retrying={false} />);

    await user.click(screen.getByRole("tab", { name: "源码修复" }));
    expect(screen.queryByText(/private\/Startup\.kt/)).not.toBeInTheDocument();
    expect(screen.queryByText(/demo\.Startup\.run/i)).not.toBeInTheDocument();
    expect(screen.getAllByText(/对应源码位置/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("源码匹配证据不足")).toBeVisible();
    expect(
      within(screen.getByLabelText("源码修改动作")).getByText(
        "调整 对应源码位置 后重新采集启动场景。",
      ),
    ).toBeVisible();
  });

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

    const status = screen.getByRole("status");
    expect(status.querySelector("strong")).toHaveTextContent(
      /^内核分析已完成，AI 最终报告暂未生成$/,
    );
    expect(status.querySelector("p")).toHaveTextContent(
      /^SmartPerfetto 的指标、问题和证据仍可查看。你可以只重新生成 AI 报告。$/,
    );
    expect(status).not.toHaveTextContent(["AI", "建议暂未生成"].join(" "));
    expect(status.querySelector("p")).not.toHaveTextContent(
      ["SmartPerfetto 的指标、问题和证据仍可查看。你可以只重新生成 AI", "建议。"].join(
        " ",
      ),
    );
    expect(screen.getByText("真实问题 1")).toBeInTheDocument();
    expect(document.getElementById(`evidence-${EVIDENCE_IDS[0]}`)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "优化建议" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: ["重新生成 AI", "建议"].join(" ") }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新生成 AI 报告" }));
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
