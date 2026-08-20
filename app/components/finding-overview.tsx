"use client";

import { useState } from "react";

import type { FindingWorkbenchReport } from "../lib/perfpilot-api";
import { FindingDetail } from "./finding-detail";

const capabilityLabels = {
  trace: {
    available: "Trace 可用",
    unavailable: "Trace 不可用",
  },
  smartperfetto: {
    available: "SmartPerfetto 已完成",
    failed: "SmartPerfetto 失败",
  },
  source: {
    matched: "源码匹配",
    mismatch: "源码不匹配",
    unavailable: "源码不可用",
    not_requested: "未关联源码",
  },
  ai: {
    available: "AI 中文总结已完成",
    deterministic_fallback: "已生成稳定中文结论",
  },
} as const;

function segmentDuration(durationNs: number): string {
  const milliseconds = durationNs / 1_000_000;
  return `${Number.isInteger(milliseconds) ? milliseconds : milliseconds.toFixed(2)} ms`;
}

export function FindingOverview({ report }: { readonly report: FindingWorkbenchReport }) {
  const [expanded, setExpanded] = useState(false);
  const findingById = new Map(
    report.workbench.findings.map((finding) => [finding.finding_id, finding]),
  );
  const conclusionById = new Map(
    report.synthesis.state === "completed"
      ? report.synthesis.output.conclusions.map((conclusion) => [conclusion.finding_id, conclusion])
      : [],
  );
  const primary = report.workbench.primary_finding_ids.flatMap((id) => {
    const finding = findingById.get(id);
    const conclusion = conclusionById.get(id);
    return finding && conclusion ? [{ finding, conclusion }] : [];
  });
  const primaryIds = new Set(report.workbench.primary_finding_ids);
  const additional = report.workbench.findings.flatMap((finding) => {
    const conclusion = conclusionById.get(finding.finding_id);
    return !primaryIds.has(finding.finding_id) && conclusion ? [{ finding, conclusion }] : [];
  });

  return (
    <section className="finding-overview" aria-label="Finding 概览">
      <header className="finding-overview-hero">
        <div>
          <p className="section-label">PERFPILOT FINDING WORKBENCH</p>
          <h2>本次分析结论</h2>
          <p>{report.synthesis.state === "completed" ? report.synthesis.output.executive_summary : "已保留可验证的 Trace 证据。"}</p>
        </div>
        <span className="finding-status is-completed">分析完成</span>
      </header>

      <dl className="finding-capabilities" aria-label="本次分析能力">
        {(Object.keys(capabilityLabels) as Array<keyof typeof capabilityLabels>).map((key) => (
          <div key={key}>
            <dt>{key === "trace" ? "Trace" : key === "smartperfetto" ? "内核" : key === "source" ? "源码" : "总结"}</dt>
            <dd>{capabilityLabels[key][report.capabilities[key] as never]}</dd>
          </div>
        ))}
      </dl>

      {report.workbench.critical_path.length > 0 ? (
        <section className="finding-critical-path" aria-labelledby="critical-path-title">
          <div className="finding-section-heading">
            <div>
              <p className="section-label">TRACE CRITICAL PATH</p>
              <h3 id="critical-path-title">关键路径</h3>
            </div>
            <span>{report.workbench.critical_path.length} 段</span>
          </div>
          <ol>
            {report.workbench.critical_path.map((segment) => (
              <li key={segment.segment_id}>
                <span aria-hidden="true" />
                <div>
                  <strong>{segment.label}</strong>
                  <small>{segment.start_ns} – {segment.end_ns} ns</small>
                </div>
                <b>{segmentDuration(segment.duration_ns)}</b>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      <section className="finding-overview-primary" aria-labelledby="primary-findings-title">
        <div className="finding-section-heading">
          <div>
            <p className="section-label">PRIORITY FINDINGS</p>
            <h3 id="primary-findings-title">主要问题</h3>
          </div>
          <span>最多展示 3 项</span>
        </div>
        {primary.map(({ finding, conclusion }) => (
          <div data-testid="primary-finding" key={finding.finding_id}>
            <FindingDetail capabilities={report.capabilities} conclusion={conclusion} finding={finding} />
          </div>
        ))}
        {additional.length > 0 ? (
          <details className="finding-additional" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
            <summary>展开其余 {additional.length} 项</summary>
            {additional.map(({ finding, conclusion }) => (
              <div data-testid="additional-finding" key={finding.finding_id}>
                <FindingDetail capabilities={report.capabilities} conclusion={conclusion} finding={finding} />
              </div>
            ))}
          </details>
        ) : null}
      </section>
    </section>
  );
}
