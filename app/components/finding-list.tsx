"use client";

import { useState } from "react";

import type { FindingWorkbenchReport } from "../lib/perfpilot-api";
import { FindingDetail } from "./finding-detail";

export function FindingList({
  report,
  selectedFindingId,
  setSelectedFindingId,
}: {
  readonly report: FindingWorkbenchReport;
  readonly selectedFindingId: string | null;
  readonly setSelectedFindingId: (findingId: string) => void;
}) {
  const [priority, setPriority] = useState("all");
  const [evidenceGrade, setEvidenceGrade] = useState("all");
  const [status, setStatus] = useState("all");
  const visible = report.workbench.findings.filter((finding) =>
    (priority === "all" || finding.priority === priority) &&
    (evidenceGrade === "all" || finding.confidence.evidence_grade === evidenceGrade) &&
    (status === "all" || finding.status === status),
  );
  const selected = visible.find((finding) => finding.finding_id === selectedFindingId) ?? visible[0] ?? null;
  const conclusion = report.synthesis.state === "completed" && selected
    ? report.synthesis.output.conclusions.find((item) => item.finding_id === selected.finding_id) ?? null
    : null;

  return (
    <section className="finding-list-region" aria-labelledby="finding-list-title">
      <div className="finding-section-heading">
        <div>
          <p className="section-label">ALL FINDINGS</p>
          <h2 id="finding-list-title">问题清单</h2>
        </div>
        <span>{visible.length} / {report.workbench.findings.length}</span>
      </div>
      <div className="finding-filters" aria-label="问题筛选">
        <label>
          优先级
          <select value={priority} onChange={(event) => setPriority(event.target.value)}>
            <option value="all">全部</option>
            {(["p0", "p1", "p2", "p3"] as const).map((value) => <option key={value} value={value}>{value.toUpperCase()}</option>)}
          </select>
        </label>
        <label>
          证据等级
          <select value={evidenceGrade} onChange={(event) => setEvidenceGrade(event.target.value)}>
            <option value="all">全部</option>
            {(["E4", "E3", "E2", "E1", "E0"] as const).map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        <label>
          状态
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="all">全部</option>
            <option value="confirmed">已确认</option>
            <option value="hypothesis">待验证</option>
            <option value="resolved">已解决</option>
            <option value="improved">已改善</option>
            <option value="unchanged">无变化</option>
            <option value="regressed">发生回退</option>
            <option value="new">新问题</option>
          </select>
        </label>
      </div>
      <div className="finding-list-layout">
        <ol className="finding-list-items">
          {visible.map((finding) => (
            <li data-priority={finding.priority} data-testid="finding-list-item" key={finding.finding_id}>
              <button
                aria-current={selected?.finding_id === finding.finding_id ? "true" : undefined}
                onClick={() => setSelectedFindingId(finding.finding_id)}
                type="button"
              >
                <span className={`finding-priority is-${finding.priority}`}>{finding.priority.toUpperCase()}</span>
                <strong>{finding.title}</strong>
                <small>{finding.confidence.evidence_grade} · {finding.status}</small>
              </button>
            </li>
          ))}
        </ol>
        {selected && conclusion ? (
          <FindingDetail
            capabilities={report.capabilities}
            conclusion={conclusion}
            criticalPathAvailable={report.workbench.critical_path.length > 0}
            finding={selected}
          />
        ) : (
          <p className="finding-empty">当前筛选条件下没有问题。</p>
        )}
      </div>
    </section>
  );
}
