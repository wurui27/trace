"use client";

import {
  CheckCircle2,
  CircleAlert,
  LoaderCircle,
  Smartphone,
} from "lucide-react";

import type { RemoteDeviceView } from "../lib/perfpilot-api";
import { usePerfPilotSession } from "./perfpilot-session-provider";

function deviceName(device: RemoteDeviceView): string {
  const name = [device.manufacturer, device.model].filter(Boolean).join(" ").trim();
  return name || "Android 设备";
}

const stateCopy: Record<
  RemoteDeviceView["state"],
  { readonly connection: string; readonly detail: string }
> = {
  ready: { connection: "设备已就绪", detail: "可开始真机分析" },
  busy: { connection: "正在执行任务", detail: "任务结束后可再次使用" },
  unauthorized: { connection: "等待 USB 调试授权", detail: "请在设备上确认授权" },
  booting: { connection: "设备正在启动", detail: "等待 Android 启动完成" },
  quarantined: { connection: "设备暂不可用", detail: "请在 Agent 端检查设备状态" },
  offline: { connection: "设备离线", detail: "请检查数据线或网络连接" },
};

function DeviceCard({
  name,
  connection,
  detail,
  loading = false,
  healthy = false,
  children,
}: {
  readonly name: string;
  readonly connection: string;
  readonly detail: string;
  readonly loading?: boolean;
  readonly healthy?: boolean;
  readonly children?: React.ReactNode;
}) {
  const StatusIcon = loading ? LoaderCircle : healthy ? CheckCircle2 : CircleAlert;
  return (
    <div className="connected-device" aria-label={`${name}，${connection}，${detail}`}>
      <span className="device-icon">
        <Smartphone aria-hidden="true" />
      </span>
      <span className="device-details">
        <strong>{name}</strong>
        <span className="device-connection">
          <StatusIcon aria-hidden="true" />
          {connection}
        </span>
        <span className="device-os">{detail}</span>
        {children}
      </span>
    </div>
  );
}

export function ConnectedDevice() {
  const {
    status,
    deviceStatus,
    devices,
    selectedDevice,
    selectedDeviceId,
    selectDevice,
  } = usePerfPilotSession();

  if (status === "loading" || deviceStatus === "loading") {
    return (
      <DeviceCard
        name="正在读取设备"
        connection="正在连接设备目录"
        detail="请稍候"
        loading
      />
    );
  }
  if (status === "error" || deviceStatus === "error") {
    return (
      <DeviceCard
        name="设备目录不可用"
        connection="暂时无法读取状态"
        detail="请确认网页服务与 API 已启动"
      />
    );
  }
  if (devices.length === 0) {
    return (
      <DeviceCard
        name="尚未连接设备"
        connection="等待设备 Agent"
        detail="连接 Android 设备后会自动显示"
      />
    );
  }

  const displayed =
    selectedDevice ??
    devices.find((device) => device.state === "unauthorized") ??
    devices.find((device) => device.state === "busy") ??
    devices[0];
  const copy = stateCopy[displayed.state];
  const android = displayed.android_release
    ? `Android ${displayed.android_release}`
    : "Android 版本未知";

  return (
    <DeviceCard
      name={deviceName(displayed)}
      connection={copy.connection}
      detail={displayed.state === "ready" ? android : copy.detail}
      healthy={displayed.state === "ready"}
    >
      {devices.length > 1 ? (
        <label className="connected-device-selector">
          <span className="sr-only">选择 Android 设备</span>
          <select
            aria-label="选择 Android 设备"
            value={selectedDeviceId ?? ""}
            onChange={(event) => selectDevice(event.target.value || null)}
          >
            <option value="">选择设备</option>
            {devices.map((device) => (
              <option
                key={device.device_id}
                value={device.device_id}
                disabled={device.state !== "ready"}
              >
                {deviceName(device)} ({stateCopy[device.state].connection})
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </DeviceCard>
  );
}
