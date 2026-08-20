"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUpRight } from "lucide-react";
import Link from "next/link";

import { AnalysisReportView } from "./analysis-report";
import {
  analysisIsTerminal,
  createPerfPilotClient,
  createRandomUuid,
  PerfPilotApiError,
  type AnalysisReport,
  type AnalysisResponse,
  type AnalysisStage,
  type AnalysisState,
  type PerfPilotClient,
  type TraceInputKind,
} from "../lib/perfpilot-api";

const stateCopy: Record<
  AnalysisState,
  { readonly title: string; readonly description: string; readonly tone: string }
> = {
  creating: {
    title: "正在创建分析",
    description: "正在准备团队隔离的分析空间。",
    tone: "active",
  },
  created: {
    title: "等待上传 Trace",
    description: "任务已经创建，等待必需的 Trace 文件。",
    tone: "pending",
  },
  uploading: {
    title: "正在接收分析文件",
    description: "文件正在直传到团队隔离存储，完成后会自动开始分析。",
    tone: "active",
  },
  queued: {
    title: "等待分析资源",
    description: "输入已经就绪，正在等待可用分析资源。",
    tone: "pending",
  },
  scheduled: {
    title: "分析即将开始",
    description: "SmartPerfetto 执行环境已经分配。",
    tone: "active",
  },
  running: {
    title: "SmartPerfetto 正在分析",
    description: "正在解析 Trace 并定位关键性能路径。",
    tone: "active",
  },
  analyzing: {
    title: "SmartPerfetto 正在分析",
    description: "正在解析 Trace 并定位关键性能路径。",
    tone: "active",
  },
  completed: {
    title: "分析完成",
    description: "SmartPerfetto 证据与 PerfPilot 建议已经生成并安全归档。",
    tone: "success",
  },
  partially_completed: {
    title: "分析完成",
    description: "SmartPerfetto 证据与 PerfPilot 建议已经生成并安全归档。",
    tone: "success",
  },
  failed: {
    title: "分析未能完成",
    description: "执行已安全停止，可根据错误代码排查后重新提交。",
    tone: "danger",
  },
  canceled: {
    title: "分析已取消",
    description: "任务已停止，不会继续访问上传文件。",
    tone: "muted",
  },
  deleted: {
    title: "分析已删除",
    description: "该分析已不再可用。",
    tone: "muted",
  },
};

const inputLabels: Record<TraceInputKind, string> = {
  trace: "Trace",
  memory_evidence: "内存证据",
  apk: "APK",
  source_archive: "源码压缩包",
  mapping: "Mapping",
  native_symbols: "Native Symbols",
  log: "日志",
};

const inputStateLabels = {
  awaiting_upload: "等待上传",
  pending: "上传中",
  finalized: "已校验",
} as const;

function analysisStillChanging(analysis: AnalysisResponse): boolean {
  return !analysisIsTerminal(analysis);
}

const scenarioCopy = {
  cold_start: { label: "冷启动采集", description: "采集应用冷启动与首帧证据。" },
  scroll: { label: "滑动采集", description: "采集页面滑动与卡顿证据。" },
  memory_cycle: { label: "内存循环采集", description: "采集内存增长与回收证据。" },
} as const;

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const cancel = () => {
      clearTimeout(timeout);
      reject(signal.reason);
    };
    const finish = () => {
      signal.removeEventListener("abort", cancel);
      resolve();
    };
    const timeout = setTimeout(finish, milliseconds);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

export interface AnalysisDetailSnapshot {
  readonly teamId: string;
  readonly analysis: AnalysisResponse;
  readonly report: AnalysisReport | null;
  readonly reportLoadFailed: boolean;
}

export type AnalysisLoader = (
  analysisId: string,
  signal: AbortSignal,
  onSnapshot: (snapshot: AnalysisDetailSnapshot) => void,
) => Promise<void>;

export type SynthesisRerunner = (
  teamId: string,
  analysisId: string,
  idempotencyKey: string,
  signal: AbortSignal,
) => Promise<void>;

