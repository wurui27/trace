// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AgentManagement } from "../app/components/agent-management";
import { PerfPilotSessionProvider } from "../app/components/perfpilot-session-provider";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

afterEach(cleanup);

it("lists Agents and explains zero-touch automatic enrollment", async () => {
  const agent = {
    agent_id: "agent-1",
    name: "Ubuntu 实验室",
    platform: "linux",
    agent_version: "1.2.3",
    hostname: "rivotek",
    os_version: "Ubuntu 24.04",
    state: "online",
    last_heartbeat_at: "2026-08-05T08:00:00Z",
    created_at: "2026-08-05T07:00:00Z",
    updated_at: "2026-08-05T08:00:00Z",
  };
  const client = {
    csrf: vi.fn().mockResolvedValue("csrf"),
    me: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      memberships: [{ team: { id: "team-1", name: "Ray" }, role: "owner" }],
    }),
    devices: vi.fn().mockResolvedValue({ schema_version: "1.0", devices: [] }),
    agents: vi.fn().mockResolvedValue({ schema_version: "1.0", agents: [agent] }),
  } as unknown as PerfPilotClient;

  render(
    <PerfPilotSessionProvider client={client}>
      <AgentManagement />
    </PerfPilotSessionProvider>,
  );

  expect(await screen.findByText("Ubuntu 实验室")).toBeInTheDocument();
  expect(screen.getByText("自动接入已开启")).toBeInTheDocument();
  expect(screen.getByText(/安装并启动 Agent 后/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Agent 名称")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "生成注册码" })).not.toBeInTheDocument();
});
