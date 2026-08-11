// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocalLogin } from "../app/components/local-login";
import { PerfPilotSessionProvider } from "../app/components/perfpilot-session-provider";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

afterEach(cleanup);

describe("LocalLogin", () => {
  it("renders a Chinese account/password sign-in form without protected children", () => {
    const client = {
      csrf: async () => "csrf",
      me: async () => {
        throw { code: "authentication_required" };
      },
      devices: async () => ({ schema_version: "1.0", devices: [] }),
    } as unknown as PerfPilotClient;
    render(<PerfPilotSessionProvider client={client}><LocalLogin><div>dashboard child</div></LocalLogin></PerfPilotSessionProvider>);

    return waitFor(() => {
      expect(screen.getByLabelText("账号")).toBeTruthy();
      expect(screen.getByLabelText("密码")).toBeTruthy();
      expect(screen.queryByText("dashboard child")).toBeNull();
    });
  });

  it("lets user01 change the required initial password before showing the dashboard", async () => {
    const team = {
      id: "81000000-0000-4000-8000-000000000001",
      name: "user01 local team",
    };
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
      devices: vi.fn().mockResolvedValue({ schema_version: "1.0", devices: [] }),
    } as unknown as PerfPilotClient;
    const user = userEvent.setup();
    render(<PerfPilotSessionProvider client={client}><LocalLogin><div>dashboard child</div></LocalLogin></PerfPilotSessionProvider>);

    await user.type(await screen.findByLabelText("账号"), "user01");
    await user.type(screen.getByLabelText("密码"), "initial user password");
    await user.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByRole("heading", { name: "修改初始密码" })).toBeTruthy();
    expect(screen.queryByText("dashboard child")).toBeNull();
    await user.type(screen.getByLabelText("当前密码"), "initial user password");
    await user.type(screen.getByLabelText("新密码"), "changed user password");
    await user.type(screen.getByLabelText("确认新密码"), "changed user password");
    await user.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(() => expect(screen.getByText("dashboard child")).toBeTruthy());
    expect(client.devices).toHaveBeenCalled();
  });
});