export function createAnalysisLoader(
  client: PerfPilotClient = createPerfPilotClient(),
  sleep: (milliseconds: number, signal: AbortSignal) => Promise<void> = wait,
): AnalysisLoader {
  return async (analysisId, signal, onSnapshot) => {
    await client.csrf(signal);
    const me = await client.me(signal);
    let teamId: string | null = null;
    let current: AnalysisResponse | null = null;
    let report: AnalysisReport | null = null;
    for (const membership of me.memberships) {
      try {
        current = await client.analysis(membership.team.id, analysisId, signal);
        teamId = membership.team.id;
        break;
      } catch (error) {
        if (!(error instanceof PerfPilotApiError) || error.code !== "resource_not_found") {
          throw error;
        }
      }
    }
    if (current === null || teamId === null) {
      throw new PerfPilotApiError("resource_not_found", "分析不存在", false, null);
    }

    const publish = async (): Promise<void> => {
      let reportLoadFailed = false;
      if (current?.report_available) {
        try {
          report = await client.report(teamId, analysisId, signal);
        } catch {
          if (signal.aborted) throw signal.reason;
          reportLoadFailed = true;
        }
      }
      onSnapshot({ teamId, analysis: current as AnalysisResponse, report, reportLoadFailed });
    };

    await publish();
    let delay = 2_000;
    while (analysisStillChanging(current)) {
      await sleep(delay, signal);
      try {
        current = await client.analysis(teamId, analysisId, signal);
        await publish();
        delay = 2_000;
      } catch (error) {
        if (!(error instanceof PerfPilotApiError) || !error.retryable) throw error;
        delay = Math.min(delay * 2, 15_000);
      }
    }
  };
}

export function createSynthesisRerunner(
  client: PerfPilotClient = createPerfPilotClient(),
): SynthesisRerunner {
  return async (teamId, analysisId, idempotencyKey, signal) => {
    await client.csrf(signal);
    await client.createSynthesisRun(teamId, analysisId, idempotencyKey, signal);
  };
}

const defaultClient = createPerfPilotClient();
const defaultLoader = createAnalysisLoader(defaultClient);
const defaultRerunner = createSynthesisRerunner(defaultClient);

interface AnalysisProgressProps {
  readonly analysisId: string;
  readonly loader?: AnalysisLoader;
  readonly rerunner?: SynthesisRerunner;
  readonly randomUUID?: () => string;
}

interface AnalysisSnapshot {
  readonly requestKey: string;
  readonly detail: AnalysisDetailSnapshot | null;
  readonly failed: boolean;
}

export function AnalysisProgress({
  analysisId,
  loader = defaultLoader,
  rerunner = defaultRerunner,
  randomUUID = createRandomUuid,
}: AnalysisProgressProps) {
  const [snapshot, setSnapshot] = useState<AnalysisSnapshot | null>(null);
  const [attempt, setAttempt] = useState(0);
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

  useEffect(() => {
    return () => {
      retryController.current?.abort();
      retryController.current = null;
    };
  }, [analysisId]);

  const current = snapshot?.requestKey === requestKey ? snapshot : null;

  if (current?.failed) {
    return (
      <section className="analysis-load-state analysis-load-error" role="alert">
        <h1>暂时无法读取分析状态</h1>
        <p>请检查登录状态或网络连接后重试。</p>
        <button type="button" onClick={() => setAttempt((value) => value + 1)}>
          重新加载
        </button>
      </section>
    );
  }
  if (current?.detail == null) {
    return (
      <section className="analysis-load-state" role="status" aria-live="polite">
        <span className="analysis-loading-dot" aria-hidden="true" />
        <h1>正在读取分析状态</h1>
        <p>正在连接团队数据与 SmartPerfetto 执行记录。</p>
      </section>
    );
  }
  const detail = current.detail;
  const retrySynthesis = async (): Promise<void> => {
    if (retrying) return;
    retryController.current?.abort();
    const controller = new AbortController();
    retryController.current = controller;
    setRetrying(true);
    try {
      await rerunner(
        detail.teamId,
        analysisId,
        randomUUID(),
        controller.signal,
      );
      if (!controller.signal.aborted) setAttempt((value) => value + 1);
    } finally {
      if (!controller.signal.aborted) setRetrying(false);
    }
  };
  return (
    <AnalysisProgressView
      analysis={detail.analysis}
      report={detail.report}
      reportLoadFailed={detail.reportLoadFailed}
      onRetrySynthesis={retrySynthesis}
      retrying={retrying}
    />
  );
}

