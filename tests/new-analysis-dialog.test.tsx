// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { NewAnalysisDialog } from "../app/components/new-analysis-dialog";
import { PerfPilotSessionProvider } from "../app/components/perfpilot-session-provider";
import type { DeviceSubmitter } from "../app/components/device-analysis-form";
import type { TraceSubmitter } from "../app/components/trace-upload-form";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

afterEach(cleanup);

it("closes after the backend accepts the uploaded Trace", async () => {
  const user = userEvent.setup();
  const submitter: TraceSubmitter = vi.fn().mockResolvedValue({
    teamId: "team-1",
    analysis: {
      schema_version: "1.0",
      analysis_id: "analysis-active-1",
      team_id: "team-1",
      analysis_mode: "trace_upload",
      analysis_profile: "auto",
      question: null,
      state: "analyzing",
      version: 3,
      created_at: "2026-08-04T08:00:00Z",
      cancel_requested_at: null,
      report_available: false,
      input_uploads: [],
      stages: [
        { stage: "input_validation", state: "completed", failure: null },
        { stage: "smartperfetto", state: "running", failure: null },
        { stage: "perfpilot_ai", state: "pending", failure: null },
        { stage: "report", state: "pending", failure: null },
      ],
      failure: null,
    },
  });
  const onSubmitted = vi.fn();
  render(
    <NewAnalysisDialog submitter={submitter} onSubmitted={onSubmitted} />,
  );

  await user.click(screen.getByRole("button", { name: "新建分析" }));
  await user.upload(
    screen.getByLabelText("Trace 文件"),
    new File([new Uint8Array([1, 2, 3])], "startup.trace"),
  );
  await user.click(screen.getByRole("button", { name: "开始分析" }));

  expect(onSubmitted).toHaveBeenCalledOnce();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("prevents a second submission while an analysis is active", () => {
  render(<NewAnalysisDialog disabled />);

  expect(screen.getByRole("button", { name: "分析进行中" })).toBeDisabled();
});

it("submits the selected remote device id with a device analysis", async () => {
  const user = userEvent.setup();
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
          device_id: "device-ready-1",
          agent_id: "agent-1",
          agent_name: "Mac Agent",
          serial_suffix: "7K2A",
          manufacturer: "UNISOC",
          model: "ums9620",
          android_release: "15",
          api_level: 35,
          connection_type: "usb",
          adb_state: "device",
          state: "ready",
          last_seen_at: "2026-08-05T08:00:00Z",
        },
      ],
    }),
  } as unknown as PerfPilotClient;
  const deviceSubmitter: DeviceSubmitter = vi.fn().mockResolvedValue({
    teamId: "team-1",
    analysis: {
      schema_version: "1.0",
      analysis_id: "analysis-device-1",
      team_id: "team-1",
      analysis_mode: "device",
      device_id: "device-ready-1",
      state: "queued",
      version: 4,
      report_available: false,
      failure: null,
    },
  });
  const onSubmitted = vi.fn();
  render(
    <PerfPilotSessionProvider client={client}>
      <NewAnalysisDialog
        deviceSubmitter={deviceSubmitter}
        onSubmitted={onSubmitted}
      />
    </PerfPilotSessionProvider>,
  );

  await user.click(screen.getByRole("button", { name: "新建分析" }));
  await user.click(screen.getByRole("button", { name: "真机自动测试" }));
  await screen.findByText("UNISOC ums9620");
  const apk = new File([new Uint8Array([1, 2, 3])], "demo.apk", {
    type: "application/vnd.android.package-archive",
  });
  await user.upload(screen.getByLabelText("APK 文件"), apk);
  await user.click(screen.getByRole("button", { name: "开始真机分析" }));

  await waitFor(() => {
    expect(deviceSubmitter).toHaveBeenCalledWith(
      expect.objectContaining({
        teamId: "team-1",
        deviceId: "device-ready-1",
        apk,
      }),
    );
    expect(onSubmitted).toHaveBeenCalledOnce();
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
