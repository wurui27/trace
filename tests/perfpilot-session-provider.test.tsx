// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PerfPilotSessionProvider } from "../app/components/perfpilot-session-provider";
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
});
