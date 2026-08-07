import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  new URL("../app/components/full-analysis-report.tsx", import.meta.url),
  "utf8",
);

describe("final report navigation", () => {
  it("disables unsupported vinext RSC prefetching on report navigation links", () => {
    expect(source).toMatch(
      /<Link\s+className="final-report-back"\s+href=\{`\/analyses\/\$\{analysisId\}`\}\s+prefetch=\{false\}>/,
    );
    expect(source).toMatch(
      /<Link\s+className="analysis-page-brand"\s+href="\/"\s+prefetch=\{false\}\s+aria-label="返回 PerfPilot 首页"\s*>/,
    );
  });
});
