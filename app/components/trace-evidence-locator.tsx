"use client";

import { useEffect, useMemo, useState } from "react";

import {
  createAnalysisLoader,
  type AnalysisDetailSnapshot,
  type AnalysisLoader,
} from "./analysis-progress";

const defaultLoader = createAnalysisLoader();

export function TraceEvidenceLocator({
  analysisId,
  evidenceId,
  loader = defaultLoader,
}: {
  readonly analysisId: string;
  readonly evidenceId: string;
  readonly loader?: AnalysisLoader;
}) {
  const [snapshot, setSnapshot] = useState<AnalysisDetailSnapshot | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void loader(analysisId, controller.signal, setSnapshot).catch(() => {
      if (!controller.signal.aborted) setFailed(true);
    });
    return () => controller.abort();
  }, [analysisId, loader]);

  const evidence = useMemo(() => {
    const report = snapshot?.report;
    return report?.schema_version === "1.3"
      ? report.workbench.evidence.find((item) => item.evidence_id === evidenceId) ?? null
      : null;
  }, [evidenceId, snapshot]);

  if (failed) return <main className="finding-empty" role="alert">证据暂时无法读取。</main>;
  if (snapshot === null) return <main className="finding-empty" role="status">正在读取证据…</main>;
  if (evidence === null) return <main className="finding-empty" role="alert">证据不存在。</main>;
  if (evidence.locator === null) return <main className="finding-empty" role="status">该证据没有可验证的 Trace 时间区间。</main>;

  const locator = JSON.stringify({
    analysis_id: analysisId,
    evidence_id: evidenceId,
    ...evidence.locator,
  });
  return (
    <main className="trace-evidence-locator">
      <p className="section-label">VALIDATED TRACE EVIDENCE</p>
      <h1>{evidence.summary}</h1>
      <p>以下定位参数来自已验证报告，不接受外部 URL 或本地文件路径。</p>
      <dl>
        <div><dt>时间区间</dt><dd>{evidence.locator.start_ns} – {evidence.locator.end_ns} ns</dd></div>
        <div><dt>进程</dt><dd>{evidence.locator.process ?? "未识别"}</dd></div>
        <div><dt>线程</dt><dd>{evidence.locator.thread ?? "未识别"}</dd></div>
        <div><dt>轨道</dt><dd>{evidence.locator.track ?? "未识别"}</dd></div>
        <div><dt>切片</dt><dd>{evidence.locator.slice ?? "未识别"}</dd></div>
        <div><dt>查询</dt><dd>{evidence.locator.query_id ?? "未提供"}</dd></div>
      </dl>
      <button type="button" onClick={() => void navigator.clipboard?.writeText(locator)}>
        复制定位参数
      </button>
    </main>
  );
}
