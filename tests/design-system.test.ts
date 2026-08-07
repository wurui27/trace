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

    expect(executableCss.match(/@media\s+print\s*\{/gi)).toHaveLength(1);

    expect(executableCss).toMatch(
      /\.final-report-actions\s*\{[^}]*display\s*:\s*flex[^}]*align-items\s*:\s*center[^}]*gap\s*:\s*10px/i,
    );
    expect(executableCss).toMatch(
      /\.final-report-download\s*\{[^}]*display\s*:\s*inline-flex[^}]*border\s*:\s*1px\s+solid\s+var\(--primary\)[^}]*background\s*:\s*var\(--primary\)[^}]*color\s*:\s*var\(--surface\)/i,
    );
    expect(executableCss).toMatch(
      /\.final-report-download:hover:not\(:disabled\)\s*\{[^}]*border-color\s*:\s*var\(--primary-dark\)[^}]*background\s*:\s*var\(--primary-dark\)/i,
    );
    expect(executableCss).toMatch(
      /\.final-report-download:disabled\s*\{[^}]*border-color\s*:\s*var\(--border-strong\)[^}]*background\s*:\s*var\(--border-strong\)[^}]*color\s*:\s*var\(--text-muted\)/i,
    );
    expect(executableCss).toMatch(
      /\.final-report-download svg\s*\{[^}]*width\s*:\s*15px[^}]*height\s*:\s*15px[^}]*stroke-width\s*:\s*1\.9/i,
    );
    expect(executableCss).toMatch(
      /\.final-report-print-unavailable\s*\{[^}]*color\s*:\s*var\(--text-muted\)[^}]*font-size\s*:\s*11px[^}]*line-height\s*:\s*1\.4/i,
    );
    expect(executableCss).toMatch(
      /button:focus-visible[\s\S]*?\{[^}]*outline\s*:\s*2px\s+solid\s+var\(--primary\)/i,
    );

    expect(printBlock).toMatch(
      /\.final-report-topbar\s*,\s*\.final-report-download\s*,\s*\.final-report-print-unavailable\s*,\s*\.analysis-report-partial button\s*\{[^}]*display\s*:\s*none\s*!important/i,
    );
    expect(printBlock).toMatch(/\.analysis-report-metric-details\s*>\s*:not\(summary\)[\s\S]*display\s*:\s*block\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-memory-evidence-details\s*>\s*:not\(summary\)[\s\S]*display\s*:\s*block\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-report-evidence details\s*>\s*:not\(summary\)[\s\S]*display\s*:\s*block\s*!important/i);
    expect(printBlock).toMatch(/\.analysis-report-provenance\s*>\s*:not\(summary\)[\s\S]*display\s*:\s*block\s*!important/i);
    expect(printBlock).toMatch(
      /\.analysis-report-metric-details\s*>\s*summary\s*,\s*\.analysis-memory-evidence-details\s*>\s*summary\s*,\s*\.analysis-report-evidence summary\s*,\s*\.analysis-report-provenance summary\s*\{[^}]*list-style\s*:\s*none[^}]*pointer-events\s*:\s*none/i,
    );
    expect(printBlock).toMatch(
      /\.analysis-report-metric-details\s*>\s*summary::-webkit-details-marker\s*,\s*\.analysis-memory-evidence-details\s*>\s*summary::-webkit-details-marker\s*,\s*\.analysis-report-evidence summary::-webkit-details-marker\s*,\s*\.analysis-report-provenance summary::-webkit-details-marker\s*\{[^}]*display\s*:\s*none/i,
    );
    expect(printBlock).toMatch(
      /\.analysis-report-section\s*,\s*\.analysis-report-findings\s*>\s*li\s*,\s*\.analysis-recommendation-list\s*>\s*li\s*,\s*\.analysis-retest-list\s*>\s*li\s*,\s*\.analysis-report-evidence\s*>\s*div\s*\{[^}]*break-inside\s*:\s*avoid[^}]*page-break-inside\s*:\s*avoid/i,
    );
    expect(printBlock).toMatch(
      /\.analysis-reference-list a\s*\{[^}]*color\s*:\s*inherit[^}]*text-decoration\s*:\s*none/i,
    );
    expect(printBlock).toMatch(
      /\.final-report-page\s*,\s*body\s*\{[^}]*background\s*:\s*var\(--surface\)/i,
    );
    expect(printBlock).toMatch(
      /\.final-report-main\s*\{[^}]*width\s*:\s*100%[^}]*padding\s*:\s*0/i,
    );
    expect(printBlock).toMatch(
      /\.final-report-masthead\s*,\s*\.analysis-report-card\s*\{[^}]*border-color\s*:\s*var\(--border-strong\)[^}]*box-shadow\s*:\s*none/i,
    );
  });
});
