import type {
  ConciseSynthesisOutput,
  SourceAwareAnalysisReport,
} from "../lib/perfpilot-api";
import {
  redactUnverifiedConclusion,
  redactUnverifiedSourceNarrative,
} from "../lib/source-report-privacy";

type Conclusion = ConciseSynthesisOutput["conclusions"][number];

function ConclusionBody({ conclusion }: { readonly conclusion: Conclusion }) {
  return (
    <dl className="analysis-conclusion-grid">
      <div>
        <dt>1. 问题点</dt>
        <dd>{conclusion.problem}</dd>
      </div>
      <div>
        <dt>2. 为什么会有这个问题</dt>
        <dd>{conclusion.cause}</dd>
      </div>
      <div>
        <dt>3. 结合源码判断的根因是什么</dt>
        <dd>{conclusion.source_root_cause}</dd>
      </div>
      <div>
        <dt>4. 修改建议</dt>
        <dd>{conclusion.recommendation}</dd>
      </div>
    </dl>
  );
}

export function ConciseReportSummary({ report }: { readonly report: SourceAwareAnalysisReport }) {
  const output = report.synthesis.state === "completed" ? report.synthesis.output : null;
  if (output === null) {
    return (
      <section className="analysis-report-section" aria-label="结论">
        <h2>PerfPilot AI 中文总结生成失败</h2>
        <p>PerfPilot AI 中文总结生成失败；SmartPerfetto 原始报告和核心 Trace 结论仍可查看。</p>
      </section>
    );
  }
  const metrics = report.scenario_reports.flatMap((scenario) =>
    scenario.bundle?.metrics ?? [],
  );
  const metricsById = new Map(metrics.map((metric) => [metric.metric_id, metric]));
  const conclusions = output.conclusions.map((conclusion) =>
    redactUnverifiedConclusion(conclusion, report.source_code),
  );
  const redact = (value: string) =>
    redactUnverifiedSourceNarrative(value, report.source_code);
  const primary = conclusions.slice(0, 3);
  const additional = conclusions.slice(3);

  return (
    <section className="source-aware-conclusion" aria-label="结论">
      <header className="analysis-report-section analysis-report-summary">
        <p className="section-label">PERFPILOT CONCLUSION</p>
        <h2>{redact(output.verdict)}</h2>
        <p>{redact(output.executive_summary)}</p>
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
        <h2>主要问题与优化方案</h2>
        <div className="analysis-primary-conclusions">
          {primary.map((conclusion, index) => (
            <article key={conclusion.finding_id} data-testid="primary-conclusion">
              <p className="section-label">主要问题 {index + 1}</p>
              <ConclusionBody conclusion={conclusion} />
            </article>
          ))}
        </div>
        {additional.length ? (
          <details className="analysis-additional-conclusions">
            <summary>展开其余 {additional.length} 条问题与优化方案</summary>
            {additional.map((conclusion) => (
              <details key={conclusion.finding_id} data-testid="additional-conclusion">
                <summary>{conclusion.problem}</summary>
                <ConclusionBody conclusion={conclusion} />
              </details>
            ))}
          </details>
        ) : null}
      </section>

      {output.limitations[0] ? (
        <aside className="analysis-report-section source-aware-limitation">
          <strong>结论边界</strong>
          <p>{redact(output.limitations[0].summary)}</p>
        </aside>
      ) : null}
    </section>
  );
}
