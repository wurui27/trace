import type { FindingWorkbenchReport } from "../lib/perfpilot-api";

export function FindingSourcePanel({ report }: { readonly report: FindingWorkbenchReport }) {
  const conclusions = report.synthesis.state === "completed" ? report.synthesis.output.conclusions : [];
  if (report.capabilities.source !== "matched" || report.source_code?.match_summary !== "strong") {
    const title = report.capabilities.source === "mismatch" ? "源码不匹配" :
      report.capabilities.source === "unavailable" ? "源码不可用" : "本次未关联源码";
    return (
      <section className="finding-source-panel" aria-labelledby="finding-source-title">
        <div className="finding-section-heading">
          <div>
            <p className="section-label">SOURCE-SAFE ACTIONS</p>
            <h2 id="finding-source-title">{title}</h2>
          </div>
        </div>
        <p>本次保留基于 SmartPerfetto 证据的修改方案，不展示文件位置或 Diff。</p>
        <p className="finding-reference-notice">修改仅供参考</p>
        <ol className="finding-manual-actions" aria-label="手动优化建议">
          {conclusions.map((conclusion) => (
            <li key={conclusion.finding_id}>{conclusion.recommendation}</li>
          ))}
        </ol>
      </section>
    );
  }

  const refs = new Map(report.source_code.source_refs.map((reference) => [reference.source_ref_id, reference]));
  return (
    <section className="finding-source-panel" aria-labelledby="finding-source-title">
      <div className="finding-section-heading">
        <div>
          <p className="section-label">SOURCE CORRELATION</p>
          <h2 id="finding-source-title">源码与优化</h2>
        </div>
        <span>{report.source_code.fixes.length} 项 Diff</span>
      </div>
      <p className="finding-reference-notice">修改仅供参考，请结合实际业务逻辑审查后再应用。</p>
      <ol className="finding-source-fixes">
        {report.source_code.fixes.map((fix) => (
          <li key={fix.fix_id}>
            <header>
              <span className={`finding-priority is-${fix.recommendation_priority}`}>
                {fix.recommendation_priority.toUpperCase()}
              </span>
              <div>
                <strong>{fix.relative_path}</strong>
                <small>{fix.symbol ?? "未识别符号"}</small>
              </div>
            </header>
            <p>{fix.diagnosis}</p>
            <pre aria-label="建议代码 Diff"><code>{fix.diff}</code></pre>
            <p>复测：{fix.retest_target}</p>
            <small>
              关联源码：{fix.source_ref_ids.map((id) => refs.get(id)?.relative_path).filter(Boolean).join("、")}
            </small>
          </li>
        ))}
      </ol>
      {report.source_code.fixes.length === 0 ? (
        <p className="finding-empty">源码已强匹配，但当前没有可安全生成的 Diff；请按问题清单中的方案人工修改。</p>
      ) : null}
    </section>
  );
}
