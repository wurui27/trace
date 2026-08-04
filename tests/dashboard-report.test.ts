import { describe, expect, it } from "vitest";

import { projectDashboardReport } from "../app/lib/dashboard-report";
import type { LatestReportSnapshot } from "../app/components/latest-analysis-report-entry";
import type {
  AnalysisListItem,
  AnalysisReport,
  ReportMetric,
} from "../app/lib/perfpilot-api";

const ANALYSIS_ID = "analysis-report-1";

function metric(
  name: string,
  numericValue: number,
  unit: string,
  definition: string,
  threshold: ReportMetric["threshold"] = null,
): ReportMetric {
  return {
    metric_id: `metric-${name}`,
    name,
    status: "available",
    numeric_value: numericValue,
    unit,
    definition,
    threshold,
  };
}

const analysis: AnalysisListItem = {
  schema_version: "1.0",
  analysis_id: ANALYSIS_ID,
  team_id: "team-1",
  analysis_mode: "trace_upload",
  analysis_profile: "startup",
  question: "首帧为什么慢？",
  state: "partially_completed",
  version: 9,
  created_at: "2026-08-04T08:00:00Z",
  cancel_requested_at: null,
  report_available: true,
  input_uploads: [],
  stages: [
    { stage: "input_validation", state: "completed", failure: null },
    { stage: "smartperfetto", state: "completed", failure: null },
    {
      stage: "perfpilot_ai",
      state: "failed",
      failure: { code: "ai_not_configured", message: "AI 未配置", retryable: false },
    },
    { stage: "report", state: "completed", failure: null },
  ],
  failure: { code: "ai_not_configured", message: "AI 未配置", retryable: false },
  source_analysis: {
    engine: "smartperfetto",
    rounds: 53,
    verification: "passed",
    session_id: "session-1",
    run_id: "run-1",
  },
  ai_rounds: [
    { round: 1, role: "extract", state: "failed", attempts: 0 },
    { round: 2, role: "review", state: "pending", attempts: 0 },
    { round: 3, role: "finalize", state: "pending", attempts: 0 },
  ],
};

function report(synthesis: "failed" | "completed" = "failed"): AnalysisReport {
  const base = {
    schema_version: "1.1" as const,
    analysis_id: ANALYSIS_ID,
    analysis_mode: "trace_upload" as const,
    state: synthesis === "completed" ? ("completed" as const) : ("partially_completed" as const),
    report_version: 4,
    generated_at: "2026-08-04T08:08:00Z",
    scenario_reports: [
      {
        scenario_job_id: "scenario-1",
        scenario_type: "startup" as const,
        result_state: "completed" as const,
        device_group_id: null,
        device_group_reason: "not_applicable",
        bundle: {
          metrics: [
            metric(
              "startup.startup_analysis_get_startups.dur_ms",
              481.772461,
              "ms",
              "启动耗时",
              synthesis === "completed"
                ? { operator: "lte" as const, value: 500, unit: "ms" }
                : null,
            ),
            metric(
              "startup.startup_analysis_get_startups.ttid_ms",
              468.358423,
              "ms",
              "TTID",
            ),
            metric(
              "startup.startup_analysis_get_startups.ttfd_ms",
              1224.982422,
              "ms",
              "TTFD",
            ),
            metric(
              "startup.startup_analysis_startup_quality.sample_count",
              1,
              "count",
              "样本数",
            ),
            metric(
              "startup.startup_analysis_main_thread_slices.total_dur_ms",
              215.5005,
              "ms",
              "主线程总耗时",
            ),
            metric(
              "startup.startup_analysis_main_thread_state_during_startup.percent",
              44.7,
              "%",
              "主线程占比",
            ),
            metric(
              "startup.startup_detail_cpu_freq_analysis.avg_freq_mhz",
              2002,
              "MHz",
              "平均频率",
            ),
            metric(
              "startup.startup_detail_cpu_core_analysis.big_core_pct",
              100,
              "%",
              "大核占比",
            ),
            ...(synthesis === "completed"
              ? [
                  metric(
                    "scroll.frame_timeline.jank_percent",
                    8.2,
                    "%",
                    "卡顿帧占比",
                  ),
                ]
              : []),
          ],
          findings: [
            {
              finding_id: "finding-critical",
              title: "**Compose:recompose** 首帧重组过重",
              summary: "**描述**：主线程独占 `61ms`，应优先拆分。",
              severity: "critical" as const,
              confidence: "high" as const,
              evidence_ids: ["evidence-1"],
            },
            {
              finding_id: "finding-warning",
              title: "Native 库加载偏慢",
              summary: "18 个库合计 37.6ms。",
              severity: "warning" as const,
              confidence: "medium" as const,
              evidence_ids: [],
            },
          ],
          evidence: [
            {
              evidence_id: "evidence-1",
              source: "perfetto.startup",
              query_id: "startup.query.1",
              interval_start_ns: 1,
              interval_end_ns: 2,
              fields: { self_ms: 61 },
            },
          ],
        },
        failure: null,
      },
    ],
  };
  if (synthesis === "failed") {
    return {
      ...base,
      synthesis: {
        state: "failed",
        output: null,
        synthesis_artifact_id: null,
        failure_code: "ai_not_configured",
        provenance: null,
      },
    };
  }
  return {
    ...base,
    synthesis: {
      state: "completed",
      output: {
        schema_version: "1.0",
        executive_summary: "**启动关键路径**仍有明确的主线程优化空间。",
        top_findings: [
          {
            finding_id: "finding-critical",
            evidence_ids: ["evidence-1"],
            user_impact: "首帧可见时间增加。",
          },
        ],
        recommendations: [],
        retest_plan: [],
        limitations: [],
      },
      synthesis_artifact_id: "artifact-1",
      failure_code: null,
      provenance: {
        provider_protocol: "chat-completions-json-schema-v1",
        provider_name: "local-provider",
        model: "local-model",
        prompt_template_version: "1.0.0",
        normalizer_version: "smartperfetto-normalizer-1",
        generated_at: "2026-08-04T08:08:00Z",
        prompt_tokens: 10,
        completion_tokens: 20,
        total_tokens: 30,
        generation: 1,
      },
    },
  };
}

