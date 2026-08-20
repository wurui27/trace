import type { FindingWorkbenchReport } from "../lib/perfpilot-api";

export interface TraceEvidenceTarget {
  readonly analysisId: string;
  readonly evidenceId: string;
}

function metricValue(value: number | string | null, unit: string | null): string {
  if (value === null) return "未采集";
  return unit ? `${value} ${unit}` : String(value);
}

export function EvidenceMetricsPanel({
  openEvidence,
  report,
  selectedFindingId,
}: {
  readonly openEvidence: (target: TraceEvidenceTarget) => void;
  readonly report: FindingWorkbenchReport;
  readonly selectedFindingId: string | null;
}) {
  const finding = report.workbench.findings.find((item) => item.finding_id === selectedFindingId) ?? report.workbench.findings[0] ?? null;
  const metrics = finding
    ? finding.metric_ids.flatMap((id) => report.workbench.metrics.filter((metric) => metric.metric_id === id))
    : [];
  const evidence = finding
    ? finding.evidence_ids.flatMap((id) => report.workbench.evidence.filter((item) => item.evidence_id === id))
    : report.workbench.evidence;

  return (
    <section className="finding-evidence-panel" aria-labelledby="finding-evidence-title">
      <div className="finding-section-heading">
        <div>
          <p className="section-label">TRACE EVIDENCE</p>
          <h2 id="finding-evidence-title">证据与指标</h2>
        </div>
        <span>{evidence.length} 条证据</span>
      </div>
      <dl className="finding-metric-grid" aria-label="关联指标">
        {metrics.map((metric) => (
          <div key={metric.metric_id}>
            <dt>{metric.name}</dt>
            <dd>{metricValue(metric.value, metric.unit)}</dd>
            <small>{metric.aggregation} · {metric.quality}</small>
          </div>
        ))}
      </dl>
      <ol className="finding-evidence-list">
        {evidence.map((item) => (
          <li key={item.evidence_id}>
            <div>
              <strong>{item.summary}</strong>
              <p>{item.source}</p>
              {item.locator ? (
                <dl>
                  <div><dt>区间</dt><dd>{item.locator.start_ns} – {item.locator.end_ns} ns</dd></div>
                  {item.locator.process ? <div><dt>进程</dt><dd>{item.locator.process}</dd></div> : null}
                  {item.locator.thread ? <div><dt>线程</dt><dd>{item.locator.thread}</dd></div> : null}
                  {item.locator.slice ? <div><dt>切片</dt><dd>{item.locator.slice}</dd></div> : null}
                </dl>
              ) : <p className="finding-evidence-unlocated">该证据没有可验证的时间区间，不生成伪定位链接。</p>}
            </div>
            {item.locator ? (
              <button
                onClick={() => openEvidence({ analysisId: report.analysis_id, evidenceId: item.evidence_id })}
                type="button"
              >
                在 Trace 中打开证据
              </button>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}
