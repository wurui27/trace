"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ShieldCheck } from "lucide-react";

import {
  enqueueDeviceAnalysis,
  PerfPilotApiError,
  type DeviceSubmissionPhase,
  type SubmitDeviceAnalysisInput,
  type SubmittedTraceAnalysis,
} from "../lib/perfpilot-api";
import { usePerfPilotSession } from "./perfpilot-session-provider";

export type DeviceSubmitter = (
  submission: SubmitDeviceAnalysisInput,
) => Promise<SubmittedTraceAnalysis>;

interface DeviceAnalysisFormProps {
  readonly submitter?: DeviceSubmitter;
  readonly onCancel?: () => void;
  readonly onSubmitted?: (result: SubmittedTraceAnalysis) => void;
}

const phaseText: Record<DeviceSubmissionPhase, string> = {
  session: "正在验证账号与团队…",
  hashing: "正在校验 APK 完整性…",
  creating: "正在创建真机任务…",
  uploading: "正在安全上传 APK…",
  submitted: "任务已提交，正在切换到后台执行…",
};

function errorText(error: unknown): string {
  if (error instanceof PerfPilotApiError) {
    const messages: Record<string, string> = {
      device_required: "请选择一台已就绪的 Android 设备。",
      apk_required: "请选择一个 APK 文件。",
      invalid_file: "APK 为空或超过 5 GB 限制。",
      analysis_device_unavailable: "设备状态已变化，请重新选择后再试。",
      object_upload_failed: "APK 上传失败，请重试。",
      upload_authorization_expired: "上传授权已过期，请重新提交。",
      network_unavailable: "网络连接不可用，请稍后重试。",
    };
    return messages[error.code] ?? "真机分析暂时无法启动，请稍后重试。";
  }
  return "真机分析暂时无法启动，请稍后重试。";
}

function selectedDeviceName(manufacturer: string | null, model: string | null): string {
  return [manufacturer, model].filter(Boolean).join(" ").trim() || "Android 设备";
}

export function DeviceAnalysisForm({
  submitter,
  onCancel,
  onSubmitted,
}: DeviceAnalysisFormProps = {}) {
  const { client, team, devices, selectedDevice, deviceStatus } = usePerfPilotSession();
  const [apk, setApk] = useState<File | null>(null);
  const [phase, setPhase] = useState<DeviceSubmissionPhase | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const busy = phase !== null;

  useEffect(
    () => () => {
      abortRef.current?.abort();
    },
    [],
  );

  const unavailableMessage = (() => {
    if (deviceStatus === "loading") return "正在读取设备";
    if (deviceStatus === "error") return "设备目录暂时不可用";
    if (devices.some((device) => device.state === "unauthorized")) {
      return "等待 USB 调试授权";
    }
    if (devices.some((device) => device.state === "busy")) return "设备正在执行任务";
    if (devices.some((device) => device.state === "offline")) return "设备离线";
    return "尚未连接可用设备";
  })();

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (team === null || selectedDevice === null || apk === null || busy) return;
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setError(null);
    setPhase("session");
    try {
      const run =
        submitter ??
        ((submission: SubmitDeviceAnalysisInput) =>
          enqueueDeviceAnalysis(submission, { client }));
      const result = await run({
        teamId: team.id,
        deviceId: selectedDevice.device_id,
        apk,
        signal: controller.signal,
        onProgress: setPhase,
      });
      setPhase(null);
      onSubmitted?.(result);
    } catch (caught) {
      if (controller.signal.aborted) {
        setPhase(null);
        return;
      }
      setPhase(null);
      setError(errorText(caught));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const cancel = () => {
    abortRef.current?.abort();
    onCancel?.();
  };

  return (
    <form
      className="device-analysis-form"
      onSubmit={handleSubmit}
      aria-busy={busy}
      noValidate
    >
      <div className="new-analysis-fields">
        <div className="device-analysis-target" role="status">
          {selectedDevice ? (
            <>
              <strong>
                {selectedDeviceName(selectedDevice.manufacturer, selectedDevice.model)}
              </strong>
              <span>
                由 {selectedDevice.agent_name} 执行启动、滑动和内存循环测试
              </span>
            </>
          ) : (
            <>
              <strong>{unavailableMessage}</strong>
              <span>连接设备并启动设备 Agent 后即可开始。</span>
            </>
          )}
        </div>

        <div className="new-analysis-field new-analysis-field-required">
          <label htmlFor="device-apk-file">APK 文件</label>
          <input
            id="device-apk-file"
            type="file"
            accept=".apk,application/vnd.android.package-archive"
            required
            disabled={busy}
            onChange={(event) => {
              setApk(event.target.files?.[0] ?? null);
              setError(null);
            }}
          />
          <span className="new-analysis-field-hint">
            Agent 会安装 APK，并按固定顺序采集三类性能证据。
          </span>
        </div>

        {phase ? (
          <p className="new-analysis-progress" role="status" aria-live="polite">
            {phaseText[phase]}
          </p>
        ) : null}
        {error ? (
          <p className="new-analysis-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>

      <div className="new-analysis-dialog-footer">
        <p className="new-analysis-privacy-note">
          <ShieldCheck aria-hidden="true" />
          设备序列号不会发送到网页端
        </p>
        <div className="new-analysis-dialog-actions">
          <button type="button" className="secondary-action" onClick={cancel}>
            取消
          </button>
          <button
            type="submit"
            className="primary-action"
            disabled={busy || team === null || selectedDevice === null || apk === null}
          >
            {busy ? "正在提交…" : "开始真机分析"}
          </button>
        </div>
      </div>
    </form>
  );
}