function snapshot(synthesis: "failed" | "completed" = "failed"): LatestReportSnapshot {
  return {
    teamId: "team-1",
    analysis:
      synthesis === "completed"
        ? {
            ...analysis,
            state: "completed",
            stages: analysis.stages.map((stage) =>
              stage.stage === "perfpilot_ai"
                ? { ...stage, state: "completed", failure: null }
                : stage,
            ),
            failure: null,
          }
        : analysis,
    report: report(synthesis),
  } as LatestReportSnapshot;
}

describe("projectDashboardReport", () => {
  it("uses SmartPerfetto findings and exact metrics when AI synthesis is unavailable", () => {
    const projection = projectDashboardReport(snapshot());

    expect(projection.conclusion).toMatchObject({
      source: "smartperfetto",
      title: "Compose:recompose 首帧重组过重",
      summary: "描述：主线程独占 61ms，应优先拆分。",
    });
    expect(projection.startup).toMatchObject({
      state: "measured",
      value: "481.8 ms",
      target: "未配置阈值",
      breakdown: [
        { label: "TTID", value: "468.4 ms" },
        { label: "TTFD", value: "1,225 ms" },
        { label: "样本数", value: "1" },
      ],
    });
    expect(projection.secondaryMetrics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "smoothness", state: "missing", context: "本次 Trace 未采集" }),
        expect.objectContaining({ id: "main-thread", state: "measured", value: "215.5 ms" }),
        expect.objectContaining({ id: "memory", state: "missing", context: "本次 Trace 未采集" }),
        expect.objectContaining({ id: "cpu", state: "measured", value: "2,002 MHz" }),
      ]),
    );
    expect(projection.problems[0]).toMatchObject({
      id: "finding-critical",
      severity: "critical",
      confidence: "high",
      href: `/analyses/${ANALYSIS_ID}/report#finding-finding-critical`,
    });
    expect(projection.credibility).toEqual({
      sampleCount: 1,
      availableMetrics: 8,
      evidenceCount: 1,
      sourceVerification: "passed",
      failedStages: 1,
      aiState: "failed",
    });
  });

  it("prefers the completed AI conclusion and existing threshold without inventing missing memory", () => {
    const projection = projectDashboardReport(snapshot("completed"));

    expect(projection.conclusion).toMatchObject({
      source: "ai",
      summary: "启动关键路径仍有明确的主线程优化空间。",
    });
    expect(projection.startup.target).toBe("≤ 500 ms");
    expect(
      projection.secondaryMetrics.find((item) => item.id === "smoothness"),
    ).toMatchObject({ state: "measured", value: "8.2 %" });
    expect(
      projection.secondaryMetrics.find((item) => item.id === "memory"),
    ).toMatchObject({ state: "missing", value: "—" });
  });

  it("does not treat a VSync metric from the memory analysis namespace as memory usage", () => {
    const base = snapshot();
    const scenario = base.report.scenario_reports[0];
    const vsync = metric(
      "scroll.memory_analysis_get_vsync_period.detected_refresh_rate_hz",
      60,
      "value",
      "检测 VSync 周期：detected_refresh_rate_hz",
    );
    const withVsync: LatestReportSnapshot = {
      ...base,
      report: {
        ...base.report,
        scenario_reports: [
          {
            ...scenario,
            bundle: scenario.bundle
              ? { ...scenario.bundle, metrics: [vsync, ...scenario.bundle.metrics] }
              : null,
          },
        ],
      },
    };

    expect(
      projectDashboardReport(withVsync).secondaryMetrics.find(
        (item) => item.id === "memory",
      ),
    ).toMatchObject({ state: "missing", value: "—" });

    const rssGrowth = metric(
      "scroll.memory_analysis_memory_growth_summary.rss_growth_pct",
      3.2,
      "%",
      "RSS/Swap 增长趋势：rss_growth_pct",
    );
    const withMemory: LatestReportSnapshot = {
      ...withVsync,
      report: {
        ...withVsync.report,
        scenario_reports: [
          {
            ...scenario,
            bundle: scenario.bundle
              ? {
                  ...scenario.bundle,
                  metrics: [vsync, rssGrowth, ...scenario.bundle.metrics],
                }
              : null,
          },
        ],
      },
    };

    expect(
      projectDashboardReport(withMemory).secondaryMetrics.find(
        (item) => item.id === "memory",
      ),
    ).toMatchObject({
      state: "measured",
      value: "3.2 %",
      context: "RSS/Swap 增长趋势：rss_growth_pct",
    });
  });
});
