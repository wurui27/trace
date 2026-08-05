// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
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
