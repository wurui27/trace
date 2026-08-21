// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AppShell } from "../app/components/app-shell";
import { PerfPilotSessionProvider } from "../app/components/perfpilot-session-provider";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows real remote devices without rendering a demo Pixel or raw serial", async () => {
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
          agent_name: "Ubuntu 实验室",
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
  render(
    <PerfPilotSessionProvider client={client}>
      <AppShell activeItem="overview">
        <p>内容</p>
      </AppShell>
    </PerfPilotSessionProvider>,
  );

  expect(await screen.findByText("UNISOC ums9620")).toBeInTheDocument();
  expect(screen.getByText("设备已就绪")).toBeInTheDocument();
  expect(screen.getByText("Android 15")).toBeInTheDocument();
  expect(screen.queryByText("Pixel 8")).not.toBeInTheDocument();
  expect(screen.queryByText("R3CN30SECRET7K2A")).not.toBeInTheDocument();
  expect(screen.getByText("尚未选择应用")).toBeInTheDocument();
  const navigation = screen.getByRole("navigation", { name: "主导航" });
  expect(within(navigation).getByRole("link", { name: "测试" })).toBeVisible();
  expect(within(navigation).getByRole("link", { name: "场景" })).toBeVisible();
  expect(
    within(navigation).queryByRole("link", { name: "问题" }),
  ).not.toBeInTheDocument();
  expect(
    within(navigation).queryByRole("link", { name: "对比" }),
  ).not.toBeInTheDocument();
});
