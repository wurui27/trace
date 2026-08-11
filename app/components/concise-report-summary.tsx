import type { SourceAwareAnalysisReport } from "../lib/perfpilot-api";

export function ConciseReportSummary({ report }: { readonly report: SourceAwareAnalysisReport }) {
  const output = report.synthesis.state === "completed" ? report.synthesis.output : null;
  if (output === null) {
    return (
      <section className="analysis-report-section" aria-label="结论">
        <h2>AI 最终结论未生成</h2>
        <p>内核证据和技术附录仍可查看。</p>
      </section>
    );
  }
  const metrics = report.scenario_reports.flatMap((scenario) =>
    scenario.bundle?.metrics ?? [],
  );
  const metricsById = new Map(metrics.map((metric) => [metric.metric_id, metric]));
  const findings = report.scenario_reports.flatMap((scenario) =>
    scenario.bundle?.findings ?? [],
  );
  const findingsById = new Map(findings.map((finding) => [finding.finding_id, finding]));

  return (
    <section className="source-aware-conclusion" aria-label="结论">
      <header className="analysis-report-section analysis-report-summary">
        <p className="section-label">PERFPILOT CONCLUSION</p>
        <h2>{output.verdict}</h2>
        <p>{output.executive_summary}</p>
      </header>

      <section className="analysis-report-section">
        <h2>关键指标</h2>
        <dl className="analysis-report-metrics">
          {output.key_metric_ids.slice(0, 3).map((metricId) => {
            const metric = metricsById.get(metricId);
            if (!metric) return null;
            return (
              <div key={metricId} data-testid="key-metric">
                <dt>{metric.definition}</dt>
                <dd>
                  {metric.status === "available" && metric.numeric_value !== null
                    ? `${metric.numeric_value}${metric.unit ? ` ${metric.unit}` : ""}`
                    : "证据不足"}
                </dd>
              </div>
            );
          })}
        </dl>
      </section>

      <section className="analysis-report-section">
        <h2>最痛的问题</h2>
        <ol className="analysis-report-findings">
          {output.top_findings.slice(0, 3).map((item) => {
            const finding = findingsById.get(item.finding_id);
            if (!finding) return null;
            return (
              <li key={item.finding_id} data-testid="top-finding">
                <h3>{finding.title}</h3>
                <p>{finding.summary}</p>
                <p className="analysis-user-impact">用户影响：{item.user_impact}</p>
              </li>
            );
          })}
        </ol>
      </section>

      <section className="analysis-report-section">
        <h2>优先优化方案</h2>
        <ol className="analysis-recommendation-list">
          {output.recommendations.slice(0, 3).map((recommendation) => (
            <li key={recommendation.priority} data-testid="priority-action">
              <span className={`analysis-priority is-${recommendation.priority}`}>
                {recommendation.priority.toUpperCase()}
              </span>
              <div>
                <h3>{recommendation.title}</h3>
                <p>{recommendation.action}</p>
                <p className="analysis-expected-effect">
                  预期效果：{recommendation.expected_effect}
                </p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      {output.limitations[0] ? (
        <aside className="analysis-report-section source-aware-limitation">
          <strong>结论边界</strong>
          <p>{output.limitations[0].summary}</p>
        </aside>
      ) : null}
    </section>
  );
}
