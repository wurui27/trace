// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";

import { LocalLogin } from "../app/components/local-login";
import {
  PerfPilotSessionProvider,
  usePerfPilotSession,
} from "../app/components/perfpilot-session-provider";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

afterEach(cleanup);

function PollingDashboard() {
  const { client, status, team } = usePerfPilotSession();

  useEffect(() => {
    if (status !== "ready" || team === null) return;
    void client.devices(team.id).catch(() => undefined);
    void client.analyses(team.id).catch(() => undefined);
    void client.activeAnalyses(team.id).catch(() => undefined);
  }, [client, status, team]);

  return <div>dashboard child</div>;
}

describe("LocalLogin", () => {
  it("renders the evidence-focused split login shell while signed out", async () => {
    const client = {
      csrf: vi.fn().mockResolvedValue("csrf"),
      me: vi.fn().mockRejectedValue({ code: "authentication_required" }),
    } as unknown as PerfPilotClient;

    render(
      <PerfPilotSessionProvider client={client}>
        <LocalLogin>
          <div>dashboard child</div>
        </LocalLogin>
      </PerfPilotSessionProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "从 Trace 直达源码优化" }),
    ).toBeTruthy();
    expect(screen.getByText("性能证据、问题定位与复测建议统一呈现。"))
      .toBeTruthy();
    expect(screen.getByRole("heading", { name: "欢迎回来" })).toBeTruthy();
    expect(screen.getByText("使用管理员分配的账号登录。"))
      .toBeTruthy();
  });

  it("keeps all dashboard polling unmounted while signed out", () => {
    const client = {
      csrf: vi.fn().mockResolvedValue("csrf"),
      me: vi.fn().mockRejectedValue({ code: "authentication_required" }),
      devices: vi.fn().mockResolvedValue({ schema_version: "1.0", devices: [] }),
      analyses: vi.fn().mockResolvedValue({ schema_version: "1.0", analyses: [] }),
      activeAnalyses: vi.fn().mockResolvedValue({ schema_version: "1.0", analyses: [] }),
    } as unknown as PerfPilotClient;
    render(<PerfPilotSessionProvider client={client}><LocalLogin><PollingDashboard /></LocalLogin></PerfPilotSessionProvider>);

    return waitFor(() => {
      expect(screen.getByLabelText("账号")).toBeTruthy();
      expect(screen.getByLabelText("密码")).toBeTruthy();
      expect(screen.queryByText("dashboard child")).toBeNull();
      expect(client.devices).not.toHaveBeenCalled();
      expect(client.analyses).not.toHaveBeenCalled();
      expect(client.activeAnalyses).not.toHaveBeenCalled();
    });
  });

  it("lets user01 change the required initial password before showing the dashboard", async () => {
    const team = {
      id: "81000000-0000-4000-8000-000000000001",
      name: "user01 local team",
    };
    const devices = vi.fn().mockResolvedValue({ schema_version: "1.0", devices: [] });
    const analyses = vi.fn().mockResolvedValue({ schema_version: "1.0", analyses: [] });
    const activeAnalyses = vi.fn().mockResolvedValue({ schema_version: "1.0", analyses: [] });
    const client = {
      csrf: vi.fn().mockResolvedValue("csrf"),
      login: vi.fn().mockResolvedValue("session-csrf"),
      changePassword: vi.fn().mockResolvedValue("rotated-csrf"),
      me: vi
        .fn()
        .mockRejectedValueOnce({ code: "authentication_required" })
        .mockResolvedValueOnce({
          schema_version: "1.0",
          user: {
            id: "80000000-0000-4000-8000-000000000001",
            username: "user01",
            is_platform_admin: false,
            must_change_password: true,
          },
          memberships: [{ id: team.id, team, role: "owner" }],
        })
        .mockResolvedValueOnce({
          schema_version: "1.0",
          user: {
            id: "80000000-0000-4000-8000-000000000001",
            username: "user01",
            is_platform_admin: false,
            must_change_password: false,
          },
          memberships: [{ id: team.id, team, role: "owner" }],
        }),
      devices,
      analyses,
      activeAnalyses,
    } as unknown as PerfPilotClient;
    const user = userEvent.setup();
    render(<PerfPilotSessionProvider client={client}><LocalLogin><PollingDashboard /></LocalLogin></PerfPilotSessionProvider>);

    await user.type(await screen.findByLabelText("账号"), "user01");
    await user.type(screen.getByLabelText("密码"), "initial user password");
    await user.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByRole("heading", { name: "修改初始密码" })).toBeTruthy();
    expect(screen.queryByText("dashboard child")).toBeNull();
    expect(client.devices).not.toHaveBeenCalled();
    expect(client.analyses).not.toHaveBeenCalled();
    expect(client.activeAnalyses).not.toHaveBeenCalled();
    await user.type(screen.getByLabelText("当前密码"), "initial user password");
    await user.type(screen.getByLabelText("新密码"), "changed user password");
    await user.type(screen.getByLabelText("确认新密码"), "changed user password");
    await user.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(() => expect(screen.getByText("dashboard child")).toBeTruthy());
    await waitFor(() => {
      expect(client.devices).toHaveBeenCalled();
      expect(client.analyses).toHaveBeenCalled();
      expect(client.activeAnalyses).toHaveBeenCalled();
    });
    for (const calls of [
      devices.mock.calls,
      analyses.mock.calls,
      activeAnalyses.mock.calls,
    ]) {
      expect(calls.every(([teamId]) => teamId === team.id)).toBe(true);
    }
  });
});
