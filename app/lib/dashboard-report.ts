import type {
  AnalysisListItem,
  AnalysisReport,
  ReportFinding,
  ReportMetric,
} from "./perfpilot-api";

export type DashboardMetricState = "measured" | "missing";

export interface DashboardReportProjection {
  readonly conclusion: {
    readonly source: "ai" | "smartperfetto";
    readonly title: string;
    readonly summary: string;
    readonly href: string;
  };
  readonly startup: {
    readonly state: DashboardMetricState;
    readonly value: string;
    readonly target: string;
    readonly context: string;
    readonly breakdown: ReadonlyArray<{
      readonly label: string;
      readonly value: string;
    }>;
  };
  readonly secondaryMetrics: ReadonlyArray<{
    readonly id: "smoothness" | "main-thread" | "memory" | "cpu";
    readonly label: string;
    readonly state: DashboardMetricState;
    readonly value: string;
    readonly context: string;
  }>;
  readonly problems: ReadonlyArray<{
    readonly id: string;
    readonly title: string;
    readonly summary: string;
    readonly severity: ReportFinding["severity"];
    readonly confidence: ReportFinding["confidence"];
    readonly href: string;
  }>;
  readonly credibility: {
    readonly sampleCount: number;
    readonly availableMetrics: number;
    readonly evidenceCount: number;
    readonly sourceVerification: "passed" | "failed" | "unknown";
    readonly failedStages: number;
    readonly aiState: "completed" | "failed";
  };
}

interface DashboardReportSnapshot {
  readonly analysis: AnalysisListItem;
  readonly report: AnalysisReport;
}

const numberFormat = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 1,
});
const severityOrder: Record<ReportFinding["severity"], number> = {
  critical: 0,
  warning: 1,
  informational: 2,
  healthy: 3,
};
const confidenceOrder: Record<ReportFinding["confidence"], number> = {
  high: 0,
  medium: 1,
  low: 2,
  none: 3,
};

