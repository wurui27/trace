import type { FindingWorkbenchReport } from "../lib/perfpilot-api";

const scenarioLabels = {
  startup: "启动",
  scroll: "滑动",
  memory_cycle: "内存循环",
  other: "其他",
} as const;

export function RetestPlanPanel({ report }: { readonly report: FindingWorkbenchReport }) {
  const findingById = new Map(report.workbench.findings.map((finding) => [finding.finding_id, finding]));
  const metricById = new Map(report.workbench.metrics.map((metric) => [metric.metric_id, metric]));
  return (
    <section className="finding-retest-panel" aria-labelledby="finding-retest-title">
      <div className="finding-section-heading">
        <div>
          <p className="section-label">VERIFY THE CHANGE</p>
          <h2 id="finding-retest-title">复测计划</h2>
        </div>
        <span>{report.workbench.retest_plans.length} 项</span>
      </div>
      <p>只有环境指纹一致时，前后结果才可直接比较；环境变化会单独标记，不能误判为已解决。</p>
      <ol className="finding-retest-list">
        {report.workbench.retest_plans.map((plan) => {
          const finding = findingById.get(plan.finding_id);
          return (
            <li key={plan.retest_plan_id}>
              <header>
                <div>
                  <span>{scenarioLabels[plan.scenario_type]}</span>
                  <strong>{finding?.title ?? "关联问题"}</strong>
                </div>
                <code>{plan.duration_seconds} 秒</code>
              </header>
              <dl>
                <div><dt>包名</dt><dd>{plan.package_name}</dd></div>
                <div><dt>环境指纹</dt><dd><code>{plan.environment_fingerprint}</code></dd></div>
                <div>
                  <dt>验证指标</dt>
                  <dd>{plan.metric_ids.map((id) => metricById.get(id)?.name ?? id).join("、")}</dd>
                </div>
              </dl>
              <ul>
                {plan.pass_criteria.map((criterion) => <li key={criterion}>{criterion}</li>)}
              </ul>
              <p>{plan.notes}</p>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
