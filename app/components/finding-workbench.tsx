"use client";

import { useState } from "react";

import type { FindingWorkbenchReport, PerfPilotClient } from "../lib/perfpilot-api";
import { FindingList } from "./finding-list";
import { FindingOverview } from "./finding-overview";
import { FindingSourcePanel } from "./finding-source-panel";
import { RetestPlanPanel } from "./retest-plan-panel";
import { SmartPerfettoOriginalReport } from "./smartperfetto-original-report";

const regions = [
  ["overview", "概览"],
  ["findings", "问题清单"],
  ["source", "源码与优化"],
  ["original", "SmartPerfetto 原始报告"],
  ["retest", "复测计划"],
] as const;

type Region = typeof regions[number][0];

export function FindingWorkbench({
  client,
  onOriginalReady,
  preloadOriginal = false,
  originalPrintFallback = false,
  report,
  teamId,
}: {
  readonly client: PerfPilotClient;
  readonly onOriginalReady?: (ready: boolean) => void;
  readonly preloadOriginal?: boolean;
  readonly originalPrintFallback?: boolean;
  readonly report: FindingWorkbenchReport;
  readonly teamId?: string;
}) {
  const [region, setRegion] = useState<Region>("overview");
  const [selectedFindingId, setSelectedFindingId] = useState(
    report.workbench.primary_finding_ids[0] ?? report.workbench.findings[0]?.finding_id ?? null,
  );

  return (
    <article className="finding-workbench" aria-label="PerfPilot Finding 工作台">
      <nav className="finding-workbench-nav" aria-label="Finding 工作台" role="tablist">
        <div className="finding-workbench-nav-title">
          <strong>Finding 工作台</strong>
          <span>{report.workbench.findings.length} 个问题</span>
        </div>
        {regions.map(([id, label]) => (
          <button
            aria-controls={`finding-region-${id}`}
            aria-selected={region === id}
            key={id}
            onClick={() => setRegion(id)}
            role="tab"
            type="button"
          >
            <span aria-hidden="true" />
            {label}
          </button>
        ))}
      </nav>
      <div className="finding-workbench-content">
        <div data-report-layer="overview" hidden={region !== "overview"} id="finding-region-overview" role="tabpanel">
          <FindingOverview report={report} />
        </div>
        <div data-report-layer="findings" hidden={region !== "findings"} id="finding-region-findings" role="tabpanel">
          <FindingList report={report} selectedFindingId={selectedFindingId} setSelectedFindingId={setSelectedFindingId} />
        </div>
        <div data-report-layer="source" hidden={region !== "source"} id="finding-region-source" role="tabpanel">
          <FindingSourcePanel report={report} />
        </div>
        <div data-report-layer="original" hidden={region !== "original"} id="finding-region-original" role="tabpanel">
          {report.smartperfetto_original ? (
            <SmartPerfettoOriginalReport
              active={region === "original" || preloadOriginal}
              analysisId={report.analysis_id}
              client={client}
              onReady={onOriginalReady}
              printFallback={originalPrintFallback}
              teamId={teamId}
            />
          ) : <p className="finding-empty">本次结果未提供 SmartPerfetto 原始 HTML。</p>}
        </div>
        <div data-report-layer="retest" hidden={region !== "retest"} id="finding-region-retest" role="tabpanel">
          <RetestPlanPanel report={report} />
        </div>
      </div>
    </article>
  );
}
