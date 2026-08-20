// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SystemHealthBanner } from "../app/components/system-health-banner";
import type { AnalysisHealthResponse } from "../app/lib/perfpilot-api";


afterEach(cleanup);

const degraded: AnalysisHealthResponse = {
  schema_version: "1.0",
  state: "degraded",
  capabilities: [
    {
      name: "smartperfetto",
      state: "unavailable",
      message: "SmartPerfetto 暂不可用",
      last_checked_at: "2026-08-20T08:00:00+00:00",
    },
    {
      name: "storage",
      state: "healthy",
      message: "本地存储可用",
      last_checked_at: "2026-08-20T08:00:00+00:00",
    },
  ],
};

describe("SystemHealthBanner", () => {
  it("does not occupy the page when all capabilities are healthy", () => {
    const { container } = render(
      <SystemHealthBanner health={{ ...degraded, state: "healthy", capabilities: [] }} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("explains degraded capabilities without blocking the page", () => {
    render(<SystemHealthBanner health={degraded} />);

    expect(screen.getByRole("status")).toHaveTextContent("部分分析能力暂不可用");
    expect(screen.getByText("SmartPerfetto 暂不可用")).toBeInTheDocument();
  });

  it("copies only a safe closed diagnostic summary", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<SystemHealthBanner health={degraded} />);

    await user.click(screen.getByRole("button", { name: "复制诊断信息" }));

    const copied = String(writeText.mock.calls[0]?.[0]);
    expect(copied).toContain("smartperfetto");
    expect(copied).not.toMatch(/token|\/Users\/|source[_ -]?content/i);
  });
});
