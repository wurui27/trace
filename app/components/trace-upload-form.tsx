"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ShieldCheck } from "lucide-react";

import {
  enqueueTraceAnalysis,
  PerfPilotApiError,
  type SubmitTraceInput,
  type SubmittedTraceAnalysis,
  type PerfPilotClient,
  type SourceBinding,
  type TraceSubmissionPhase,
  type UploadedTraceTestType,
} from "../lib/perfpilot-api";
import { SourceWorkspaceField } from "./source-workspace-field";

export type TraceSubmitter = (
  submission: SubmitTraceInput,
) => Promise<SubmittedTraceAnalysis>;

interface TraceUploadFormProps {
  readonly submitter?: TraceSubmitter;
  readonly onCancel?: () => void;
  readonly onSubmitted?: (result: SubmittedTraceAnalysis) => void;
  readonly client?: PerfPilotClient;
  readonly teamId?: string | null;
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
  client,
  teamId,
}: TraceUploadFormProps) {
  const [testType, setTestType] = useState<UploadedTraceTestType>("cold_start");
  const [packageName, setPackageName] = useState("");
  const [customTestName, setCustomTestName] = useState("");
  const [customTestDescription, setCustomTestDescription] = useState("");
  const [question, setQuestion] = useState("");
  const [trace, setTrace] = useState<File | null>(null);
  const [phase, setPhase] = useState<TraceSubmissionPhase | null>(null);
  const [phaseDetail, setPhaseDetail] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sourceBinding, setSourceBinding] = useState<SourceBinding | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const busy = phase !== null && error === null;

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (
      trace === null ||
      packageName.trim() === "" ||
      (testType === "other" &&
        (customTestName.trim() === "" || customTestDescription.trim() === "")) ||
      busy
    ) {
      return;
    }
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setError(null);
    setPhase("session");
    setPhaseDetail(null);
    try {
      const submitted = await submitter({
        testType,
        packageName,
        customTestName: testType === "other" ? customTestName : undefined,
        customTestDescription:
          testType === "other" ? customTestDescription : undefined,
        question,
        trace,
        sourceBinding: sourceBinding ?? undefined,
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
  const submitDisabled =
    disabled ||
    trace === null ||
    packageName.trim() === "" ||
    (testType === "other" &&
      (customTestName.trim() === "" || customTestDescription.trim() === ""));

  return (
    <form
      className="trace-upload-form"
      onSubmit={handleSubmit}
      aria-busy={busy}
      noValidate
    >
      <div className="new-analysis-fields">
        <div className="new-analysis-field">
          <label htmlFor="trace-test-type">测试类型</label>
          <select
            id="trace-test-type"
            value={testType}
            onChange={(event) =>
              setTestType(event.target.value as UploadedTraceTestType)
            }
            disabled={disabled}
          >
            <option value="cold_start">冷启动</option>
            <option value="hot_start">热启动</option>
            <option value="scroll">滑动</option>
            <option value="other">其他</option>
          </select>
        </div>

        <div className="new-analysis-field new-analysis-field-required">
          <label htmlFor="trace-package-name">应用包名</label>
          <input
            id="trace-package-name"
            type="text"
            value={packageName}
            placeholder="例如：com.rivotek.mediacenter"
            autoComplete="off"
            required
            disabled={disabled}
            onChange={(event) => setPackageName(event.target.value)}
          />
        </div>

        {testType === "other" ? (
          <>
            <div className="new-analysis-field new-analysis-field-required">
              <label htmlFor="trace-custom-test-name">测试名称</label>
              <input
                id="trace-custom-test-name"
                type="text"
                value={customTestName}
                maxLength={80}
                required
                disabled={disabled}
                onChange={(event) => setCustomTestName(event.target.value)}
              />
            </div>
            <div className="new-analysis-field new-analysis-field-required">
              <label htmlFor="trace-custom-test-description">测试说明</label>
              <textarea
                id="trace-custom-test-description"
                value={customTestDescription}
                maxLength={500}
                rows={3}
                placeholder="说明测试做什么、关注哪段业务流程。"
                required
                disabled={disabled}
                onChange={(event) => setCustomTestDescription(event.target.value)}
              />
            </div>
          </>
        ) : null}

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
            onChange={(event) => {
              setTrace(event.target.files?.[0] ?? null);
              setError(null);
            }}
          />
          <span className="new-analysis-field-hint">
            支持 Perfetto Trace，SmartPerfetto 将首先解析这份数据。
          </span>
        </div>

        <SourceWorkspaceField
          client={client}
          teamId={teamId}
          value={sourceBinding}
          onChange={setSourceBinding}
          disabled={disabled}
        />

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
            disabled={submitDisabled}
          >
            {busy ? "处理中…" : "开始分析"}
          </button>
        </div>
      </footer>
    </form>
  );
}
