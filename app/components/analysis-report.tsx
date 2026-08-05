"use client";

import type {
  AnalysisReport,
  ReportEvidence,
  ReportFinding,
  ReportMetric,
} from "../lib/perfpilot-api";

interface AnalysisReportViewProps {
  readonly report: AnalysisReport;
  readonly onRetrySynthesis: () => void | Promise<void>;
  readonly retrying: boolean;
}

const priorityOrder = { p0: 0, p1: 1, p2: 2, p3: 3 } as const;
const scenarioLabels = {
  cold_start: "冷启动",
  startup: "启动",
  scroll: "滑动",
  memory_cycle: "内存循环",
} as const;
const severityLabels = {
  critical: "严重",
  warning: "需关注",
  healthy: "正常",
  informational: "信息",
} as const;
const confidenceLabels = {
  high: "高可信",
  medium: "中可信",
  low: "低可信",
  none: "无可信度",
} as const;
const primaryMetricLimit = 8;

function plainReportText(value: string): string {
  return value
    .replace(/\[([^\]]+)\]\([^\s)]+\)/g, "$1")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/`{1,3}([^`\n]+)`{1,3}/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/~~(.*?)~~/g, "$1")
    .trim();
}

function metricValue(metric: ReportMetric): string | null {
  if (metric.status !== "available" || metric.numeric_value === null) return null;
  return metric.unit ? `${metric.numeric_value} ${metric.unit}` : String(metric.numeric_value);
}

function generatedAt(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false,
    timeZone: "Asia/Shanghai",
  }).format(parsed);
}

function evidenceCopy(evidence: ReportEvidence): string {
  const interval =
    evidence.interval_start_ns === null || evidence.interval_end_ns === null
      ? null
      : `${evidence.interval_start_ns} 至 ${evidence.interval_end_ns} ns`;
  return [evidence.source, evidence.query_id, interval].filter(Boolean).join(" · ");
}

export function AnalysisReportView({
  report,
  onRetrySynthesis,
  retrying,
}: AnalysisReportViewProps) {
  const bundles = report.scenario_reports.flatMap((scenario) =>
    scenario.bundle === null ? [] : [{ scenario: scenario.scenario_type, bundle: scenario.bundle }],
  );
  const metrics = bundles.flatMap(({ scenario, bundle }) =>
    bundle.metrics.map((metric) => ({ scenario, metric })),
  );
  const availableMetrics = metrics.flatMap(({ scenario, metric }) => {
    const value = metricValue(metric);
    return value === null ? [] : [{ scenario, metric, value }];
  });
  const primaryMetrics = availableMetrics.slice(0, primaryMetricLimit);
  const additionalMetrics = availableMetrics.slice(primaryMetricLimit);
  const coreFindings = bundles.flatMap(({ bundle }) => bundle.findings);
  const findingsById = new Map(coreFindings.map((finding) => [finding.finding_id, finding]));
  const evidenceById = new Map(
    bundles.flatMap(({ bundle }) => bundle.evidence).map((evidence) => [evidence.evidence_id, evidence]),
  );
  const output = report.synthesis.state === "completed" ? report.synthesis.output : null;
  const visibleFindings: Array<{
    finding: ReportFinding;
    evidenceIds: readonly string[];
    userImpact: string | null;
  }> = output
    ? output.top_findings
        .slice(0, 5)
        .map((item) => ({
          finding: findingsById.get(item.finding_id),
          evidenceIds: item.evidence_ids,
          userImpact: item.user_impact,
        }))
        .filter(
          (item): item is {
            finding: ReportFinding;
            evidenceIds: readonly string[];
            userImpact: string;
          } => item.finding !== undefined,
        )
    : coreFindings.slice(0, 5).map((finding) => ({
        finding,
        evidenceIds: finding.evidence_ids,
        userImpact: null,
      }));
  const visibleEvidence = Array.from(
    new Set(visibleFindings.flatMap((item) => [...item.evidenceIds])),
  )
    .map((evidenceId) => evidenceById.get(evidenceId))
    .filter((evidence): evidence is ReportEvidence => evidence !== undefined);
  const recommendations = output
    ? [...output.recommendations].sort(
        (left, right) => priorityOrder[left.priority] - priorityOrder[right.priority],
      )
    : [];
  const scenarioFailures = report.scenario_reports.flatMap((scenario) =>
    scenario.failure === null ? [] : [scenario.failure.message],
  );
  const limitations = output
    ? [...output.limitations.map((item) => item.summary), ...scenarioFailures]
    : scenarioFailures;

  return (
    <article className="analysis-report-card" aria-label="PerfPilot 分析报告">
      {output === null ? (
        <div className="analysis-report-partial" role="status">
          <div>
            <strong>
              {report.synthesis.state === "not_requested"
                ? "真机内核报告已生成"
                : "内核分析已完成，AI 建议暂未生成"}
            </strong>
            <p>
              {report.synthesis.state === "not_requested"
                ? "当前报告包含 SmartPerfetto 的指标、问题和证据。"
                : "SmartPerfetto 的指标、问题和证据仍可查看。你可以只重新生成 AI 建议。"}
            </p>
          </div>
          {report.synthesis.state === "failed" ? (
            <button type="button" onClick={onRetrySynthesis} disabled={retrying}>
              {retrying ? "正在重新生成" : "重新生成 AI 建议"}
            </button>
          ) : null}
        </div>
      ) : null}

      {output ? (
        <section className="analysis-report-section analysis-report-summary" aria-labelledby="report-summary-title">
          <p className="section-label">PERFPILOT CONCLUSION</p>
          <h2 id="report-summary-title">执行摘要</h2>
          <p>{plainReportText(output.executive_summary)}</p>
        </section>
      ) : null}

      <section className="analysis-report-section" aria-labelledby="report-findings-title">
        <div className="analysis-report-heading">
          <div>
            <p className="section-label">SMARTPERFETTO EVIDENCE</p>
            <h2 id="report-findings-title">重点问题</h2>
          </div>
          <span>{visibleFindings.length} 项</span>
        </div>

        {availableMetrics.length > 0 ? (
          <>
            <dl className="analysis-report-metrics" aria-label="关键场景指标">
              {primaryMetrics.map(({ scenario, metric, value }) => (
                <div key={metric.metric_id}>
                  <dt>{scenarioLabels[scenario]} · {plainReportText(metric.definition)}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
            {additionalMetrics.length > 0 ? (
              <details className="analysis-report-metric-details">
                <summary>另有 {additionalMetrics.length} 项原始指标</summary>
                <dl className="analysis-report-metrics" aria-label="其余场景指标">
                  {additionalMetrics.map(({ scenario, metric, value }) => (
                    <div key={metric.metric_id}>
                      <dt>{scenarioLabels[scenario]} · {plainReportText(metric.definition)}</dt>
                      <dd>{value}</dd>
                    </div>
                  ))}
                </dl>
              </details>
            ) : null}
          </>
        ) : null}

        {visibleFindings.length > 0 ? (
          <ol className="analysis-report-findings">
            {visibleFindings.map(({ finding, evidenceIds, userImpact }) => (
              <li id={`finding-${finding.finding_id}`} key={finding.finding_id}>
                <div className="analysis-finding-meta">
                  <span className={`is-${finding.severity}`}>{severityLabels[finding.severity]}</span>
                  <span>{confidenceLabels[finding.confidence]}</span>
                </div>
                <h3>{plainReportText(finding.title)}</h3>
                <p>{plainReportText(finding.summary)}</p>
                {userImpact ? <p className="analysis-user-impact">用户影响：{plainReportText(userImpact)}</p> : null}
                {evidenceIds.length > 0 ? (
                  <div className="analysis-reference-list" aria-label="关联证据">
                    {evidenceIds.map((evidenceId, index) => (
                      <a href={`#evidence-${evidenceId}`} key={evidenceId}>
                        证据 {index + 1}
                      </a>
                    ))}
                  </div>
                ) : null}
              </li>
            ))}
          </ol>
        ) : (
          <p className="analysis-report-empty">内核没有返回可展示的问题。</p>
        )}

        {visibleEvidence.length > 0 ? (
          <div className="analysis-report-evidence" aria-label="问题证据">
            {visibleEvidence.map((evidence, index) => (
              <div id={`evidence-${evidence.evidence_id}`} key={evidence.evidence_id}>
                <strong>证据 {index + 1}</strong>
                <p>{evidenceCopy(evidence)}</p>
                {Object.keys(evidence.fields).length > 0 ? (
                  <details>
                    <summary>查看原始字段</summary>
                    <pre>{JSON.stringify(evidence.fields, null, 2)}</pre>
                  </details>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}
      </section>

      {output && recommendations.length > 0 ? (
        <section className="analysis-report-section" aria-labelledby="report-recommendations-title">
          <p className="section-label">ACTION PLAN</p>
          <h2 id="report-recommendations-title">优化建议</h2>
          <ol className="analysis-recommendation-list">
            {recommendations.map((recommendation, index) => (
              <li key={`${recommendation.priority}-${index}`}>
                <span className={`analysis-priority is-${recommendation.priority}`} data-testid="recommendation-priority">
                  {recommendation.priority.toUpperCase()}
                </span>
                <div>
                  <h3>{plainReportText(recommendation.title)}</h3>
                  <p>{plainReportText(recommendation.action)}</p>
                  <p className="analysis-expected-effect">预期效果：{plainReportText(recommendation.expected_effect)}</p>
                  <div className="analysis-reference-list" aria-label="建议依据">
                    {recommendation.finding_ids.map((findingId) => (
                      <a href={`#finding-${findingId}`} key={findingId}>关联问题</a>
                    ))}
                    {recommendation.evidence_ids.map((evidenceId) => (
                      <a href={`#evidence-${evidenceId}`} key={evidenceId}>关联证据</a>
                    ))}
                  </div>
                </div>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {output && output.retest_plan.length > 0 ? (
        <section className="analysis-report-section" aria-labelledby="report-retest-title">
          <p className="section-label">VERIFY THE CHANGE</p>
          <h2 id="report-retest-title">复测计划</h2>
          <ol className="analysis-retest-list">
            {output.retest_plan.map((item, index) => (
              <li key={`${item.mode}-${item.scenario_type}-${index}`}>
                <span>{index + 1}</span>
                <div>
                  <strong>{scenarioLabels[item.scenario_type]}</strong>
                  <p>{plainReportText(item.steps)}</p>
                  <small>
                    {item.mode === "verify_metric" ? "验证现有指标与阈值" : "补齐缺失证据"}
                  </small>
                </div>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      <section className="analysis-report-section" aria-labelledby="report-limitations-title">
        <p className="section-label">EVIDENCE BOUNDARY</p>
        <h2 id="report-limitations-title">限制与缺失证据</h2>
        {limitations.length > 0 ? (
          <ul className="analysis-limitation-list">
            {limitations.map((limitation, index) => <li key={`${limitation}-${index}`}>{plainReportText(limitation)}</li>)}
          </ul>
        ) : (
          <p className="analysis-report-empty">当前报告未标记限制。</p>
        )}
      </section>

      <details className="analysis-report-provenance">
        <summary>生成信息</summary>
        <dl>
          <div><dt>报告版本</dt><dd>v{report.report_version}</dd></div>
          <div><dt>生成时间</dt><dd>{generatedAt(report.generated_at)}</dd></div>
          {report.synthesis.provenance ? (
            <>
              <div><dt>生成批次</dt><dd>{report.synthesis.provenance.generation}</dd></div>
              <div><dt>AI 模型</dt><dd>{report.synthesis.provenance.provider_name} · {report.synthesis.provenance.model}</dd></div>
              <div><dt>提示模板</dt><dd>{report.synthesis.provenance.prompt_template_version}</dd></div>
              <div><dt>内核归一化</dt><dd>{report.synthesis.provenance.normalizer_version}</dd></div>
            </>
          ) : (
            <div>
              <dt>AI 生成</dt>
              <dd>{report.synthesis.state === "not_requested" ? "当前报告未包含" : "未完成"}</dd>
            </div>
          )}
        </dl>
      </details>
    </article>
  );
}
