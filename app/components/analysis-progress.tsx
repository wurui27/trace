"use client";

import { useEffect, useState } from "react";

import {
  createPerfPilotClient,
  PerfPilotApiError,
  type AnalysisResponse,
  type AnalysisState,
  type PerfPilotClient,
  type TraceInputKind,
} from "../lib/perfpilot-api";

const TERMINAL_STATES = new Set<AnalysisState>([
  "completed",
  "partially_completed",
  "failed",
  "canceled",
  "deleted",
]);

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
    title: "分析已完成",
    description: "SmartPerfetto 结果已完成并安全归档。",
    tone: "success",
  },
  partially_completed: {
    title: "分析完成，部分证据不足",
    description: "已保留可验证结论，并明确标记缺少的证据。",
    tone: "warning",
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

export type AnalysisLoader = (
  analysisId: string,
  signal: AbortSignal,
  onAnalysis: (analysis: AnalysisResponse) => void,
) => Promise<void>;

export function createAnalysisLoader(
  client: PerfPilotClient = createPerfPilotClient(),
): AnalysisLoader {
  return async (analysisId, signal, onAnalysis) => {
    await client.csrf(signal);
    const me = await client.me(signal);
    let teamId: string | null = null;
    let current: AnalysisResponse | null = null;
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
    onAnalysis(current);
    let delay = 2_000;
    while (!TERMINAL_STATES.has(current.state)) {
      await wait(delay, signal);
      try {
        current = await client.analysis(teamId, analysisId, signal);
        onAnalysis(current);
        delay = 2_000;
      } catch (error) {
        if (!(error instanceof PerfPilotApiError) || !error.retryable) throw error;
        delay = Math.min(delay * 2, 15_000);
      }
    }
  };
}

const defaultLoader = createAnalysisLoader();

interface AnalysisProgressProps {
  readonly analysisId: string;
  readonly loader?: AnalysisLoader;
}

interface AnalysisSnapshot {
  readonly requestKey: string;
  readonly analysis: AnalysisResponse | null;
  readonly failed: boolean;
}

export function AnalysisProgress({
  analysisId,
  loader = defaultLoader,
}: AnalysisProgressProps) {
  const [snapshot, setSnapshot] = useState<AnalysisSnapshot | null>(null);
  const [attempt, setAttempt] = useState(0);
  const requestKey = `${analysisId}:${attempt}`;

  useEffect(() => {
    const controller = new AbortController();
    void loader(analysisId, controller.signal, (analysis) => {
      if (!controller.signal.aborted) {
        setSnapshot({ requestKey, analysis, failed: false });
      }
    }).catch(() => {
      if (!controller.signal.aborted) {
        setSnapshot({ requestKey, analysis: null, failed: true });
      }
    });
    return () => controller.abort();
  }, [analysisId, loader, requestKey]);

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
  if (current?.analysis == null) {
    return (
      <section className="analysis-load-state" role="status" aria-live="polite">
        <span className="analysis-loading-dot" aria-hidden="true" />
        <h1>正在读取分析状态</h1>
        <p>正在连接团队数据与 SmartPerfetto 执行记录。</p>
      </section>
    );
  }
  return <AnalysisProgressView analysis={current.analysis} />;
}

export function AnalysisProgressView({ analysis }: { readonly analysis: AnalysisResponse }) {
  const copy = stateCopy[analysis.state];
  const traceReady = analysis.input_uploads.some(
    (input) => input.artifact_kind === "trace" && input.state === "finalized",
  );
  const engineDone = TERMINAL_STATES.has(analysis.state);

  return (
    <article className="analysis-progress-card">
      <header className="analysis-progress-header">
        <div>
          <p className="page-eyebrow">TRACE ANALYSIS</p>
          <h1 className={`analysis-state-title is-${copy.tone}`}>{copy.title}</h1>
          <p>{copy.description}</p>
        </div>
        <span className={`analysis-state-badge is-${copy.tone}`}>{analysis.state}</span>
      </header>

      <dl className="analysis-identity">
        <div>
          <dt>分析编号</dt>
          <dd><code>{analysis.analysis_id}</code></dd>
        </div>
        <div>
          <dt>分析重点</dt>
          <dd>{analysis.analysis_profile === "auto" ? "自动识别" : analysis.analysis_profile === "startup" ? "启动性能" : "滑动与卡顿"}</dd>
        </div>
        <div>
          <dt>版本</dt>
          <dd>v{analysis.version}</dd>
        </div>
      </dl>

      <section className="analysis-stage-section" aria-labelledby="analysis-stage-title">
        <h2 id="analysis-stage-title">处理进度</h2>
        <ol className="analysis-stage-list">
          <li className={traceReady ? "is-complete" : "is-current"}>
            <span aria-hidden="true" />
            <div><strong>文件校验</strong><p>确认大小、类型与 SHA-256。</p></div>
          </li>
          <li className={engineDone ? "is-complete" : traceReady ? "is-current" : ""}>
            <span aria-hidden="true" />
            <div><strong>SmartPerfetto 解析</strong><p>分析 Trace 中的关键性能路径。</p></div>
          </li>
          <li className={analysis.report_available ? "is-complete" : engineDone ? "is-current" : ""}>
            <span aria-hidden="true" />
            <div><strong>结果归档</strong><p>保留可追溯的原始结果与报告状态。</p></div>
          </li>
        </ol>
      </section>

      <section className="analysis-input-section" aria-labelledby="analysis-input-title">
        <div className="analysis-section-heading">
          <h2 id="analysis-input-title">输入文件</h2>
          <span>{analysis.input_uploads.length} 项</span>
        </div>
        <ul className="analysis-input-list">
          {analysis.input_uploads.map((input) => (
            <li key={input.artifact_kind}>
              <span className={`analysis-input-state is-${input.state}`} aria-hidden="true" />
              <div>
                <strong>{inputLabels[input.artifact_kind]}</strong>
                <span>{input.mime} · {formatBytes(input.size)}</span>
              </div>
              <span>{inputStateLabels[input.state]}</span>
            </li>
          ))}
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
  );
}
