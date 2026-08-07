"use client";

import Link from "next/link";
import { ArrowUpRight, FileCheck2 } from "lucide-react";
import { memo, useEffect, useState } from "react";

import { aiCompletionBadge } from "../lib/analysis-ai-status";
import {
  createPerfPilotClient,
  type AnalysisListItem,
  type AnalysisReport,
  type PerfPilotClient,
  type TraceProfile,
} from "../lib/perfpilot-api";

export interface LatestReportSnapshot {
  readonly teamId: string;
  readonly analysis: AnalysisListItem;
  readonly report: AnalysisReport;
}

export type LatestReportLoader = (
  signal: AbortSignal,
) => Promise<LatestReportSnapshot | null>;

export function createLatestReportLoader(
  client: PerfPilotClient = createPerfPilotClient(),
): LatestReportLoader {
  return async (signal) => {
    await client.csrf(signal);
    const me = await client.me(signal);
    const teamId = me.memberships[0]?.team.id;
    if (teamId === undefined) return null;
    const list = await client.analyses(teamId, 1, signal);
    const analysis = list.analyses[0];
    if (analysis === undefined) return null;
    const report = await client.report(teamId, analysis.analysis_id, signal);
    return { teamId, analysis, report };
  };
}

type LatestReportView =
  | { readonly state: "loading" }
  | { readonly state: "empty" }
  | { readonly state: "error" }
  | { readonly state: "ready"; readonly snapshot: LatestReportSnapshot };

const profileLabels: Record<TraceProfile, string> = {
  auto: "综合性能分析",
  startup: "启动性能分析",
  scroll: "流畅度分析",
};

const defaultLoader = createLatestReportLoader();

interface LatestAnalysisReportEntryProps {
  readonly loader?: LatestReportLoader;
  readonly refreshToken?: number;
  readonly onSnapshot?: (snapshot: LatestReportSnapshot | null) => void;
}

export const LatestAnalysisReportEntry = memo(function LatestAnalysisReportEntry({
  loader = defaultLoader,
  refreshToken = 0,
  onSnapshot,
}: LatestAnalysisReportEntryProps) {
  const [view, setView] = useState<LatestReportView>({ state: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    setView({ state: "loading" });
    loader(controller.signal)
      .then((snapshot) => {
        if (!controller.signal.aborted) {
          setView(snapshot === null ? { state: "empty" } : { state: "ready", snapshot });
          onSnapshot?.(snapshot);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setView({ state: "error" });
      });
    return () => controller.abort();
  }, [loader, onSnapshot, refreshToken]);

  if (view.state === "loading") {
    return (
      <section
        className="latest-report-entry is-loading"
        role="status"
        aria-label="正在读取最新报告"
      >
        <span className="latest-report-icon" aria-hidden="true">
          <FileCheck2 />
        </span>
        <div className="latest-report-copy">
          <p className="latest-report-label">最新分析报告</p>
          <strong>正在读取最新报告</strong>
          <div className="latest-report-skeleton" aria-hidden="true">
            <span />
            <span />
          </div>
        </div>
        <span className="latest-report-action-skeleton" aria-hidden="true" />
      </section>
    );
  }

  if (view.state === "empty") {
    return (
      <section className="latest-report-entry is-empty" aria-labelledby="latest-report-title">
        <span className="latest-report-icon" aria-hidden="true">
          <FileCheck2 />
        </span>
        <div className="latest-report-copy">
          <p className="latest-report-label">分析数据</p>
          <h2 id="latest-report-title">还没有分析数据</h2>
          <p>新建一次分析后，真实的性能结论和最终报告会显示在这里。</p>
        </div>
        <span className="latest-report-empty-hint">使用右上角“新建分析”开始</span>
      </section>
    );
  }

  if (view.state === "error") {
    return (
      <section className="latest-report-entry is-error" role="alert">
        <span className="latest-report-icon" aria-hidden="true">
          <FileCheck2 />
        </span>
        <div className="latest-report-copy">
          <p className="latest-report-label">最新分析报告</p>
          <h2>暂时无法读取最新报告</h2>
          <p>请稍后刷新页面，已有报告不会被替换为演示数据。</p>
        </div>
      </section>
    );
  }

  const { analysis, report } = view.snapshot;
  const profileLabel =
    analysis.analysis_mode === "device"
      ? "真机综合性能分析"
      : profileLabels[analysis.analysis_profile ?? "auto"];
  const title =
    analysis.question?.trim() ||
    analysis.application_metadata?.package_name ||
    profileLabel;
  const summary =
    report.synthesis.state === "completed"
      ? report.synthesis.output.executive_summary
      : report.synthesis.state === "not_requested"
        ? "三类真机性能证据已经归档，可打开报告查看完整内核结论。"
        : "报告已保留可验证证据，AI 总结暂未完成。";
  const smartPerfettoLabel =
    analysis.source_analysis?.rounds === null || analysis.source_analysis?.rounds === undefined
      ? "SmartPerfetto 已完成"
      : `SmartPerfetto ${analysis.source_analysis.rounds} 轮`;
  const aiLabel =
    report.synthesis.state === "not_requested"
      ? "当前报告未包含 AI"
      : report.synthesis.state === "failed"
      ? "PerfPilot AI 未完成"
      : aiCompletionBadge(analysis.ai_rounds);
  const reportState = report.state === "completed" ? "完整报告" : "部分结论";

  return (
    <section className="latest-report-entry is-ready" aria-labelledby="latest-report-title">
      <span className="latest-report-icon" aria-hidden="true">
        <FileCheck2 />
      </span>
      <div className="latest-report-copy">
        <div className="latest-report-heading">
          <p className="latest-report-label">最新分析报告</p>
          <span className="latest-report-state">{reportState}</span>
        </div>
        <h2 id="latest-report-title">{title}</h2>
        <p className="latest-report-summary">{summary}</p>
        <div className="latest-report-meta" aria-label="报告处理信息">
          <span>{profileLabel}</span>
          <span>{smartPerfettoLabel}</span>
          <span>{aiLabel}</span>
          <span>报告 v{report.report_version}</span>
        </div>
      </div>
      <Link
        className="latest-report-link"
        href={`/analyses/${analysis.analysis_id}/report`}
        prefetch={false}
      >
        打开报告
        <ArrowUpRight aria-hidden="true" />
      </Link>
    </section>
  );
});