const stageCopy: Record<
  AnalysisStage["stage"],
  { readonly label: string; readonly description: string }
> = {
  input_validation: { label: "文件校验", description: "确认大小、类型与 SHA-256。" },
  smartperfetto: { label: "SmartPerfetto", description: "分析 Trace 中的关键性能路径。" },
  perfpilot_ai: { label: "PerfPilot AI", description: "提炼结论并生成优化建议。" },
  report: { label: "报告完成", description: "归档可追溯的指标、证据与结论。" },
};

const runtimeStageCopy = {
  input_validation: "正在校验分析输入",
  device_claim: "正在等待设备",
  device_capture: "正在采集真机 Trace",
  smartperfetto: "SmartPerfetto 正在分析",
  source_code: "正在读取并匹配源码",
  perfpilot_ai: "正在生成中文分析结论",
  report: "正在生成分析报告",
} as const;

function stageClass(stage: AnalysisStage): string {
  if (stage.state === "completed") return "is-complete";
  if (stage.state === "running") return "is-current";
  if (stage.state === "failed") return "is-failed";
  if (stage.state === "canceled") return "is-canceled";
  if (stage.state === "not_requested") return "is-not-requested";
  return "";
}

interface AnalysisProgressViewProps {
  readonly analysis: AnalysisResponse;
  readonly report?: AnalysisReport | null;
  readonly reportLoadFailed?: boolean;
  readonly onRetrySynthesis?: () => void | Promise<void>;
  readonly retrying?: boolean;
}

