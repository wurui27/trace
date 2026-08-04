"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ShieldCheck } from "lucide-react";

import {
  enqueueTraceAnalysis,
  PerfPilotApiError,
  type SubmitTraceInput,
  type SubmittedTraceAnalysis,
  type TraceInputKind,
  type TraceProfile,
  type TraceSubmissionPhase,
} from "../lib/perfpilot-api";

export type TraceSubmitter = (
  submission: SubmitTraceInput,
) => Promise<SubmittedTraceAnalysis>;

interface TraceUploadFormProps {
  readonly submitter?: TraceSubmitter;
  readonly onCancel?: () => void;
  readonly onSubmitted?: (result: SubmittedTraceAnalysis) => void;
}

const phaseText: Record<TraceSubmissionPhase, string> = {
  session: "正在验证账号与团队…",
  hashing: "正在校验文件完整性…",
  creating: "正在创建分析任务…",
  uploading: "正在安全上传文件…",
  submitted: "任务已提交，正在切换到后台分析…",
};

function errorText(error: unknown): string {
  if (error instanceof PerfPilotApiError) {
    const messages: Record<string, string> = {
      unauthenticated: "请先登录 PerfPilot 后再开始分析。",
      csrf_unavailable: "会话已失效，请刷新页面后重试。",
      trace_required: "请选择一个 Trace 文件。",
      invalid_file: "文件为空或超过 5 GB 限制。",
      team_required: "当前账号尚未加入可用团队。",
      object_upload_failed: "文件上传失败，请重试。",
      upload_authorization_expired: "上传授权已过期，请重新提交。",
      invalid_upload_authorization: "上传授权无效，请重新提交。",
      network_unavailable: "网络连接不可用，请稍后重试。",
      proxy_configuration_invalid: "本地分析服务未配置或未启动，请先启动本地服务。",
      upstream_unavailable: "本地分析服务当前不可用，请确认服务已启动。",
      upstream_timeout: "本地分析服务响应超时，请稍后重试。",
    };
    return messages[error.code] ?? "分析暂时无法启动，请稍后重试。";
  }
  return "分析暂时无法启动，请稍后重试。";
}

