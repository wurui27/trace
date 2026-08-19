import type { SourceAwareAnalysisReport, SourceFix } from "../lib/perfpilot-api";
import { redactUnverifiedConclusion } from "../lib/source-report-privacy";

const verificationCopy: Record<SourceFix["verification"]["state"], string> = {
  not_requested: "未执行自动验证",
  pending: "等待验证源码修复",
  validating: "正在验证源码修复",
  verified: "修复已验证通过",
  apply_failed: "修复无法应用到源码快照",
  validation_failed: "修复应用成功，但验证任务未通过",
  source_changed: "源码已变化，需要重新生成修复",
  not_configured: "未配置源码验证方案",
  timeout: "源码验证超时",
  canceled: "源码验证已取消",
  unavailable: "源码验证暂时不可用",
};

export function SourceFixesPanel({ report }: { readonly report: SourceAwareAnalysisReport }) {
  const source = report.source_code;
  const output = report.synthesis.state === "completed" ? report.synthesis.output : null;
  const conclusions = (output?.conclusions ?? []).map((conclusion) =>
    redactUnverifiedConclusion(conclusion, source),
  );
  const fixes = source.match_summary === "strong" ? source.fixes : [];
  const fixesByFinding = new Map<string, SourceFix[]>();
  for (const fix of fixes) {
    const current = fixesByFinding.get(fix.finding_id) ?? [];
    current.push(fix);
    fixesByFinding.set(fix.finding_id, current);
  }
  const status = !source.requested
    ? ["本次分析未关联源码", "以下方案来自 SmartPerfetto 证据，不包含文件位置或 Diff。"]
    : source.context_state === "unavailable"
      ? ["源码上下文未能读取", "以下方案仍可直接执行，但本次不展示文件位置或 Diff。"]
      : source.match_summary !== "strong"
        ? ["源码匹配证据不足", "本次只展示基于 Trace 证据的修改方案，不展示文件位置或 Diff。"]
        : null;
  if (conclusions.length === 0) {
    return (
      <section className="analysis-report-section">
        <h2>{status?.[0] ?? "源码修改暂不可用"}</h2>
        {status?.[1] ? <p>{status[1]}</p> : null}
      </section>
    );
  }
  const renderAction = (
    conclusion: (typeof conclusions)[number],
    index: number,
  ) => (
    <article
      className="analysis-report-section source-action-group"
      data-testid="source-action-group"
      key={conclusion.finding_id}
    >
      <p className="section-label">源码修改 {index + 1}</p>
      <h2>{conclusion.problem}</h2>
      <div className="source-action-plan">
        <strong>修改方案</strong>
        <p>{conclusion.recommendation}</p>
      </div>
      {(fixesByFinding.get(conclusion.finding_id) ?? []).map((fix) => (
        <section className="source-fix-card" key={fix.fix_id}>
          <header>
            <div>
              <p className="section-label">{fix.rule_id}</p>
              <h3>{fix.relative_path}</h3>
              {fix.symbol ? <code>{fix.symbol}</code> : null}
            </div>
            <strong className={`source-verification-state is-${fix.verification.state}`}>
              {verificationCopy[fix.verification.state]}
            </strong>
          </header>
          <p>{fix.diagnosis}</p>
          <p>复测：{fix.retest_target}</p>
          {fix.verification.log_summary ? <p>{fix.verification.log_summary}</p> : null}
          <pre aria-label="建议代码 Diff">{fix.diff}</pre>
        </section>
      ))}
    </article>
  );
  return (
    <section className="source-fix-list" aria-label="源码修改动作">
      {status ? (
        <aside className="analysis-report-section source-action-status">
          <h2>{status[0]}</h2>
          <p>{status[1]}</p>
        </aside>
      ) : null}
      <p className="source-action-notice">
        修改建议仅供参考，请结合实际业务逻辑审查后再应用。
      </p>
      {conclusions.slice(0, 3).map(renderAction)}
      {conclusions.length > 3 ? (
        <details className="source-additional-actions">
          <summary>展开其余 {conclusions.length - 3} 项源码修改</summary>
          {conclusions.slice(3).map((conclusion, index) =>
            renderAction(conclusion, index + 3),
          )}
        </details>
      ) : null}
    </section>
  );
}
