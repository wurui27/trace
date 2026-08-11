import { describe, expect, it, vi } from "vitest";

import { printAnalysisReport, supportsReportPrint } from "../app/lib/report-print";

describe("report printing", () => {
  it("prints with a report-specific title and restores the document title", () => {
    const target = {
      document: { title: "PerfPilot", documentElement: { dataset: {} as Record<string, string> } },
      print: vi.fn(),
    };

    target.print.mockImplementation(() => {
      expect(target.document.title).toBe("PerfPilot-analysis-42");
      expect(target.document.documentElement.dataset.reportPrinting).toBe("true");
    });

    expect(printAnalysisReport("analysis/42", target)).toBe(true);
    expect(target.print).toHaveBeenCalledOnce();
    expect(target.document.title).toBe("PerfPilot");
    expect(target.document.documentElement.dataset.reportPrinting).toBeUndefined();
  });

  it("returns false and restores the title when printing throws", () => {
    const target = {
      document: { title: "PerfPilot" },
      print: vi.fn(() => {
        throw new Error("blocked");
      }),
    };

    expect(printAnalysisReport("analysis-42", target)).toBe(false);
    expect(target.document.title).toBe("PerfPilot");
  });

  it("returns false when browser printing is unavailable", () => {
    expect(printAnalysisReport("analysis-42", null)).toBe(false);
  });

  it("sanitizes unsafe and empty IDs before printing", () => {
    let printedTitle = "";
    const target = {
      document: { title: "PerfPilot" },
      print: vi.fn(() => {
        printedTitle = target.document.title;
      }),
    };
    const unsafe = `${"?".repeat(130)}report / safe`;

    printAnalysisReport(unsafe, target);

    expect(printedTitle).toMatch(/^PerfPilot-[A-Za-z0-9._-]{1,128}$/);
    expect(printedTitle).toHaveLength("PerfPilot-".length + 128);
    printAnalysisReport("", target);
    expect(printedTitle).toBe("PerfPilot-report");
    expect(target.document.title).toBe("PerfPilot");
    expect(supportsReportPrint({ document: { title: "x" }, print: vi.fn() })).toBe(true);
  });
});
