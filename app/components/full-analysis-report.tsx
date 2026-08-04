"use client";

import { ArrowLeft, CheckCircle2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import {
  createAnalysisLoader,
  createSynthesisRerunner,
  type AnalysisDetailSnapshot,
  type AnalysisLoader,
  type SynthesisRerunner,
} from "./analysis-progress";
import { AnalysisReportView } from "./analysis-report";

const defaultLoader = createAnalysisLoader();
const defaultRerunner = createSynthesisRerunner();

interface FullAnalysisReportProps {
  readonly analysisId: string;
  readonly loader?: AnalysisLoader;
  readonly rerunner?: SynthesisRerunner;
  readonly randomUUID?: () => string;
}

interface ReportSnapshot {
  readonly requestKey: string;
  readonly detail: AnalysisDetailSnapshot | null;
  readonly failed: boolean;
}

export function FullAnalysisReport({
  analysisId,
  loader = defaultLoader,
  rerunner = defaultRerunner,
  randomUUID = () => crypto.randomUUID(),
}: FullAnalysisReportProps) {
  const [attempt, setAttempt] = useState(0);
  const [snapshot, setSnapshot] = useState<ReportSnapshot | null>(null);
  const [retrying, setRetrying] = useState(false);
  const retryController = useRef<AbortController | null>(null);
  const requestKey = `${analysisId}:${attempt}`;

  useEffect(() => {
    const controller = new AbortController();
    void loader(analysisId, controller.signal, (detail) => {
      if (!controller.signal.aborted) {
        setSnapshot({ requestKey, detail, failed: false });
      }
    }).catch(() => {
      if (!controller.signal.aborted) {
        setSnapshot({ requestKey, detail: null, failed: true });
      }
    });
    return () => controller.abort();
  }, [analysisId, loader, requestKey]);

  useEffect(
    () => () => {
      retryController.current?.abort();
    },
    [analysisId],
  );

  const current = snapshot?.requestKey === requestKey ? snapshot : null;
  const backLink = (
    <Link className="final-report-back" href={`/analyses/${analysisId}`}>
      <ArrowLeft aria-hidden="true" />
      返回分析进度
    </Link>
  );

  if (current?.failed) {
    return (
      <main className="final-report-state" role="alert">
        {backLink}
        <h1>最终报告暂时无法读取</h1>
        <p>请确认本地 API 正在运行，然后重新加载。</p>
        <button type="button" onClick={() => setAttempt((value) => value + 1)}>
          重新加载
        </button>
      </main>
    );
  }

  if (current?.detail == null) {
    return (
      <main className="final-report-state" role="status" aria-live="polite">
        {backLink}
        <div className="final-report-skeleton" aria-hidden="true" />
        <h1>正在读取最终报告</h1>
        <p>正在校验 SmartPerfetto 证据与 PerfPilot AI 结论。</p>
      </main>
    );
  }

  const { analysis, report, reportLoadFailed, teamId } = current.detail;
  if (report === null) {
    return (
      <main className="final-report-state" role={reportLoadFailed ? "alert" : "status"}>
        {backLink}
        <h1>{reportLoadFailed ? "最终报告暂时无法读取" : "最终报告尚未生成"}</h1>
        <p>
          {reportLoadFailed
            ? "分析记录仍然可用，请稍后重新加载报告。"
            : "SmartPerfetto 或 PerfPilot AI 仍在处理，请返回进度页查看。"}
        </p>
      </main>
    );
  }

  const sourceRounds = analysis.source_analysis?.rounds;
  const completedAiRounds =
    analysis.ai_rounds?.filter((round) => round.state === "completed").length ??
    (report.synthesis.state === "completed" ? 3 : 0);
  const verification = analysis.source_analysis?.verification ?? "unknown";

  const retrySynthesis = async (): Promise<void> => {
    if (retrying) return;
    retryController.current?.abort();
    const controller = new AbortController();
    retryController.current = controller;
    setRetrying(true);
    try {
      await rerunner(teamId, analysisId, randomUUID(), controller.signal);
      if (!controller.signal.aborted) setAttempt((value) => value + 1);
    } finally {
      if (!controller.signal.aborted) setRetrying(false);
    }
  };

  return (
    <div className="final-report-page">
      <header className="final-report-topbar">
        <Link className="analysis-page-brand" href="/" aria-label="返回 PerfPilot 首页">
          <span className="brand-mark" aria-hidden="true">
            <span className="brand-mark-bar brand-mark-bar-short" />
            <span className="brand-mark-bar brand-mark-bar-medium" />
            <span className="brand-mark-bar brand-mark-bar-tall" />
          </span>
          <span>PerfPilot</span>
        </Link>
        {backLink}
      </header>

      <main className="final-report-main">
        <section className="final-report-masthead" aria-labelledby="final-report-title">
          <div className="final-report-intro">
            <p className="section-label">ANDROID PERFORMANCE REPORT</p>
            <div className="final-report-title-row">
              <h1 id="final-report-title">最终性能报告</h1>
              <span className={`is-${report.state}`}>
                <CheckCircle2 aria-hidden="true" />
                {report.state === "completed" ? "结论完整" : "部分结论"}
              </span>
            </div>
            <p>
              SmartPerfetto 提供可验证的 Trace 证据，PerfPilot AI 负责复核、归纳并生成最终优化方案。
            </p>
            <code>{analysis.analysis_id}</code>
          </div>

          <dl className="final-report-process" aria-label="报告生成过程">
            <div>
              <dt>内核分析</dt>
              <dd>
                {sourceRounds === null || sourceRounds === undefined
                  ? "SmartPerfetto 分析已完成"
                  : `${sourceRounds} 轮 SmartPerfetto 分析`}
              </dd>
              <span>
                {verification === "passed"
                  ? "证据校验通过"
                  : verification === "failed"
                    ? "证据校验未通过"
                    : "证据校验状态未知"}
              </span>
            </div>
            <div>
              <dt>AI 复核</dt>
              <dd>
                {report.synthesis.state === "completed"
                  ? `${completedAiRounds} 轮 PerfPilot AI 已完成`
                  : "PerfPilot AI 未完成"}
              </dd>
              <span>
                {report.synthesis.state === "completed"
                  ? "提取、复核、定稿"
                  : "SmartPerfetto 基础报告仍可查看"}
              </span>
            </div>
            <div>
              <dt>报告版本</dt>
              <dd>v{report.report_version}</dd>
              <span>{analysis.analysis_profile === "auto" ? "自动识别场景" : analysis.analysis_profile}</span>
            </div>
          </dl>
        </section>

        <AnalysisReportView
          report={report}
          onRetrySynthesis={retrySynthesis}
          retrying={retrying}
        />
      </main>
    </div>
  );
}
