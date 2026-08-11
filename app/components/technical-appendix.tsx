import type { SourceAwareAnalysisReport } from "../lib/perfpilot-api";

export function TechnicalAppendix({ report }: { readonly report: SourceAwareAnalysisReport }) {
  return (
    <section className="technical-appendix" aria-label="技术附录">
      <section className="analysis-report-section">
        <h2>SmartPerfetto 指标与证据</h2>
        {report.scenario_reports.map((scenario) => (
          <details key={scenario.scenario_job_id}>
            <summary>{scenario.scenario_type}</summary>
            <dl className="analysis-report-metrics">
              {(scenario.bundle?.metrics ?? []).map((metric) => (
                <div key={metric.metric_id}>
                  <dt>{metric.definition}</dt>
                  <dd>{metric.numeric_value ?? metric.status}{metric.unit ? ` ${metric.unit}` : ""}</dd>
                </div>
              ))}
            </dl>
            {(scenario.bundle?.evidence ?? []).map((evidence) => (
              <details key={evidence.evidence_id}>
                <summary>{evidence.source} · {evidence.query_id}</summary>
                <pre>{JSON.stringify(evidence, null, 2)}</pre>
              </details>
            ))}
          </details>
        ))}
      </section>

      <section className="analysis-report-section">
        <h2>源码证据边界</h2>
        {report.source_code.snapshot ? (
          <p>Snapshot {report.source_code.snapshot.snapshot_hash}</p>
        ) : (
          <p>本次没有源码快照。</p>
        )}
        {report.source_code.match_summary === "strong" ? (
          report.source_code.source_refs.map((reference) => (
            <p key={reference.source_ref_id}>
              {reference.relative_path} · {reference.start_line}-{reference.end_line} · {reference.match_grade}
            </p>
          ))
        ) : (
          <p>源码匹配不足，未公开文件路径或行号。</p>
        )}
        {report.source_code.exclusions.map((exclusion, index) => (
          <p key={`${exclusion.reason_code}:${index}`}>
            {report.source_code.match_summary === "strong" && exclusion.relative_path
              ? `${exclusion.relative_path} · `
              : null}
            {exclusion.reason_code}
          </p>
        ))}
      </section>

      <section className="analysis-report-section">
        <h2>生成信息</h2>
        {report.synthesis.provenance ? (
          <dl>
            <dt>Provider</dt><dd>{report.synthesis.provenance.provider_name}</dd>
            <dt>模型</dt><dd>{report.synthesis.provenance.model}</dd>
            <dt>Prompt</dt><dd>{report.synthesis.provenance.prompt_template_version}</dd>
            <dt>Normalizer</dt><dd>{report.synthesis.provenance.normalizer_version}</dd>
          </dl>
        ) : <p>AI 生成失败，未产生 Provider 信息。</p>}
      </section>
    </section>
  );
}