export function AnalysisProgressView({
  analysis,
  report = null,
  reportLoadFailed = false,
  onRetrySynthesis = () => undefined,
  retrying = false,
}: AnalysisProgressViewProps) {
  const copy = stateCopy[analysis.state];
  const deviceMode = analysis.analysis_mode === "device";
  const runtimeStatus = analysis.schema_version === "1.3" ? analysis.runtime_status : undefined;
  const terminal = analysisIsTerminal(analysis);
  const title = runtimeStatus && !terminal
    ? runtimeStageCopy[runtimeStatus.current_stage]
    : copy.title;
  const description = runtimeStatus && !terminal
    ? "当前进度由服务端任务状态实时确认。"
    : copy.description;

  return (
    <div className="analysis-detail-stack">
      <article className="analysis-progress-card">
      <header className="analysis-progress-header">
        <div>
          <p className="page-eyebrow">{deviceMode ? "DEVICE ANALYSIS" : "TRACE ANALYSIS"}</p>
          <h1 className={`analysis-state-title is-${copy.tone}`}>{title}</h1>
          <p>{description}</p>
        </div>
        <span className={`analysis-state-badge is-${copy.tone}`}>{analysis.state}</span>
      </header>

      <dl className="analysis-identity">
        <div>
          <dt>分析编号</dt>
          <dd><code>{analysis.analysis_id}</code></dd>
        </div>
        <div>
          <dt>分析方式</dt>
          <dd>
            {deviceMode
              ? "真机性能测试"
              : analysis.analysis_profile === "auto"
                ? "自动识别"
                : analysis.analysis_profile === "startup"
                  ? "启动性能"
                  : "滑动与卡顿"}
          </dd>
        </div>
        <div>
          <dt>版本</dt>
          <dd>v{analysis.version}</dd>
        </div>
      </dl>

      <section className="analysis-stage-section" aria-labelledby="analysis-stage-title">
        <h2 id="analysis-stage-title">处理进度</h2>
        {runtimeStatus ? (
          <div className={`analysis-runtime-status is-${runtimeStatus.stage_state}`}>
            <strong>{runtimeStageCopy[runtimeStatus.current_stage]}</strong>
            <p>{runtimeStatus.progress_summary}</p>
            {runtimeStatus.stage_state === "slow" ? (
              <p className="analysis-runtime-warning is-warning">
                处理时间较长，任务仍在继续
              </p>
            ) : runtimeStatus.stage_state === "waiting_for_upstream" ? (
              <p className="analysis-runtime-warning is-warning">
                上游仍在处理，暂未收到新的进度
              </p>
            ) : null}
            <time dateTime={runtimeStatus.updated_at}>
              最近更新：{runtimeStatus.updated_at}
            </time>
          </div>
        ) : (
          <ol className="analysis-stage-list">
            {deviceMode
            ? analysis.scenarios?.map((scenario) => {
                const content = scenarioCopy[scenario.scenario_type];
                const className =
                  scenario.state === "completed"
                    ? "is-complete"
                    : ["running", "analyzing"].includes(scenario.state)
                      ? "is-current"
                      : scenario.state === "failed"
                        ? "is-failed"
                        : scenario.state === "canceled"
                          ? "is-canceled"
                          : "";
                return (
                  <li className={className} key={scenario.scenario_type}>
                    <span aria-hidden="true" />
                    <div>
                      <strong>{content.label}</strong>
                      <p>{scenario.failure?.message ?? content.description}</p>
                    </div>
                  </li>
                );
              })
            : analysis.stages.map((stage) => {
                const content = stageCopy[stage.stage];
                return (
                  <li className={stageClass(stage)} key={stage.stage}>
                    <span aria-hidden="true" />
                    <div>
                      <strong>{content.label}</strong>
                      <p>{stage.failure?.message ?? content.description}</p>
                    </div>
                  </li>
                );
              })}
          </ol>
        )}
      </section>

      <section className="analysis-input-section" aria-labelledby="analysis-input-title">
        <div className="analysis-section-heading">
          <h2 id="analysis-input-title">输入文件</h2>
          <span>{deviceMode ? (analysis.apk_upload ? 1 : 0) : analysis.input_uploads.length} 项</span>
        </div>
        <ul className="analysis-input-list">
          {deviceMode && analysis.apk_upload ? (
            <li>
              <span
                className={`analysis-input-state is-${analysis.apk_upload.state}`}
                aria-hidden="true"
              />
              <div>
                <strong>APK</strong>
                <span>{analysis.apk_upload.mime} · {formatBytes(analysis.apk_upload.size)}</span>
              </div>
              <span>
                {analysis.apk_upload.state === "finalized" ? "已校验" : "上传中"}
              </span>
            </li>
          ) : (
            analysis.input_uploads.map((input) => (
              <li key={input.artifact_kind}>
                <span className={`analysis-input-state is-${input.state}`} aria-hidden="true" />
                <div>
                  <strong>{inputLabels[input.artifact_kind]}</strong>
                  <span>{input.mime} · {formatBytes(input.size)}</span>
                </div>
                <span>{inputStateLabels[input.state]}</span>
              </li>
            ))
          )}
        </ul>
      </section>

      {analysis.question ? (
        <section className="analysis-question" aria-labelledby="analysis-question-title">
          <h2 id="analysis-question-title">补充问题</h2>
          <p>{analysis.question}</p>
        </section>
      ) : null}

      {analysis.failure ? (
        <section className="analysis-failure" role="alert">
          <strong>错误代码</strong>
          <code>{analysis.failure.code}</code>
          <span>{analysis.failure.retryable ? "可以重试" : "需要检查输入或服务配置"}</span>
        </section>
      ) : null}
      </article>
      {report ? (
        <>
          <section className="analysis-report-entry" aria-label="最终报告入口">
            <div>
              <strong>最终性能报告已生成</strong>
              <p>在独立页面查看完整证据、AI 结论、优化建议与复测计划。</p>
            </div>
            <Link href={`/analyses/${analysis.analysis_id}/report`} prefetch={false}>
              打开完整报告
              <ArrowUpRight aria-hidden="true" />
            </Link>
          </section>
          <AnalysisReportView
            report={report}
            teamId={analysis.team_id}
            client={defaultClient}
            onRetrySynthesis={onRetrySynthesis}
            retrying={retrying}
          />
        </>
      ) : analysis.report_available && reportLoadFailed ? (
        <section className="analysis-report-load-state is-error" role="alert">
          <h2>报告暂时无法读取</h2>
          <p>分析状态仍来自当前团队数据库，请稍后重新加载。</p>
        </section>
      ) : analysis.report_available ? (
        <section className="analysis-report-load-state" role="status">
          <h2>正在读取报告</h2>
          <p>正在校验最新报告版本。</p>
        </section>
      ) : null}
    </div>
  );
}