export function TraceUploadForm({
  submitter = enqueueTraceAnalysis,
  onCancel,
  onSubmitted,
}: TraceUploadFormProps) {
  const [profile, setProfile] = useState<TraceProfile>("auto");
  const [question, setQuestion] = useState("");
  const [files, setFiles] = useState<Partial<Record<TraceInputKind, File>>>({});
  const [phase, setPhase] = useState<TraceSubmissionPhase | null>(null);
  const [phaseDetail, setPhaseDetail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const busy = phase !== null && error === null;

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const selectFile = (kind: TraceInputKind, file: File | undefined) => {
    setFiles((current) => {
      const next = { ...current };
      if (file) {
        next[kind] = file;
      } else {
        delete next[kind];
      }
      return next;
    });
    setError(null);
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!files.trace || busy) {
      return;
    }
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setError(null);
    setPhase("session");
    setPhaseDetail(null);
    try {
      const selectedFiles = Object.entries(files).map(([kind, file]) => ({
        kind: kind as TraceInputKind,
        file,
      }));
      const submitted = await submitter({
        profile,
        question,
        files: selectedFiles,
        signal: controller.signal,
        onProgress: (nextPhase, detail) => {
          setPhase(nextPhase);
          setPhaseDetail(detail ?? null);
        },
      });
      setPhase(null);
      setPhaseDetail(null);
      onSubmitted?.(submitted);
    } catch (caught) {
      if (controller.signal.aborted) {
        setPhase(null);
        setPhaseDetail(null);
        return;
      }
      setError(errorText(caught));
      setPhase(null);
      setPhaseDetail(null);
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  };

  const cancel = () => {
    abortRef.current?.abort();
    onCancel?.();
  };

  const disabled = busy;

  return (
    <form
      className="trace-upload-form"
      onSubmit={handleSubmit}
      aria-busy={busy}
      noValidate
    >
      <div className="new-analysis-fields">
        <div className="new-analysis-field">
          <label htmlFor="trace-analysis-profile">分析重点</label>
          <select
            id="trace-analysis-profile"
            value={profile}
            onChange={(event) => setProfile(event.target.value as TraceProfile)}
            disabled={disabled}
          >
            <option value="auto">自动识别关键问题</option>
            <option value="startup">启动性能</option>
            <option value="scroll">页面滑动与卡顿</option>
          </select>
        </div>

        <div className="new-analysis-field">
          <label htmlFor="trace-question">补充问题（可选）</label>
          <textarea
            id="trace-question"
            value={question}
            maxLength={2000}
            rows={3}
            placeholder="例如：首帧前为什么慢？"
            onChange={(event) => setQuestion(event.target.value)}
            disabled={disabled}
          />
        </div>

        <div className="new-analysis-field new-analysis-field-required">
          <label htmlFor="trace-file">Trace 文件</label>
          <input
            id="trace-file"
            type="file"
            accept=".perfetto-trace,.trace,.ctrace,.pb"
            required
            disabled={disabled}
            onChange={(event) => selectFile("trace", event.target.files?.[0])}
          />
          <span className="new-analysis-field-hint">
            支持 Perfetto Trace，SmartPerfetto 将首先解析这份数据。
          </span>
        </div>

        <div className="trace-optional-files" aria-label="可选辅助文件">
          <div className="new-analysis-field">
            <label htmlFor="memory-evidence-file">内存证据（可选）</label>
            <input
              id="memory-evidence-file"
              type="file"
              accept=".zip,.hprof,.json"
              disabled={disabled}
              onChange={(event) =>
                selectFile("memory_evidence", event.target.files?.[0])
              }
            />
          </div>
          <div className="new-analysis-field">
            <label htmlFor="trace-apk-file">APK 文件（可选）</label>
            <input
              id="trace-apk-file"
              type="file"
              accept=".apk"
              disabled={disabled}
              onChange={(event) => selectFile("apk", event.target.files?.[0])}
            />
          </div>
          <div className="new-analysis-field">
            <label htmlFor="source-archive-file">源码压缩包（可选）</label>
            <input
              id="source-archive-file"
              type="file"
              accept=".zip,.tar,.tar.gz,.tgz"
              disabled={disabled}
              onChange={(event) =>
                selectFile("source_archive", event.target.files?.[0])
              }
            />
          </div>
          <div className="new-analysis-field">
            <label htmlFor="mapping-file">Mapping 文件（可选）</label>
            <input
              id="mapping-file"
              type="file"
              accept=".txt"
              disabled={disabled}
              onChange={(event) => selectFile("mapping", event.target.files?.[0])}
            />
          </div>
          <div className="new-analysis-field">
            <label htmlFor="native-symbols-file">Native Symbols（可选）</label>
            <input
              id="native-symbols-file"
              type="file"
              accept=".zip,.tar,.tar.gz,.tgz,.so"
              disabled={disabled}
              onChange={(event) =>
                selectFile("native_symbols", event.target.files?.[0])
              }
            />
          </div>
          <div className="new-analysis-field">
            <label htmlFor="trace-log-file">日志文件（可选）</label>
            <input
              id="trace-log-file"
              type="file"
              accept=".txt,.log"
              disabled={disabled}
              onChange={(event) => selectFile("log", event.target.files?.[0])}
            />
          </div>
        </div>

        {phase ? (
          <div className="trace-upload-status" role="status" aria-live="polite">
            <span className="trace-upload-status-dot" aria-hidden="true" />
            <span>
              {phaseText[phase]}
              {phaseDetail ? ` · ${phaseDetail}` : ""}
            </span>
          </div>
        ) : null}
        {error ? <p className="trace-upload-error" role="alert">{error}</p> : null}
      </div>

      <footer className="new-analysis-dialog-footer">
        <p className="new-analysis-privacy-note">
          <ShieldCheck aria-hidden="true" />
          文件直传到团队隔离存储，不经过网页服务器。
        </p>
        <div className="new-analysis-dialog-actions">
          <button type="button" onClick={cancel}>
            {busy ? "停止" : "取消"}
          </button>
          <button
            type="submit"
            className="primary-action"
            disabled={!files.trace || disabled}
          >
            {busy ? "处理中…" : "开始分析"}
          </button>
        </div>
      </footer>
    </form>
  );
}
