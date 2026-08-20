import type {
  FindingSynthesisOutput,
  FindingWorkbenchFinding,
  ReportCapabilities,
} from "../lib/perfpilot-api";

const confidenceLabels = {
  data_completeness: "数据完整性",
  evidence_grade: "证据等级",
  attribution: "归因可信度",
  statistical: "统计可信度",
} as const;

export function FindingDetail({
  capabilities,
  conclusion,
  finding,
}: {
  readonly capabilities: ReportCapabilities;
  readonly conclusion: FindingSynthesisOutput["conclusions"][number];
  readonly finding: FindingWorkbenchFinding;
}) {
  return (
    <section className="finding-detail" aria-label={`${finding.title} 诊断`}>
      <header className="finding-detail-heading">
        <div>
          <span className={`finding-priority is-${finding.priority}`}>
            {finding.priority.toUpperCase()}
          </span>
          <span>{finding.scenario_type}</span>
        </div>
        <h3>{finding.title}</h3>
        <p>{finding.impact}</p>
      </header>
      <dl className="finding-diagnostic-chain">
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
          <dd>
            {capabilities.source === "matched"
              ? conclusion.source_root_cause
              : "本次没有经过验证的匹配源码，只能确认 Trace 机制，不能定位具体代码根因。"}
          </dd>
        </div>
        <div>
          <dt>4. 修改建议</dt>
          <dd>{conclusion.recommendation}</dd>
        </div>
      </dl>
      <dl aria-label="结论可信度" className="finding-confidence">
        {(Object.keys(confidenceLabels) as Array<keyof typeof confidenceLabels>).map((key) => (
          <div key={key}>
            <dt>{confidenceLabels[key]}</dt>
            <dd>{finding.confidence[key]}</dd>
          </div>
        ))}
      </dl>
      {finding.unconfirmed_items.length > 0 ? (
        <details className="finding-boundary">
          <summary>仍需确认的边界</summary>
          <ul>
            {finding.unconfirmed_items.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
