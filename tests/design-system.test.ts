// @vitest-environment node

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  new URL("../app/globals.css", import.meta.url),
  "utf8",
);
const executableCss = css.replace(/\/\*[\s\S]*?\*\//g, "");

function escapePattern(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

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
    [".sidebar", String.raw`position\s*:\s*fixed\s*;`],
    [".top-bar", String.raw`position\s*:\s*sticky\s*;`],
    [".conclusion-hero", String.raw`background\s*:\s*linear-gradient\(`],
    [".core-overview-panel", String.raw`display\s*:\s*grid\s*;`],
    [".new-analysis-dialog", String.raw`width\s*:\s*min\(\s*670px`],
    [":focus-visible", String.raw`outline\s*:\s*2px\s+solid`],
    [".skip-link", String.raw`position\s*:\s*fixed\s*;`],
  ])(
    "gives the %s contract selector a required declaration",
    (selector, declaration) => {
      expect(executableCss).toMatch(
        new RegExp(
          `${escapePattern(selector)}[^{}]*\\{[^}]*${declaration}`,
          "i",
        ),
      );
    },
  );

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

  it("keeps final reports printable and expands their evidence", () => {
    const printBlock = css.match(/@media\s+print\s*\{([\s\S]*?)\n\}\n\n\/\*/)?.[1] ?? "";

    expect(printBlock).toMatch(/\.final-report-download[\s\S]*display\s*:\s*none\s*!important/i);
    expect(printBlock).toMatch(/\.final-report-print-unavailable[\s\S]*display\s*:\s*none\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-report-partial button[\s\S]*display\s*:\s*none\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-report-metric-details\s*>\s*:not\(summary\)[\s\S]*display\s*:\s*block\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-memory-evidence-details\s*>\s*:not\(summary\)[\s\S]*display\s*:\s*block\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-report-evidence details\s*>\s*:not\(summary\)[\s\S]*display\s*:\s*block\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-report-provenance\s*>\s*:not\(summary\)[\s\S]*display\s*:\s*block\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-report-section[\s\S]*break-inside\s*:\s*avoid/i);
    expect(printBlock).toMatch(/\.analysis-reference-list a[\s\S]*text-decoration\s*:\s*none/i);
  });
});
