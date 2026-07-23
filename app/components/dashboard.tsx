import Link from "next/link";
import { ArrowRight } from "lucide-react";

import type {
  DashboardData,
  MetricState,
  Severity,
} from "../lib/performance-data";
import { NewAnalysisDialog } from "./new-analysis-dialog";

interface DashboardProps {
  readonly data: DashboardData;
}

const metricStateLabels: Record<MetricState, string> = {
  measured: "已测得",
  missing: "未采集",
  failed: "查询失败",
};

const priorityLabels: Record<Severity, string> = {
  critical: "高优先级",
  warning: "中优先级",
  healthy: "已通过",
};

export function Dashboard({ data }: DashboardProps) {
  const focusProblems = data.problems.slice(0, 3);
  const conclusionTitle = `发现 ${focusProblems.length} 个需要关注的问题`;

  return (
    <div className="dashboard">
      <header className="page-header">
        <div className="page-header-copy">
          <p className="page-eyebrow">
            {data.app.name} · {data.app.version}
          </p>
          <h1>性能总览</h1>
          <p className="page-subtitle">
            快速了解当前版本的关键性能、用户影响与复现质量。
          </p>
        </div>
        <NewAnalysisDialog />
      </header>

      <section className="conclusion-hero" aria-labelledby="conclusion-title">
        <div className="conclusion-heading">
          <p className="section-label">本次结论</p>
          <h2 id="conclusion-title">{conclusionTitle}</h2>
        </div>
        <ul className="conclusion-problem-list">
          {focusProblems.map((problem) => (
            <li key={problem.id}>
              <Link
                className="conclusion-problem-link"
                href={`/problems/${problem.id}`}
              >
                <span className="conclusion-problem-copy">
                  <strong>{problem.title}</strong>
                  <span>{problem.impactLabel}</span>
                </span>
                <ArrowRight aria-hidden="true" />
              </Link>
            </li>
          ))}
        </ul>
        <Link className="all-problems-link" href="/problems">
          查看问题
          <ArrowRight aria-hidden="true" />
        </Link>
      </section>

      <section className="core-overview" aria-labelledby="core-overview-title">
        <header className="section-heading">
          <h2 id="core-overview-title">核心表现</h2>
        </header>

        <div className="core-overview-panel">
          <article className="startup-overview">
            <header className="metric-heading">
              <div>
                <p className="metric-category">主要指标</p>
                <h3>启动体验</h3>
              </div>
              <span
                className={`metric-state metric-state-${data.startup.state}`}
              >
                {metricStateLabels[data.startup.state]}
              </span>
            </header>

            <div className="startup-result">
              <p className="startup-value">{data.startup.value}</p>
              <p className="startup-target">
                目标 <strong>{data.startup.target}</strong>
              </p>
            </div>
            <p className="metric-context">{data.startup.context}</p>

            <dl className="startup-breakdown">
              <div className="startup-breakdown-item">
                <dt>冷启动</dt>
                <dd>{data.startup.cold}</dd>
              </div>
              <div className="startup-breakdown-item">
                <dt>温启动</dt>
                <dd>{data.startup.warm}</dd>
              </div>
              <div className="startup-breakdown-item">
                <dt>热启动</dt>
                <dd>{data.startup.hot}</dd>
              </div>
            </dl>
          </article>

          <div className="secondary-metrics" aria-label="其他核心指标">
            {data.secondaryMetrics.map((metric) => (
              <article className="secondary-metric" key={metric.id}>
                <header className="secondary-metric-heading">
                  <h3>{metric.label}</h3>
                  <span
                    className={`metric-state metric-state-${metric.state}`}
                  >
                    {metricStateLabels[metric.state]}
                  </span>
                </header>
                <p className="secondary-metric-value">
                  <span>{metric.value}</span>{" "}
                  <span className="secondary-metric-unit">{metric.unit}</span>
                </p>
                <p className="metric-context">{metric.context}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="focus-section" aria-labelledby="focus-title">
        <header className="section-heading">
          <h2 id="focus-title">本次重点</h2>
        </header>
        <div className="focus-card-grid">
          {focusProblems.map((problem) => (
            <article className="focus-card" key={problem.id}>
              <div className="focus-card-meta">
                <span
                  className={`priority-label priority-${problem.severity}`}
                >
                  {priorityLabels[problem.severity]}
                </span>
                <span className="problem-status">{problem.status}</span>
              </div>
              <h3>{problem.title}</h3>
              <p className="focus-card-summary">{problem.summary}</p>
              <div className="focus-card-footer">
                <span className="confidence">
                  可信度 {problem.confidence}%
                </span>
                <Link
                  className="focus-card-link"
                  href={`/problems/${problem.id}`}
                  aria-label={`查看${problem.title}详情`}
                >
                  查看详情
                  <ArrowRight aria-hidden="true" />
                </Link>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section
        className="data-credibility"
        aria-labelledby="credibility-title"
      >
        <div className="credibility-copy">
          <h2 id="credibility-title">数据可信度</h2>
          <p>结论可在相同设备、构建和场景下复现。</p>
        </div>
        <ul className="credibility-facts">
          <li>{data.credibility.runs} 轮有效采集</li>
          <li>{data.credibility.deviceConsistency}</li>
          <li>{data.credibility.thermalState}</li>
          <li>{data.credibility.failures} 次采样失败</li>
        </ul>
      </section>
    </div>
  );
}
