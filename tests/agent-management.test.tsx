// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { AgentManagement } from "../app/components/agent-management";
import { PerfPilotSessionProvider } from "../app/components/perfpilot-session-provider";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

afterEach(cleanup);

it("lets the user open one automatic enrollment slot and delete an Agent", async () => {
  const user = userEvent.setup();
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
    agentEnrollment: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      enrollment: null,
    }),
    openAgentEnrollment: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      enrollment: {
        enrollment_id: "98000000-0000-4000-8000-000000000001",
        name: "新测试电脑",
        expires_at: "2026-08-19T11:10:00Z",
      },
    }),
    revokeAgent: vi.fn().mockResolvedValue({ ...agent, state: "revoked" }),
  } as unknown as PerfPilotClient;

  render(
    <PerfPilotSessionProvider client={client}>
      <AgentManagement />
    </PerfPilotSessionProvider>,
  );

  expect(await screen.findByText("Ubuntu 实验室")).toBeInTheDocument();
  await user.type(screen.getByLabelText("Agent 名称"), "新测试电脑");
  await user.click(screen.getByRole("button", { name: "添加 Agent" }));
  expect(client.openAgentEnrollment).toHaveBeenCalledWith(
    "team-1",
    "新测试电脑",
    expect.any(AbortSignal),
  );
  expect(await screen.findByText(/等待 Agent 自动连接/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "生成注册码" })).not.toBeInTheDocument();

  vi.spyOn(window, "confirm").mockReturnValue(true);
  await user.click(screen.getByRole("button", { name: "删除 Ubuntu 实验室" }));
  expect(client.revokeAgent).toHaveBeenCalledWith(
    "team-1",
    "agent-1",
    expect.any(AbortSignal),
  );
  expect(screen.queryByText("Ubuntu 实验室")).not.toBeInTheDocument();
});
