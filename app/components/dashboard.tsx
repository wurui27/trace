"use client";

import Link from "next/link";
import { ArrowUpRight } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { projectDashboardReport } from "../lib/dashboard-report";
import {
  createPerfPilotClient,
  PerfPilotApiError,
  type AnalysisResponse,
  type AnalysisState,
  type PerfPilotClient,
  type SubmittedTraceAnalysis,
} from "../lib/perfpilot-api";
import { ActiveAnalysisTaskCard } from "./active-analysis-task-card";
import { NewAnalysisDialog } from "./new-analysis-dialog";
import {
  createLatestReportLoader,
  LatestAnalysisReportEntry,
  type LatestReportLoader,
  type LatestReportSnapshot,
} from "./latest-analysis-report-entry";
import { useOptionalPerfPilotSession } from "./perfpilot-session-provider";

const activeStates = new Set<AnalysisState>([
  "creating",
  "created",
  "uploading",
  "queued",
  "scheduled",
  "running",
  "analyzing",
]);

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const cancel = () => {
      window.clearTimeout(timer);
      reject(signal.reason);
    };
    const finish = () => {
      signal.removeEventListener("abort", cancel);
      resolve();
    };
    const timer = window.setTimeout(finish, milliseconds);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

interface DashboardProps {
  readonly client?: PerfPilotClient;
  readonly pollDelay?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
  readonly latestReportLoader?: LatestReportLoader;
  readonly confirmCancel?: () => boolean;
}

const defaultClient = createPerfPilotClient();

const emptySecondaryMetrics = [
  { id: "smoothness", label: "页面流畅度" },
  { id: "main-thread", label: "主线程响应" },
  { id: "memory", label: "内存稳定性" },
  { id: "cpu", label: "CPU 与调度" },
] as const;

const severityLabels = {
  critical: "严重",
  warning: "需关注",
  healthy: "正常",
  informational: "信息",
} as const;

const confidenceLabels = {
  high: "高可信",
  medium: "中可信",
  low: "低可信",
  none: "未评级",
} as const;

