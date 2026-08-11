// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  PerfPilotSessionProvider,
  usePerfPilotSession,
} from "../app/components/perfpilot-session-provider";
import { LocalLogin } from "../app/components/local-login";
import type { PerfPilotClient } from "../app/lib/perfpilot-api";

describe("PerfPilotSessionProvider", () => {
  it("keeps dashboard children hidden and does not poll devices while signed out", async () => {
    const client = {
      csrf: vi.fn().mockResolvedValue("csrf"),
      me: vi.fn().mockRejectedValue({ code: "authentication_required" }),
      devices: vi.fn(),
    } as unknown as PerfPilotClient;

    render(
      <PerfPilotSessionProvider client={client}>
        <LocalLogin><div>dashboard child</div></LocalLogin>
      </PerfPilotSessionProvider>,
    );

    await waitFor(() => expect(screen.queryByText("dashboard child")).toBeNull());
    expect(client.devices).not.toHaveBeenCalled();
  });

  it("clears the ready session when the client reports a runtime authentication failure", async () => {
    const team = { id: "81000000-0000-4000-8000-000000000001", name: "user01" };
    let notify: (() => void) | undefined;
    const client = {
      csrf: vi.fn().mockResolvedValue("csrf"),
      me: vi.fn().mockResolvedValue({
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
      subscribeAuthFailures: vi.fn((listener: () => void) => {
        notify = listener;
        return () => undefined;
      }),
    } as unknown as PerfPilotClient;

    function ReadyChild() {
      const session = usePerfPilotSession();
      return <div>{session.team?.id ?? "no-team"}</div>;
    }

    render(
      <PerfPilotSessionProvider client={client}>
        <LocalLogin><ReadyChild /></LocalLogin>
      </PerfPilotSessionProvider>,
    );

    expect(await screen.findByText(team.id)).toBeTruthy();
    expect(client.subscribeAuthFailures).toHaveBeenCalledTimes(1);
    notify?.();
    await waitFor(() => expect(screen.getByLabelText("账号")).toBeTruthy());
    expect(screen.queryByText(team.id)).toBeNull();
  });
});