function plainText(value: string): string {
  return value
    .replace(/\[([^\]]+)\]\([^\s)]+\)/g, "$1")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/`{1,3}([^`\n]+)`{1,3}/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/~~(.*?)~~/g, "$1")
    .trim();
}

function availableMetric(metric: ReportMetric): boolean {
  return metric.status === "available" && metric.numeric_value !== null;
}

function selectMetric(
  metrics: readonly ReportMetric[],
  exactNames: readonly string[],
  suffixes: readonly string[] = [],
): ReportMetric | null {
  for (const name of exactNames) {
    const exact = metrics.find((metric) => metric.name === name && availableMetric(metric));
    if (exact) return exact;
  }
  for (const suffix of suffixes) {
    const match = metrics.find(
      (metric) => metric.name.endsWith(suffix) && availableMetric(metric),
    );
    if (match) return match;
  }
  return null;
}

function formatNumber(value: number | null): string {
  return value === null ? "—" : numberFormat.format(value);
}

function formatMetric(metric: ReportMetric | null, includeUnit = true): string {
  if (!metric || metric.numeric_value === null) return "—";
  const value = formatNumber(metric.numeric_value);
  return includeUnit && metric.unit ? `${value} ${metric.unit}` : value;
}

function missingMetric(
  id: DashboardReportProjection["secondaryMetrics"][number]["id"],
  label: string,
): DashboardReportProjection["secondaryMetrics"][number] {
  return { id, label, state: "missing", value: "—", context: "本次 Trace 未采集" };
}

function genericFamilyMetric(
  metrics: readonly ReportMetric[],
  family: "smoothness" | "memory",
): ReportMetric | null {
  const patterns =
    family === "smoothness"
      ? [/jank.*(?:percent|pct)/i, /frame.*(?:duration|time|percent|pct)/i]
      : [/(?:^|[._])(?:memory|pss|rss|heap)(?:[._]|$)/i];
  return (
    metrics.find(
      (metric) => {
        const leafName = metric.name.split(".").at(-1) ?? metric.name;
        return (
          availableMetric(metric) &&
          patterns.some((pattern) => pattern.test(leafName))
        );
      },
    ) ?? null
  );
}

function orderedFindings(
  findings: readonly ReportFinding[],
  report: AnalysisReport,
): readonly ReportFinding[] {
  const byId = new Map(findings.map((finding) => [finding.finding_id, finding]));
  if (report.synthesis.state === "completed") {
    const selected = report.synthesis.output.top_findings
      .map((item) => byId.get(item.finding_id))
      .filter((finding): finding is ReportFinding => finding !== undefined);
    if (selected.length > 0) return selected;
  }
  return [...findings].sort(
    (left, right) =>
      severityOrder[left.severity] - severityOrder[right.severity] ||
      confidenceOrder[left.confidence] - confidenceOrder[right.confidence],
  );
}

function thresholdLabel(metric: ReportMetric | null): string {
  if (!metric?.threshold) return "未配置阈值";
  const operators: Record<
    NonNullable<ReportMetric["threshold"]>["operator"],
    string
  > = {
    gt: ">",
    gte: "≥",
    lt: "<",
    lte: "≤",
    eq: "=",
  };
  return `${operators[metric.threshold.operator]} ${formatNumber(metric.threshold.value)} ${metric.threshold.unit}`;
}

export function projectDashboardReport(
  snapshot: DashboardReportSnapshot,
): DashboardReportProjection {
  const { analysis, report } = snapshot;
  const bundles = report.scenario_reports.flatMap((scenario) =>
    scenario.bundle ? [scenario.bundle] : [],
  );
  const metrics = bundles.flatMap((bundle) => bundle.metrics);
  const findings = bundles.flatMap((bundle) => bundle.findings);
  const sortedFindings = orderedFindings(findings, report);
  const reportHref = `/analyses/${analysis.analysis_id}/report`;

  const startupDuration = selectMetric(
    metrics,
    [
      "startup.startup_analysis_get_startups.dur_ms",
      "startup.startup_detail_startup_info.dur_ms",
      "startup.startup_slow_reasons_startup_overview.dur_ms",
      "startup.startup_analysis_analyze_startups.dur_ms",
    ],
    [".startup_info.dur_ms", ".startup_overview.dur_ms"],
  );
  const ttid = selectMetric(
    metrics,
    [
      "startup.startup_analysis_get_startups.ttid_ms",
      "startup.startup_slow_reasons_startup_overview.ttid_ms",
    ],
    [".ttid_ms"],
  );
  const ttfd = selectMetric(
    metrics,
    [
      "startup.startup_analysis_get_startups.ttfd_ms",
      "startup.startup_slow_reasons_startup_overview.ttfd_ms",
    ],
    [".ttfd_ms"],
  );
  const sampleCount = selectMetric(metrics, [
    "startup.startup_analysis_startup_quality.sample_count",
  ], [".sample_count"]);
  const mainThread = selectMetric(
    metrics,
    [
      "startup.startup_analysis_main_thread_slices.total_dur_ms",
      "startup.startup_analysis_main_thread_state_during_startup.total_dur_ms",
    ],
    [".main_thread_slices.total_dur_ms"],
  );
  const mainThreadPercent = selectMetric(metrics, [
    "startup.startup_analysis_main_thread_state_during_startup.percent",
  ]);
  const cpu = selectMetric(
    metrics,
    [
      "startup.startup_detail_cpu_freq_analysis.avg_freq_mhz",
      "startup.startup_detail_cpu_freq_analysis.max_freq_mhz",
      "startup.startup_detail_init_cpu_topology.max_freq_mhz",
    ],
    [".cpu_freq_analysis.avg_freq_mhz", ".cpu_freq_analysis.max_freq_mhz"],
  );
  const bigCore = selectMetric(metrics, [
    "startup.startup_detail_cpu_core_analysis.big_core_pct",
    "startup.startup_detail_critical_tasks.big_core_pct",
  ]);
  const smoothness = selectMetric(
    metrics,
    ["scroll.frame_timeline.jank_percent"],
  ) ?? genericFamilyMetric(metrics, "smoothness");
  const memory = genericFamilyMetric(metrics, "memory");

  const problems = sortedFindings.slice(0, 3).map((finding) => ({
    id: finding.finding_id,
    title: plainText(finding.title),
    summary: plainText(finding.summary),
    severity: finding.severity,
    confidence: finding.confidence,
    href: `${reportHref}#finding-${finding.finding_id}`,
  }));
  const fallback = problems[0];
  const conclusion =
    report.synthesis.state === "completed"
      ? {
          source: "ai" as const,
          title: "PerfPilot AI 最终结论",
          summary: plainText(report.synthesis.output.executive_summary),
          href: reportHref,
        }
      : {
          source: "smartperfetto" as const,
          title: fallback?.title ?? "未发现明确性能问题",
          summary: fallback?.summary ?? "SmartPerfetto 未返回可展示的问题。",
          href: reportHref,
        };

  const secondaryMetrics: DashboardReportProjection["secondaryMetrics"] = [
    smoothness
      ? {
          id: "smoothness",
          label: "页面流畅度",
          state: "measured",
          value: formatMetric(smoothness),
          context: plainText(smoothness.definition),
        }
      : missingMetric("smoothness", "页面流畅度"),
    mainThread
      ? {
          id: "main-thread",
          label: "主线程响应",
          state: "measured",
          value: formatMetric(mainThread),
          context: mainThreadPercent
            ? `启动期间主线程占比 ${formatMetric(mainThreadPercent)}`
            : plainText(mainThread.definition),
        }
      : missingMetric("main-thread", "主线程响应"),
    memory
      ? {
          id: "memory",
          label: "内存稳定性",
          state: "measured",
          value: formatMetric(memory),
          context: plainText(memory.definition),
        }
      : missingMetric("memory", "内存稳定性"),
    cpu
      ? {
          id: "cpu",
          label: "CPU 与调度",
          state: "measured",
          value: formatMetric(cpu),
          context: bigCore
            ? `启动期间大核占比 ${formatMetric(bigCore)}`
            : plainText(cpu.definition),
        }
      : missingMetric("cpu", "CPU 与调度"),
  ];

  return {
    conclusion,
    startup: {
      state: startupDuration ? "measured" : "missing",
      value: formatMetric(startupDuration),
      target: thresholdLabel(startupDuration),
      context: startupDuration
        ? sampleCount?.numeric_value
          ? `基于 ${formatNumber(sampleCount.numeric_value)} 次启动样本`
          : plainText(startupDuration.definition)
        : "本次 Trace 未采集启动耗时",
      breakdown: [
        { label: "TTID", value: formatMetric(ttid) },
        { label: "TTFD", value: formatMetric(ttfd) },
        { label: "样本数", value: formatMetric(sampleCount, false) },
      ],
    },
    secondaryMetrics,
    problems,
    credibility: {
      sampleCount: sampleCount?.numeric_value ?? 0,
      availableMetrics: metrics.filter(availableMetric).length,
      evidenceCount: new Set(
        bundles.flatMap((bundle) => bundle.evidence.map((evidence) => evidence.evidence_id)),
      ).size,
      sourceVerification: analysis.source_analysis?.verification ?? "unknown",
      failedStages: analysis.stages.filter((stage) => stage.state === "failed").length,
      aiState: report.synthesis.state,
    },
  };
}
