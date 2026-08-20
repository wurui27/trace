"use client";

import { ArrowLeft, CheckCircle2, Download } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";

import { completedAiProcessCopy } from "../lib/analysis-ai-status";
import { createRandomUuid } from "../lib/perfpilot-api";
import { createPerfPilotClient, type PerfPilotClient } from "../lib/perfpilot-api";
import { printAnalysisReport, supportsReportPrint } from "../lib/report-print";
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
const defaultClient = createPerfPilotClient();

interface FullAnalysisReportProps {
  readonly analysisId: string;
  readonly loader?: AnalysisLoader;
  readonly rerunner?: SynthesisRerunner;
  readonly randomUUID?: () => string;
  readonly printer?: (analysisId: string) => boolean;
  readonly client?: PerfPilotClient;
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
  randomUUID = createRandomUuid,
  printer = printAnalysisReport,
  client = defaultClient,
}: FullAnalysisReportProps) {
  const [attempt, setAttempt] = useState(0);
  const [snapshot, setSnapshot] = useState<ReportSnapshot | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [printSupported, setPrintSupported] = useState<boolean | null>(null);
  const [preparingPrint, setPreparingPrint] = useState(false);
  const [preloadOriginal, setPreloadOriginal] = useState(false);
  const [originalPrintFallback, setOriginalPrintFallback] = useState(false);
  const retryController = useRef<AbortController | null>(null);
  const originalReady = useRef(false);
  const originalWaiter = useRef<((ready: boolean) => void) | null>(null);
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
      originalWaiter.current?.(false);
      originalWaiter.current = null;
    },
    [analysisId],
  );

  useEffect(() => {
    const supported = supportsReportPrint();
    let active = true;
    void Promise.resolve().then(() => {
      if (active) setPrintSupported(supported);
    });
    return () => {
      active = false;
    };
  }, []);

  const current = snapshot?.requestKey === requestKey ? snapshot : null;
  const backLink = (
    <Link className="final-report-back" href={`/analyses/${analysisId}`} prefetch={false}>
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
        <p>正在校验内核证据与 PerfPilot AI 结论。</p>
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
            : "内核分析或 PerfPilot AI 仍在处理，请返回进度页查看。"}
        </p>
      </main>
    );
  }

  const sourceRounds = analysis.source_analysis?.rounds;
  const aiProcess = completedAiProcessCopy(analysis.ai_rounds);
  const verification = analysis.source_analysis?.verification ?? "unknown";
  const memoryScenario = report.scenario_reports.find(
    (scenario) => scenario.scenario_type === "memory_cycle",
  );
  const hasMemoryScenario = memoryScenario !== undefined;
  const memoryComplete = memoryScenario?.result_state === "completed";
  const findingWorkbenchComplete = report.schema_version === "1.3";
  const smartPerfettoSummary =
    sourceRounds === null || sourceRounds === undefined
      ? "SmartPerfetto 已完成"
      : `SmartPerfetto ${sourceRounds} 轮`;

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

  const handleOriginalReady = (ready: boolean): void => {
    originalReady.current = ready;
    originalWaiter.current?.(ready);
    originalWaiter.current = null;
  };

  const printReport = async (): Promise<void> => {
    if (preparingPrint) return;
    setPreparingPrint(true);
    try {
      if (report.schema_version === "1.3" && report.smartperfetto_original) {
        originalReady.current = false;
        flushSync(() => {
          setOriginalPrintFallback(false);
          setPreloadOriginal(true);
        });
        const ready = await new Promise<boolean>((resolve) => {
          if (originalReady.current) {
            resolve(true);
            return;
          }
          const timeout = window.setTimeout(() => {
            originalWaiter.current = null;
            resolve(false);
          }, 3000);
          originalWaiter.current = (value) => {
            window.clearTimeout(timeout);
            resolve(value);
          };
        });
        if (!ready) flushSync(() => setOriginalPrintFallback(true));
      }
      printer(analysisId);
    } finally {
      setPreparingPrint(false);
    }
  };

  return (
    <div className="final-report-page">
      <header className="final-report-topbar">
        <Link
          className="analysis-page-brand"
          href="/"
          prefetch={false}
          aria-label="返回 PerfPilot 首页"
        >
          <span className="brand-mark" aria-hidden="true">
            <span className="brand-mark-bar brand-mark-bar-short" />
            <span className="brand-mark-bar brand-mark-bar-medium" />
            <span className="brand-mark-bar brand-mark-bar-tall" />
          </span>
          <span>PerfPilot</span>
        </Link>
        <div className="final-report-actions">
          {backLink}
          <button
            aria-describedby={printSupported === false ? "report-print-unavailable" : undefined}
            className="final-report-download"
            disabled={printSupported !== true || preparingPrint}
            onClick={() => void printReport()}
            type="button"
          >
            <Download aria-hidden="true" />
            下载 PDF
          </button>
          {printSupported === false ? (
            <p
              className="final-report-print-unavailable"
              id="report-print-unavailable"
              role="status"
            >
              当前浏览器不支持打印，请使用浏览器菜单保存报告。
            </p>
          ) : null}
        </div>
      </header>

      <main className="final-report-main">
        <section className="final-report-masthead" aria-labelledby="final-report-title">
          <div className="final-report-intro">
            <p className="section-label">ANDROID PERFORMANCE REPORT</p>
            <div className="final-report-title-row">
              <h1 id="final-report-title">最终性能报告</h1>
              <span className={findingWorkbenchComplete ? "is-completed" : `is-${report.state}`}>
                <CheckCircle2 aria-hidden="true" />
                {findingWorkbenchComplete
                  ? "分析完成"
                  : report.state === "completed"
                    ? "结论完整"
                    : "部分结论"}
              </span>
            </div>
            <p>
              {hasMemoryScenario
                ? "SmartPerfetto 提供 Trace 证据，Android Memory 提供内存采集事实，PerfPilot AI 负责复核、归纳并生成最终优化方案。"
                : "SmartPerfetto 提供可验证的 Trace 证据，PerfPilot AI 负责复核、归纳并生成最终优化方案。"}
            </p>
            <code>{analysis.analysis_id}</code>
          </div>

          <dl className="final-report-process" aria-label="报告生成过程">
            <div>
              <dt>内核分析</dt>
              <dd>
                {hasMemoryScenario
                  ? `${smartPerfettoSummary}，Android Memory ${memoryComplete ? "已汇聚" : "不完整"}`
                  : sourceRounds === null || sourceRounds === undefined
                    ? "SmartPerfetto 分析已完成"
                    : `${sourceRounds} 轮 SmartPerfetto 分析`}
              </dd>
              <span>
                {verification === "passed" && hasMemoryScenario
                  ? memoryComplete
                    ? "Trace 证据已校验，内存证据已归一化"
                    : "Trace 证据已校验，内存证据不完整"
                  : verification === "passed"
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
                  ? aiProcess.title
                  : report.synthesis.state === "not_requested"
                    ? "当前报告未包含 AI"
                    : "PerfPilot AI 未完成"}
              </dd>
              <span>
                {report.synthesis.state === "completed"
                  ? aiProcess.detail
                  : report.synthesis.state === "not_requested"
                    ? hasMemoryScenario
                      ? "双内核基础报告"
                      : "SmartPerfetto 基础报告"
                    : hasMemoryScenario
                      ? "双内核基础报告仍可查看"
                      : "SmartPerfetto 基础报告仍可查看"}
              </span>
            </div>
            <div>
              <dt>报告版本</dt>
              <dd>v{report.report_version}</dd>
              <span>
                {analysis.analysis_mode === "device"
                  ? `${report.scenario_reports.length} 个真机场景`
                  : analysis.analysis_profile === "auto"
                    ? "自动识别场景"
                    : analysis.analysis_profile}
              </span>
            </div>
          </dl>
        </section>

        <AnalysisReportView
          report={report}
          teamId={analysis.team_id}
          client={client}
          onOriginalReady={handleOriginalReady}
          onRetrySynthesis={retrySynthesis}
          originalPrintFallback={originalPrintFallback}
          preloadOriginal={preloadOriginal}
          retrying={retrying}
        />
      </main>
    </div>
  );
}
