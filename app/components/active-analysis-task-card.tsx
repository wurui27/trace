"use client";

import Link from "next/link";
import { ArrowUpRight, CircleStop, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";

import type {
  AnalysisResponse,
  AnalysisStage,
  AnalysisState,
} from "../lib/perfpilot-api";

const activeStates = new Set<AnalysisState>([
  "creating",
  "created",
  "uploading",
  "queued",
  "scheduled",
  "running",
  "analyzing",
]);

const stageNames: Record<AnalysisStage["stage"], string> = {
  input_validation: "输入校验",
  smartperfetto: "SmartPerfetto",
  perfpilot_ai: "PerfPilot AI",
  report: "最终报告",
};

const stageStates: Record<AnalysisStage["state"], string> = {
  pending: "等待中",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  canceled: "已取消",
  not_requested: "未执行",
};

export function activeAnalysisStageLabel(analysis: AnalysisResponse): string {
  if (analysis.state === "completed") return "分析已完成";
  if (analysis.state === "partially_completed") return "分析完成，部分证据不足";
  if (analysis.state === "failed") return "分析未能完成";
  if (analysis.state === "canceled") return "分析已取消";
  if (analysis.state === "deleted") return "分析已删除";
  if (analysis.cancel_requested_at) return "正在取消分析";
  if (["creating", "created", "uploading"].includes(analysis.state)) {
    return "正在准备分析输入";
  }
  const smartPerfetto = analysis.stages.find(
    (stage) => stage.stage === "smartperfetto",
  );
  if (smartPerfetto?.state === "running") return "SmartPerfetto 正在解析 Trace";
  const aiStage = analysis.stages.find((stage) => stage.stage === "perfpilot_ai");
  if (aiStage?.state === "running") {
    const runningRound = analysis.ai_rounds?.find((round) => round.state === "running");
    return runningRound
      ? `PerfPilot AI 第 ${runningRound.round}/3 轮`
      : "PerfPilot AI 正在生成建议";
  }
  const report = analysis.stages.find((stage) => stage.stage === "report");
  if (report?.state === "running" || aiStage?.state === "completed") {
    return "正在生成最终报告";
  }
  return "等待分析资源";
}

function elapsedLabel(createdAt: string | undefined, now: number): string {
  const started = createdAt ? Date.parse(createdAt) : Number.NaN;
  if (Number.isNaN(started)) return "已用时不到 1 分钟";
  const minutes = Math.max(0, Math.floor((now - started) / 60_000));
  if (minutes < 1) return "已用时不到 1 分钟";
  if (minutes < 60) return `已用时 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder === 0
    ? `已用时 ${hours} 小时`
    : `已用时 ${hours} 小时 ${remainder} 分钟`;
}

interface ActiveAnalysisTaskCardProps {
  readonly analysis: AnalysisResponse;
  readonly now?: number;
  readonly canceling: boolean;
  readonly stale: boolean;
  readonly cancelError?: string | null;
  readonly onCancel: () => void;
}

export function ActiveAnalysisTaskCard({
  analysis,
  now,
  canceling,
  stale,
  cancelError = null,
  onCancel,
}: ActiveAnalysisTaskCardProps) {
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (now !== undefined) return;
    const timer = window.setInterval(() => setClock(Date.now()), 30_000);
    return () => window.clearInterval(timer);
  }, [now]);

  const isActive = activeStates.has(analysis.state);
  const title = isActive ? "正在分析" : activeAnalysisStageLabel(analysis);
  const terminalDescription: Partial<Record<AnalysisState, string>> = {
    completed: "最终报告已经生成",
    partially_completed: "可验证结果已生成，部分证据仍缺失",
    failed: "任务已停止，可查看详情定位失败阶段",
    canceled: "后端已确认任务停止",
    deleted: "该任务已不再可用",
  };
  const stage = canceling
    ? "正在取消分析"
    : isActive
      ? activeAnalysisStageLabel(analysis)
      : terminalDescription[analysis.state] ?? activeAnalysisStageLabel(analysis);
  const detailHref = analysis.report_available
    ? `/analyses/${analysis.analysis_id}/report`
    : `/analyses/${analysis.analysis_id}`;
  const detailLabel = analysis.report_available ? "查看最终报告" : "查看详情";

  return (
    <section
      className={`active-analysis-task is-${analysis.state}`}
      role="status"
      aria-live="polite"
    >
      <span className="active-analysis-task-icon" aria-hidden="true">
        {isActive ? <LoaderCircle /> : <CircleStop />}
      </span>
      <div className="active-analysis-task-copy">
        <div className="active-analysis-task-heading">
          <p className="latest-report-label">当前任务</p>
          <span>{elapsedLabel(analysis.created_at, now ?? clock)}</span>
        </div>
        <h2>{title}</h2>
        <p className="active-analysis-stage">{stage}</p>
        {isActive ? (
          <p className="active-analysis-estimate">
            通常需要 3–8 分钟，复杂 Trace 可能更久
          </p>
        ) : null}
        {stale ? (
          <p className="active-analysis-warning">连接中断，正在保留当前状态并重试。</p>
        ) : null}
        {cancelError ? <p className="active-analysis-warning">{cancelError}</p> : null}
        <details className="active-analysis-details">
          <summary>查看阶段详情</summary>
          <ol>
            {analysis.stages.map((item) => (
              <li key={item.stage} className={`is-${item.state}`}>
                <span>{stageNames[item.stage]}</span>
                <strong>{stageStates[item.state]}</strong>
              </li>
            ))}
          </ol>
        </details>
      </div>
      <div className="active-analysis-task-actions">
        <Link href={detailHref} className="active-analysis-detail-link">
          {detailLabel}
          <ArrowUpRight aria-hidden="true" />
        </Link>
        {isActive ? (
          <button type="button" onClick={onCancel} disabled={canceling}>
            {canceling ? "正在取消…" : "取消分析"}
          </button>
        ) : null}
      </div>
    </section>
  );
}
