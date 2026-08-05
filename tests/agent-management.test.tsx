// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { AgentManagement } from "../app/components/agent-management";
import { PerfPilotSessionProvider } from "../app/components/perfpilot-session-provider";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

afterEach(cleanup);

it("lists Agents and shows each registration code only after it is generated", async () => {
  const user = userEvent.setup();
  const registrationCode = `ppreg_${"A".repeat(43)}`;
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
    createAgentRegistrationCode: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      agent_id: "agent-2",
      registration_code: registrationCode,
      expires_at: "2026-08-05T08:10:00Z",
    }),
  } as unknown as PerfPilotClient;

  render(
    <PerfPilotSessionProvider client={client}>
      <AgentManagement />
    </PerfPilotSessionProvider>,
  );

  expect(await screen.findByText("Ubuntu 实验室")).toBeInTheDocument();
  expect(screen.queryByText(registrationCode)).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Agent 名称"), "Mac Agent");
  await user.click(screen.getByRole("button", { name: "生成注册码" }));

  expect(client.createAgentRegistrationCode).toHaveBeenCalledWith(
    "team-1",
    "Mac Agent",
    expect.any(AbortSignal),
  );
  expect(await screen.findByText(registrationCode)).toBeInTheDocument();
  expect(screen.getByText(/有效期至/)).toBeInTheDocument();
});