export function Dashboard({
  client: providedClient,
  pollDelay = wait,
  latestReportLoader,
  confirmCancel = () =>
    window.confirm("确定取消当前分析吗？这会停止 SmartPerfetto 和后续 AI 分析。"),
}: DashboardProps = {}) {
  const session = useOptionalPerfPilotSession();
  const client = providedClient ?? session?.client ?? defaultClient;
  const [teamId, setTeamId] = useState<string | null>(null);
  const [currentAnalysis, setCurrentAnalysis] = useState<AnalysisResponse | null>(null);
  const [stale, setStale] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [reportRefreshToken, setReportRefreshToken] = useState(0);
  const [latestSnapshot, setLatestSnapshot] = useState<LatestReportSnapshot | null>(null);
  const cancelController = useRef<AbortController | null>(null);
  const activeAnalysisId = useMemo(
    () =>
      currentAnalysis && activeStates.has(currentAnalysis.state)
        ? currentAnalysis.analysis_id
        : null,
    [currentAnalysis],
  );
  const reportProjection = useMemo(
    () => (latestSnapshot ? projectDashboardReport(latestSnapshot) : null),
    [latestSnapshot],
  );
  const resolvedLatestReportLoader = useMemo(
    () => latestReportLoader ?? createLatestReportLoader(client),
    [client, latestReportLoader],
  );

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      await client.csrf(controller.signal);
      const me = await client.me(controller.signal);
      const resolvedTeamId = me.memberships[0]?.team.id;
      if (!resolvedTeamId) return;
      const active = await client.activeAnalyses(resolvedTeamId, 1, controller.signal);
      if (controller.signal.aborted) return;
      setTeamId(resolvedTeamId);
      setCurrentAnalysis(active.analyses[0] ?? null);
      setStale(false);
    })().catch(() => {
      if (!controller.signal.aborted) setStale(true);
    });
    return () => controller.abort();
  }, [client]);

  useEffect(() => {
    if (teamId === null || activeAnalysisId === null) return;
    const controller = new AbortController();
    void (async () => {
      let delay = 2_000;
      while (!controller.signal.aborted) {
        await pollDelay(delay, controller.signal);
        try {
          const next = await client.analysis(
            teamId,
            activeAnalysisId,
            controller.signal,
          );
          if (controller.signal.aborted) return;
          setCurrentAnalysis(next);
          setStale(false);
          delay = 2_000;
          if (!activeStates.has(next.state)) {
            if (next.report_available) {
              setReportRefreshToken((value) => value + 1);
            }
            return;
          }
        } catch (error) {
          if (controller.signal.aborted) return;
          if (!(error instanceof PerfPilotApiError) || !error.retryable) {
            setStale(true);
            return;
          }
          setStale(true);
          delay = Math.min(delay * 2, 15_000);
        }
      }
    })().catch(() => {
      if (!controller.signal.aborted) setStale(true);
    });
    return () => controller.abort();
  }, [activeAnalysisId, client, pollDelay, teamId]);

  useEffect(
    () => () => {
      cancelController.current?.abort();
    },
    [],
  );

  const handleSubmitted = useCallback((result: SubmittedTraceAnalysis) => {
    setTeamId(result.teamId);
    setCurrentAnalysis(result.analysis);
    setStale(false);
    setCancelError(null);
  }, []);

  const handleLatestSnapshot = useCallback(
    (snapshot: LatestReportSnapshot | null) => setLatestSnapshot(snapshot),
    [],
  );

  const handleCancel = useCallback(() => {
    if (
      teamId === null ||
      currentAnalysis === null ||
      !activeStates.has(currentAnalysis.state) ||
      !confirmCancel()
    ) {
      return;
    }
    const controller = new AbortController();
    cancelController.current?.abort();
    cancelController.current = controller;
    setCanceling(true);
    setCancelError(null);
    void client
      .cancelAnalysis(teamId, currentAnalysis.analysis_id, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setCurrentAnalysis(next);
        if (next.report_available) setReportRefreshToken((value) => value + 1);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setCancelError("取消请求未被服务端接受，请稍后重试。");
        }
      })
      .finally(() => {
        if (cancelController.current === controller) {
          cancelController.current = null;
          setCanceling(false);
        }
      });
  }, [client, confirmCancel, currentAnalysis, teamId]);

  return (
    <div className="dashboard">
      <header className="page-header">
        <div className="page-header-copy">
          <p className="page-eyebrow">PerfPilot</p>
          <h1>性能总览</h1>
          <p className="page-subtitle">
            新建分析后，这里只展示真实采集和分析产生的结果。
          </p>
        </div>
        <NewAnalysisDialog
          disabled={activeAnalysisId !== null}
          onSubmitted={handleSubmitted}
        />
      </header>

      {currentAnalysis ? (
        <ActiveAnalysisTaskCard
          analysis={currentAnalysis}
          canceling={canceling}
          stale={stale}
          cancelError={cancelError}
          onCancel={handleCancel}
        />
      ) : null}

      <LatestAnalysisReportEntry
        loader={resolvedLatestReportLoader}
        refreshToken={reportRefreshToken}
        onSnapshot={handleLatestSnapshot}
      />

      <section
        className={`conclusion-hero${reportProjection ? "" : " conclusion-hero-empty"}`}
        aria-labelledby="conclusion-title"
      >
        <div className="conclusion-heading">
          <p className="section-label">
            {reportProjection?.conclusion.source === "ai"
              ? "PERFPILOT AI 结论"
              : reportProjection
                ? "SMARTPERFETTO 结论"
                : "本次结论"}
          </p>
          <h2 id="conclusion-title">
            {reportProjection?.conclusion.title ?? "等待首次分析"}
          </h2>
        </div>
        <div className="conclusion-empty-copy">
          {reportProjection ? (
            <p>{reportProjection.conclusion.summary}</p>
          ) : (
            <>
              <strong>暂无分析结论</strong>
              <p>完成一次 Trace 分析后，这里会显示最需要关注的问题。</p>
            </>
          )}
        </div>
        {reportProjection ? (
          <Link
            className="all-problems-link"
            href={reportProjection.conclusion.href}
            prefetch={false}
          >
            打开完整报告
            <ArrowUpRight aria-hidden="true" />
          </Link>
        ) : (
          <span className="all-problems-link is-disabled" aria-disabled="true">
            暂无问题
          </span>
        )}
      </section>

      <section className="core-overview" aria-labelledby="core-overview-title">
        <header className="section-heading">
          <h2 id="core-overview-title">核心表现</h2>
        </header>

        <div className={`core-overview-panel${reportProjection ? "" : " is-empty"}`}>
          <article className="startup-overview">
            <header className="metric-heading">
              <div>
                <p className="metric-category">主要指标</p>
                <h3>启动体验</h3>
              </div>
              <span
                className={`metric-state metric-state-${reportProjection?.startup.state ?? "missing"}`}
              >
                {reportProjection?.startup.state === "measured" ? "已采集" : "未分析"}
              </span>
            </header>

            <div className="startup-result">
              <p
                className={`startup-value${reportProjection?.startup.state === "measured" ? "" : " empty-metric-value"}`}
              >
                {reportProjection?.startup.value ?? "—"}
              </p>
              <p className="startup-target">
                阈值 <strong>{reportProjection?.startup.target ?? "—"}</strong>
              </p>
            </div>
            <p className="metric-context">
              {reportProjection?.startup.context ?? "暂无启动数据"}
            </p>

            <dl className="startup-breakdown">
              {(reportProjection?.startup.breakdown ?? [
                { label: "TTID", value: "—" },
                { label: "TTFD", value: "—" },
                { label: "样本数", value: "—" },
              ]).map((item) => (
                <div className="startup-breakdown-item" key={item.label}>
                  <dt>{item.label}</dt>
                  <dd>{item.value}</dd>
                </div>
              ))}
            </dl>
          </article>

          <div className="secondary-metrics" aria-label="其他核心指标">
            {(reportProjection?.secondaryMetrics ??
              emptySecondaryMetrics.map((metric) => ({
                ...metric,
                state: "missing" as const,
                value: "—",
                context: "暂无数据",
              }))).map((metric) => (
              <article className="secondary-metric" key={metric.id}>
                <header className="secondary-metric-heading">
                  <h3>{metric.label}</h3>
                  <span className={`metric-state metric-state-${metric.state}`}>
                    {metric.state === "measured" ? "已采集" : "未采集"}
                  </span>
                </header>
                <p
                  className={`secondary-metric-value${metric.state === "measured" ? "" : " empty-metric-value"}`}
                >
                  {metric.value}
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
          {reportProjection && reportProjection.problems.length > 0 ? (
            reportProjection.problems.map((problem) => (
              <article className="focus-card" key={problem.id}>
                <div className="focus-card-meta">
                  <span
                    className={`priority-label priority-${
                      problem.severity === "critical"
                        ? "critical"
                        : problem.severity === "healthy"
                          ? "healthy"
                          : "warning"
                    }`}
                  >
                    {severityLabels[problem.severity]}
                  </span>
                  <span className="problem-status">
                    {confidenceLabels[problem.confidence]}
                  </span>
                </div>
                <h3>{problem.title}</h3>
                <p className="focus-card-summary">{problem.summary}</p>
                <footer className="focus-card-footer">
                  <span className="confidence">SmartPerfetto 证据</span>
                  <Link className="focus-card-link" href={problem.href} prefetch={false}>
                    查看证据
                    <ArrowUpRight aria-hidden="true" />
                  </Link>
                </footer>
              </article>
            ))
          ) : (
            <article className="focus-card focus-card-empty">
              <span className="problem-status">
                {reportProjection ? "分析完成" : "等待分析"}
              </span>
              <h3>
                {reportProjection ? "本次未发现明确问题" : "暂无重点问题"}
              </h3>
              <p className="focus-card-summary">
                {reportProjection
                  ? "内核没有返回可展示的性能问题，可打开完整报告检查证据边界。"
                  : "分析完成后，优先级最高的问题和优化方向会显示在这里。"}
              </p>
            </article>
          )}
        </div>
      </section>

      <section
        className={`data-credibility${reportProjection ? "" : " is-empty"}`}
        aria-labelledby="credibility-title"
      >
        <div className="credibility-copy">
          <h2 id="credibility-title">数据可信度</h2>
          <p>
            {reportProjection
              ? "以下信息直接来自本次报告与内核处理记录。"
              : "完成首次采集后生成可信度信息。"}
          </p>
        </div>
        <ul className="credibility-facts">
          {reportProjection ? (
            <>
              <li>有效采集 {reportProjection.credibility.sampleCount} 次</li>
              <li>可用指标 {reportProjection.credibility.availableMetrics} 项</li>
              <li>可验证证据 {reportProjection.credibility.evidenceCount} 条</li>
              <li>
                来源核验 {reportProjection.credibility.sourceVerification === "passed"
                  ? "已通过"
                  : reportProjection.credibility.sourceVerification === "failed"
                    ? "未通过"
                    : "待确认"}
              </li>
              <li>失败阶段 {reportProjection.credibility.failedStages} 个</li>
              <li>
                AI 最终报告 {reportProjection.credibility.aiState === "completed"
                  ? "已完成"
                  : reportProjection.credibility.aiState === "not_requested"
                    ? "当前报告未包含"
                    : "未完成"}
              </li>
            </>
          ) : (
            <>
              <li>有效采集 —</li>
              <li>可用指标 —</li>
              <li>可验证证据 —</li>
              <li>AI 最终报告 —</li>
            </>
          )}
        </ul>
      </section>
    </div>
  );
}
