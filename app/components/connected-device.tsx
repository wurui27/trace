"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, CircleAlert, LoaderCircle, Smartphone } from "lucide-react";

import {
  createPerfPilotClient,
  type LocalDeviceStatusResponse,
} from "../lib/perfpilot-api";

type DeviceView =
  | { readonly state: "loading" }
  | { readonly state: "error" }
  | { readonly state: "ready"; readonly status: LocalDeviceStatusResponse };

function EmptyDevice({
  name,
  connection,
  detail,
}: {
  readonly name: string;
  readonly connection: string;
  readonly detail: string;
}) {
  return (
    <div className="connected-device" aria-label={`${name}，${connection}，${detail}`}>
      <span className="device-icon">
        <Smartphone aria-hidden="true" />
      </span>
      <span className="device-details">
        <strong>{name}</strong>
        <span className="device-connection">
          <CircleAlert aria-hidden="true" />
          {connection}
        </span>
        <span className="device-os">{detail}</span>
      </span>
    </div>
  );
}

export function ConnectedDevice() {
  const [view, setView] = useState<DeviceView>({ state: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    createPerfPilotClient()
      .device(controller.signal)
      .then((status) => setView({ state: "ready", status }))
      .catch(() => {
        if (!controller.signal.aborted) setView({ state: "error" });
      });
    return () => controller.abort();
  }, []);

  if (view.state === "loading") {
    return (
      <div className="connected-device" aria-label="正在检测 ADB 设备">
        <span className="device-icon">
          <LoaderCircle aria-hidden="true" />
        </span>
        <span className="device-details">
          <strong>正在检测设备</strong>
          <span className="device-connection">正在读取 ADB</span>
          <span className="device-os">请稍候</span>
        </span>
      </div>
    );
  }
  if (view.state === "error" || view.status.state === "unavailable") {
    return (
      <EmptyDevice
        name="未检测到设备"
        connection="ADB 不可用"
        detail="请确认本地 API 已启动"
      />
    );
  }
  if (view.status.state === "multiple") {
    return (
      <EmptyDevice
        name="检测到多台设备"
        connection="暂未选择设备"
        detail="请只保留一台 ADB 设备"
      />
    );
  }
  if (view.status.state === "unauthorized") {
    return (
      <EmptyDevice
        name="设备尚未授权"
        connection="等待 USB 调试授权"
        detail="请在设备上确认授权"
      />
    );
  }
  if (view.status.state === "disconnected" || view.status.device === null) {
    return (
      <EmptyDevice
        name="未检测到设备"
        connection="ADB 未连接"
        detail="请连接 Android 设备"
      />
    );
  }
  const device = view.status.device;
  return (
    <div
      className="connected-device"
      aria-label={`${device.name}，ADB 已连接，${device.os}，序列号 ${device.serial}`}
      title={`序列号：${device.serial}`}
    >
      <span className="device-icon">
        <Smartphone aria-hidden="true" />
      </span>
      <span className="device-details">
        <strong>{device.name}</strong>
        <span className="device-connection">
          <CheckCircle2 aria-hidden="true" />
          ADB 已连接
        </span>
        <span className="device-os">{device.os}</span>
      </span>
    </div>
  );
}
