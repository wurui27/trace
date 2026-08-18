// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { DeviceAnalysisForm } from "../app/components/device-analysis-form";
import { PerfPilotSessionProvider } from "../app/components/perfpilot-session-provider";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

afterEach(cleanup);

it("requires one ready device and explains how to make a device available", async () => {
  const client = {
    csrf: vi.fn().mockResolvedValue("csrf"),
    me: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      memberships: [{ team: { id: "team-1", name: "Ray" }, role: "owner" }],
    }),
    devices: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      devices: [
        {
          device_id: "device-1",
          agent_id: "agent-1",
          agent_name: "Windows Agent",
          serial_suffix: "42AA",
          manufacturer: "Google",
          model: "Pixel 7",
          android_release: "14",
          api_level: 34,
          connection_type: "usb",
          adb_state: "unauthorized",
          state: "unauthorized",
          last_seen_at: "2026-08-05T08:00:00Z",
        },
      ],
    }),
  } as unknown as PerfPilotClient;

  render(
    <PerfPilotSessionProvider client={client}>
      <DeviceAnalysisForm />
    </PerfPilotSessionProvider>,
  );

  expect(await screen.findByText("等待 USB 调试授权")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始真机分析" })).toBeDisabled();
});

it("submits a ready device script capture without asking for an APK", async () => {
  const user = userEvent.setup();
  const submitter = vi.fn().mockResolvedValue({
    teamId: "team-1",
    analysis: { analysis_id: "analysis-1" },
  });
  const client = {
    csrf: vi.fn().mockResolvedValue("csrf"),
    me: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      memberships: [{ team: { id: "team-1", name: "Ray" }, role: "owner" }],
    }),
    devices: vi.fn().mockResolvedValue({
      schema_version: "1.1",
      devices: [
        {
          device_id: "device-1",
          agent_id: "agent-1",
          agent_name: "Mac Agent",
          serial_suffix: "CDEF",
          manufacturer: "Rivotek",
          model: "Media Center",
          android_release: "13",
          api_level: 33,
          connection_type: "usb",
          adb_state: "device",
          state: "ready",
          last_seen_at: "2026-08-18T08:00:00Z",
          launch_targets: [
            {
              package_name: "com.rivotek.mediacenter",
              launch_activity: "com.rivotek.mediacenter/.shell.MediaCenterActivity",
            },
          ],
        },
      ],
    }),
    sourceWorkspaces: vi.fn().mockResolvedValue({ schema_version: "1.0", workspaces: [] }),
  } as unknown as PerfPilotClient;

  render(
    <PerfPilotSessionProvider client={client}>
      <DeviceAnalysisForm submitter={submitter} />
    </PerfPilotSessionProvider>,
  );

  expect(await screen.findByText("Rivotek Media Center")).toBeInTheDocument();
  expect(screen.queryByLabelText("APK 文件")).not.toBeInTheDocument();
  expect(screen.getByRole("radio", { name: "冷启动" })).toBeChecked();
  expect(screen.getByRole("radio", { name: "自动" })).toBeChecked();
  expect(screen.getByLabelText("测试时长（秒）")).toHaveValue(15);
  expect(screen.getByLabelText("包名")).toHaveValue("com.rivotek.mediacenter");

  await user.clear(screen.getByLabelText("测试时长（秒）"));
  await user.type(screen.getByLabelText("测试时长（秒）"), "20");
  await user.click(screen.getByRole("button", { name: "开始真机分析" }));

  await waitFor(() => expect(submitter).toHaveBeenCalledOnce());
  expect(submitter).toHaveBeenCalledWith(
    expect.objectContaining({
      teamId: "team-1",
      deviceId: "device-1",
      testType: "cold_start",
      launchMode: "automatic",
      durationSeconds: 20,
      target: {
        package_name: "com.rivotek.mediacenter",
        launch_activity: "com.rivotek.mediacenter/.shell.MediaCenterActivity",
      },
    }),
  );
});

it("uses manual launch for cold or hot starts and keeps scroll manual", async () => {
  const user = userEvent.setup();
  const client = {
    csrf: vi.fn().mockResolvedValue("csrf"),
    me: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      memberships: [{ team: { id: "team-1", name: "Ray" }, role: "owner" }],
    }),
    devices: vi.fn().mockResolvedValue({
      schema_version: "1.1",
      devices: [
        {
          device_id: "device-1",
          agent_id: "agent-1",
          agent_name: "Mac Agent",
          serial_suffix: "CDEF",
          manufacturer: "Rivotek",
          model: "Media Center",
          android_release: "13",
          api_level: 33,
          connection_type: "usb",
          adb_state: "device",
          state: "ready",
          last_seen_at: "2026-08-18T08:00:00Z",
          launch_targets: [
            {
              package_name: "com.rivotek.mediacenter",
              launch_activity: "com.rivotek.mediacenter/.shell.MediaCenterActivity",
            },
          ],
        },
      ],
    }),
    sourceWorkspaces: vi.fn().mockResolvedValue({ schema_version: "1.0", workspaces: [] }),
  } as unknown as PerfPilotClient;

  render(
    <PerfPilotSessionProvider client={client}>
      <DeviceAnalysisForm />
    </PerfPilotSessionProvider>,
  );

  expect(await screen.findByText("Rivotek Media Center")).toBeInTheDocument();
  await user.click(screen.getByRole("radio", { name: "手动" }));
  expect(screen.queryByLabelText("包名")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("启动类")).not.toBeInTheDocument();

  await user.click(screen.getByRole("radio", { name: "滑动" }));
  expect(screen.queryByRole("radio", { name: "自动" })).not.toBeInTheDocument();
  expect(screen.queryByRole("radio", { name: "手动" })).not.toBeInTheDocument();
  expect(screen.getByText(/滑动测试固定为手动/)).toBeInTheDocument();
  expect(screen.getByLabelText("包名")).toBeInTheDocument();
  expect(screen.getByLabelText("启动类")).toBeInTheDocument();
});
