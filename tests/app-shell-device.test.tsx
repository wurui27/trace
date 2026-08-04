// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { AppShell } from "../app/components/app-shell";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("replaces the Pixel demo card with the device reported by the local ADB API", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>().mockResolvedValue(
      Response.json({
        schema_version: "1.0",
        state: "connected",
        device: {
          serial: "0123456789ABCDEF",
          manufacturer: "UNISOC",
          model: "uis7870_2h10_car_c200_6",
          name: "UNISOC uis7870_2h10_car_c200_6",
          os: "Android 13",
          api_level: 33,
        },
      }),
    ),
  );
  render(
    <AppShell activeItem="overview">
      <p>内容</p>
    </AppShell>,
  );

  expect(
    await screen.findByText("UNISOC uis7870_2h10_car_c200_6"),
  ).toBeInTheDocument();
  expect(screen.getByText("ADB 已连接")).toBeInTheDocument();
  expect(screen.getByText("Android 13")).toBeInTheDocument();
  expect(screen.queryByText("Pixel 8")).not.toBeInTheDocument();
  expect(screen.getByText("尚未选择应用")).toBeInTheDocument();
});
