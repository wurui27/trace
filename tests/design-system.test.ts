// @vitest-environment node

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  new URL("../app/globals.css", import.meta.url),
  "utf8",
);

describe("Focus Light design system", () => {
  it.each([
    ["primary", "#2d6df6"],
    ["page", "#f5f7fa"],
    ["nav", "#101d2d"],
    ["text", "#132238"],
    ["border", "#e1e7ee"],
    ["sidebar-width", "212px"],
    ["topbar-height", "66px"],
  ])("defines the exact --%s token", (name, value) => {
    expect(css).toMatch(
      new RegExp(`--${name}\\s*:\\s*${value.replace("#", "\\#")}\\s*;`, "i"),
    );
  });

  it.each([
    ".sidebar",
    ".top-bar",
    ".conclusion-hero",
    ".core-overview-panel",
    ".new-analysis-dialog",
    ":focus-visible",
    ".skip-link",
  ])("styles the %s contract selector", (selector) => {
    expect(css).toContain(selector);
  });

  it("defines the two responsive layout breakpoints", () => {
    expect(css).toMatch(/@media\s*\(\s*max-width\s*:\s*1050px\s*\)/i);
    expect(css).toMatch(/@media\s*\(\s*max-width\s*:\s*780px\s*\)/i);
  });

  it("keeps a 64px fixed sidebar rail at the 780px breakpoint", () => {
    expect(css).toMatch(
      /@media\s*\(\s*max-width\s*:\s*780px\s*\)[\s\S]*?\.sidebar\s*\{[^}]*\bwidth\s*:\s*64px\s*;/i,
    );
  });

  it("provides a reduced-motion mode", () => {
    expect(css).toMatch(
      /@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)/i,
    );
  });

  it("does not restore an automatic dark color-scheme block", () => {
    expect(css).not.toMatch(
      /@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)/i,
    );
  });
});
