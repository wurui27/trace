interface PrintTarget {
  document: {
    title: string;
    documentElement?: { dataset: Record<string, string | undefined> };
  };
  print: () => void;
}

function browserTarget(): PrintTarget | null {
  if (typeof window === "undefined" || typeof window.print !== "function") {
    return null;
  }

  return window;
}

export function supportsReportPrint(target = browserTarget()): boolean {
  return target !== null;
}

export function printAnalysisReport(
  analysisId: string,
  target = browserTarget(),
): boolean {
  if (target === null) return false;

  const safeId = analysisId.replace(/[^A-Za-z0-9._-]/g, "-").slice(0, 128) || "report";
  const previousTitle = target.document.title;
  const dataset = target.document.documentElement?.dataset;
  const previousPrinting = dataset?.reportPrinting;

  try {
    target.document.title = `PerfPilot-${safeId}`;
    if (dataset) dataset.reportPrinting = "true";
    target.print();
    return true;
  } catch {
    return false;
  } finally {
    target.document.title = previousTitle;
    if (dataset) {
      if (previousPrinting === undefined) delete dataset.reportPrinting;
      else dataset.reportPrinting = previousPrinting;
    }
  }
}
