"use client";

import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ShieldCheck } from "lucide-react";

import {
  enqueueDeviceAnalysis,
  PerfPilotApiError,
  type DeviceSubmissionPhase,
  type DeviceLaunchMode,
  type DeviceTestType,
  type SourceBinding,
  type SubmitDeviceAnalysisInput,
  type SubmittedTraceAnalysis,
} from "../lib/perfpilot-api";
import { usePerfPilotSession } from "./perfpilot-session-provider";
import { SourceWorkspaceField } from "./source-workspace-field";

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
  creating: "正在创建真机任务…",
  submitted: "任务已提交，正在切换到后台执行…",
};

function errorText(error: unknown): string {
  if (error instanceof PerfPilotApiError) {
    const messages: Record<string, string> = {
      device_required: "请选择一台已就绪的 Android 设备。",
      capture_configuration_invalid: "请选择完整的测试类型、启动方式和目标应用。",
      launch_target_unavailable: "设备上的目标应用已变化，请重新选择。",
      analysis_device_unavailable: "设备状态已变化，请重新选择后再试。",
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
  const [testType, setTestType] = useState<DeviceTestType>("cold_start");
  const [launchMode, setLaunchMode] = useState<DeviceLaunchMode>("automatic");
  const [durationSeconds, setDurationSeconds] = useState(15);
  const [packageName, setPackageName] = useState("");
  const [launchActivity, setLaunchActivity] = useState("");
  const [phase, setPhase] = useState<DeviceSubmissionPhase | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sourceBinding, setSourceBinding] = useState<SourceBinding | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const busy = phase !== null;
  const launchTargets = useMemo(
    () => selectedDevice?.launch_targets ?? [],
    [selectedDevice],
  );
  const packages = useMemo(
    () => Array.from(new Set(launchTargets.map((target) => target.package_name))).sort(),
    [launchTargets],
  );
  const selectedPackageName = packages.includes(packageName)
    ? packageName
    : (packages[0] ?? "");
  const activities = useMemo(
    () => launchTargets.filter((target) => target.package_name === selectedPackageName),
    [launchTargets, selectedPackageName],
  );
  const selectedLaunchActivity = activities.some(
    (target) => target.launch_activity === launchActivity,
  )
    ? launchActivity
    : (activities[0]?.launch_activity ?? "");
  const requiresTarget = launchMode === "automatic" || testType === "scroll";
  const selectedTarget = requiresTarget
    ? launchTargets.find(
        (target) =>
          target.package_name === selectedPackageName &&
          target.launch_activity === selectedLaunchActivity,
      ) ?? null
    : null;
  const canSubmit =
    !busy &&
    team !== null &&
    selectedDevice !== null &&
    Number.isInteger(durationSeconds) &&
    durationSeconds >= 1 &&
    durationSeconds <= 300 &&
    (!requiresTarget || selectedTarget !== null);

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
    if (!canSubmit || team === null || selectedDevice === null) return;
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
        testType,
        launchMode,
        durationSeconds,
        target: selectedTarget,
        sourceBinding: sourceBinding ?? undefined,
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
                由 {selectedDevice.agent_name} 执行所选真机测试
              </span>
            </>
          ) : (
            <>
              <strong>{unavailableMessage}</strong>
              <span>连接设备并启动设备 Agent 后即可开始。</span>
            </>
          )}
        </div>

        <fieldset className="new-analysis-field new-analysis-field-required">
          <legend>测试类别</legend>
          <div className="device-analysis-options">
            {(
              [
                ["cold_start", "冷启动"],
                ["hot_start", "热启动"],
                ["scroll", "滑动"],
              ] as const
            ).map(([value, label]) => (
              <label key={value}>
                <input
                  type="radio"
                  name="device-test-type"
                  value={value}
                  checked={testType === value}
                  disabled={busy}
                  onChange={() => {
                    setTestType(value);
                    if (value === "scroll") setLaunchMode("manual");
                    setError(null);
                  }}
                />
                {label}
              </label>
            ))}
          </div>
        </fieldset>

        {testType !== "scroll" ? (
          <fieldset className="new-analysis-field new-analysis-field-required">
            <legend>启动方式</legend>
            <div className="device-analysis-options">
              {(
                [
                  ["automatic", "自动"],
                  ["manual", "手动"],
                ] as const
              ).map(([value, label]) => (
                <label key={value}>
                  <input
                    type="radio"
                    name="device-launch-mode"
                    value={value}
                    checked={launchMode === value}
                    disabled={busy}
                    onChange={() => {
                      setLaunchMode(value);
                      setError(null);
                    }}
                  />
                  {label}
                </label>
              ))}
            </div>
            <span className="new-analysis-field-hint">
              手动模式开始录制后，请在设备上自行启动或恢复应用。
            </span>
          </fieldset>
        ) : (
          <p className="new-analysis-field-hint">
            滑动测试固定为手动：开始后请在设备目标页面上手动滑动。
          </p>
        )}

        {requiresTarget ? (
          <div className="device-analysis-target-grid">
            <div className="new-analysis-field new-analysis-field-required">
              <label htmlFor="device-package-name">包名</label>
              <select
                id="device-package-name"
                value={selectedPackageName}
                disabled={busy || packages.length === 0}
                required
                onChange={(event) => {
                  const nextPackageName = event.target.value;
                  setPackageName(nextPackageName);
                  setLaunchActivity(
                    launchTargets.find(
                      (target) => target.package_name === nextPackageName,
                    )?.launch_activity ?? "",
                  );
                }}
              >
                {packages.length === 0 ? <option value="">设备未上报可启动应用</option> : null}
                {packages.map((value) => (
                  <option key={value} value={value}>{value}</option>
                ))}
              </select>
            </div>
            <div className="new-analysis-field new-analysis-field-required">
              <label htmlFor="device-launch-activity">启动类</label>
              <select
                id="device-launch-activity"
                value={selectedLaunchActivity}
                disabled={busy || activities.length === 0}
                required
                onChange={(event) => setLaunchActivity(event.target.value)}
              >
                {activities.length === 0 ? <option value="">暂无启动类</option> : null}
                {activities.map((target) => (
                  <option key={target.launch_activity} value={target.launch_activity}>
                    {target.launch_activity}
                  </option>
                ))}
              </select>
              {testType === "scroll" ? (
                <span className="new-analysis-field-hint">仅用于性能归因，不会自动启动应用。</span>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="new-analysis-field new-analysis-field-required">
          <label htmlFor="device-duration-seconds">测试时长（秒）</label>
          <input
            id="device-duration-seconds"
            type="number"
            min={1}
            max={300}
            step={1}
            value={durationSeconds}
            disabled={busy}
            required
            onChange={(event) => setDurationSeconds(Number(event.target.value))}
          />
          <span className="new-analysis-field-hint">默认 15 秒，可设置 1–300 秒。</span>
        </div>

        <SourceWorkspaceField
          client={client}
          teamId={team?.id ?? null}
          value={sourceBinding}
          onChange={setSourceBinding}
          disabled={busy}
        />

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
            disabled={!canSubmit}
          >
            {busy ? "正在提交…" : "开始真机分析"}
          </button>
        </div>
      </div>
    </form>
  );
}
