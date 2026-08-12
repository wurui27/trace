"use client";

import { useEffect, useMemo, useState } from "react";

import type { PerfPilotClient, SmartPerfettoOriginal } from "../lib/perfpilot-api";

function text(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value;
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const item = value as Record<string, unknown>;
    for (const key of ["conclusion", "summary", "title", "description", "message"]) {
      if (typeof item[key] === "string" && item[key].trim()) return item[key];
    }
  }
  return null;
}

function array(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

export function SmartPerfettoOriginalReport({
  active,
  analysisId,
  teamId,
  client,
}: {
  readonly active: boolean;
  readonly analysisId: string;
  readonly teamId?: string;
  readonly client: PerfPilotClient;
}) {
  const [attempt, setAttempt] = useState(0);
  const [document, setDocument] = useState<SmartPerfettoOriginal | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!active || !teamId || document !== null) return;
    const controller = new AbortController();
    void client.smartPerfettoOriginal(teamId, analysisId, controller.signal).then(
      (value) => setDocument(value),
      () => {
        if (!controller.signal.aborted) setFailed(true);
      },
    );
    return () => controller.abort();
  }, [active, analysisId, attempt, client, document, teamId]);

  const summary = useMemo(() => text(document?.summary), [document]);
  const findings = useMemo(() => array(document?.findings), [document]);
  const verification = useMemo(
    () => text(document?.claimVerificationResult ?? document?.claimSupport ?? document?.verification),
    [document],
  );

  if (!teamId) {
    return <p role="status">当前页面缺少团队上下文，无法读取原始报告。</p>;
  }
  if (failed) {
    return (
      <div role="alert">
        <p>SmartPerfetto 原始报告暂时无法读取。</p>
        <button type="button" onClick={() => { setFailed(false); setAttempt((value) => value + 1); }}>重试</button>
      </div>
    );
  }
  if (document === null) return <p role="status">正在读取 SmartPerfetto 原始报告…</p>;

  return (
    <section className="smartperfetto-original-report" aria-label="SmartPerfetto 原始报告内容">
      <header className="analysis-report-section">
        <h2>原始摘要</h2>
        <p>{summary ?? "原始报告未提供文本摘要。"}</p>
        <a
          href={client.smartPerfettoOriginalDownloadUrl(teamId, analysisId)}
          download={`smartperfetto-${analysisId}.json`}
        >
          下载原始报告
        </a>
      </header>
      <section className="analysis-report-section">
        <h2>原始发现</h2>
        {findings.length ? (
          <ol>{findings.map((finding, index) => <li key={index}>{text(finding) ?? `发现 ${index + 1}`}</li>)}</ol>
        ) : <p>原始报告未列出独立发现。</p>}
      </section>
      <section className="analysis-report-section">
        <h2>验证结果</h2>
        <p>{verification ?? "原始报告未提供独立验证摘要。"}</p>
      </section>
      <details className="smartperfetto-full-json">
        <summary>查看完整 JSON</summary>
        <pre>{JSON.stringify(document, null, 2)}</pre>
      </details>
    </section>
  );
}
