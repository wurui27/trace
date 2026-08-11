import type { SourceAwareAnalysisReport, SourceFix } from "../lib/perfpilot-api";

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
  if (!source.requested) {
    return <section className="analysis-report-section"><h2>本次分析未关联源码</h2></section>;
  }
  if (source.context_state === "unavailable") {
    return (
      <section className="analysis-report-section">
        <h2>源码上下文未能读取</h2>
        <p>性能建议仍然有效，但本次无法给出具体代码修改。</p>
      </section>
    );
  }
  if (source.match_summary !== "strong") {
    return (
      <section className="analysis-report-section">
        <h2>源码匹配证据不足</h2>
        <p>本次只提供基于 Trace 证据的优化建议，不展示文件位置或 Diff。</p>
      </section>
    );
  }
  if (source.fixes.length === 0) {
    return <section className="analysis-report-section"><h2>没有可安全生成的源码修复</h2></section>;
  }
  return (
    <section className="source-fix-list" aria-label="源码修复">
      {source.fixes.slice(0, 3).map((fix) => (
        <article className="analysis-report-section source-fix-card" key={fix.fix_id}>
          <header>
            <div>
              <p className="section-label">{fix.rule_id}</p>
              <h2>{fix.relative_path}</h2>
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
        </article>
      ))}
    </section>
  );
}
