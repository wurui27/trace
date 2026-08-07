interface PrintTarget {
  document: { title: string };
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

  try {
    target.document.title = `PerfPilot-${safeId}`;
    target.print();
    return true;
  } catch {
    return false;
  } finally {
    target.document.title = previousTitle;
  }
}
