"use client";

import { useEffect, useMemo, useState } from "react";

import type {
  PerfPilotClient,
  SmartPerfettoOriginal,
  SmartPerfettoScenarioOriginal,
} from "../lib/perfpilot-api";

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

function scenarioCollection(
  value: SmartPerfettoOriginal | null,
): readonly SmartPerfettoScenarioOriginal[] | null {
  if (
    value === null ||
    value.mode !== "scenario_collection" ||
    !Array.isArray(value.reports)
  ) {
    return null;
  }
  return value.reports as readonly SmartPerfettoScenarioOriginal[];
}

function OriginalDocument({
  analysisId,
  client,
  document,
  scenario,
  teamId,
  title,
}: {
  readonly analysisId: string;
  readonly client: PerfPilotClient;
  readonly document: Readonly<Record<string, unknown>>;
  readonly scenario?: "startup" | "scroll";
  readonly teamId: string;
  readonly title?: string;
}) {
  const summary = text(document.summary);
  const findings = array(document.findings);
  const verification = text(
    document.claimVerificationResult ?? document.claimSupport ?? document.verification,
  );
  return (
    <section className="smartperfetto-original-document">
      {title ? <h2>{title}</h2> : null}
      <header className="analysis-report-section">
        <h3>原始摘要</h3>
        <p>{summary ?? "原始报告未提供文本摘要。"}</p>
        <a
          href={client.smartPerfettoOriginalDownloadUrl(teamId, analysisId, scenario)}
          download={`smartperfetto-${analysisId}${scenario ? `-${scenario}` : ""}.json`}
        >
          下载原始报告
        </a>
      </header>
      <section className="analysis-report-section">
        <h3>原始发现</h3>
        {findings.length ? (
          <ol>{findings.map((finding, index) => <li key={index}>{text(finding) ?? `发现 ${index + 1}`}</li>)}</ol>
        ) : <p>原始报告未列出独立发现。</p>}
      </section>
      <section className="analysis-report-section">
        <h3>验证结果</h3>
        <p>{verification ?? "原始报告未提供独立验证摘要。"}</p>
      </section>
      <details className="smartperfetto-full-json">
        <summary>查看完整 JSON</summary>
        <pre>{JSON.stringify(document, null, 2)}</pre>
      </details>
    </section>
  );
}

export function SmartPerfettoOriginalReport({
  active,
  analysisId,
  teamId,
  client,
  preloadedDocument = null,
  preloadFailed = false,
}: {
  readonly active: boolean;
  readonly analysisId: string;
  readonly teamId?: string;
  readonly client: PerfPilotClient;
  readonly preloadedDocument?: SmartPerfettoOriginal | null;
  readonly preloadFailed?: boolean;
}) {
  const [attempt, setAttempt] = useState(0);
  const [document, setDocument] = useState<SmartPerfettoOriginal | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!active || !teamId || document !== null || preloadedDocument !== null) return;
    const controller = new AbortController();
    void client.smartPerfettoOriginal(teamId, analysisId, controller.signal).then(
      (value) => setDocument(value),
      () => {
        if (!controller.signal.aborted) setFailed(true);
      },
    );
    return () => controller.abort();
  }, [active, analysisId, attempt, client, document, preloadedDocument, teamId]);

  const visibleDocument = preloadedDocument ?? document;
  const collection = useMemo(() => scenarioCollection(visibleDocument), [visibleDocument]);

  if (!teamId) {
    return <p role="status">当前页面缺少团队上下文，无法读取原始报告。</p>;
  }
  if (failed || preloadFailed) {
    return (
      <div role="alert">
        <p>SmartPerfetto 原始报告暂时无法读取。</p>
        <button type="button" onClick={() => { setFailed(false); setAttempt((value) => value + 1); }}>重试</button>
      </div>
    );
  }
  if (visibleDocument === null) return <p role="status">正在读取 SmartPerfetto 原始报告…</p>;

  if (collection !== null) {
    return (
      <section className="smartperfetto-original-report" aria-label="SmartPerfetto 原始报告内容">
        {collection.map((item) => (
          <OriginalDocument
            key={item.scenario_type}
            analysisId={analysisId}
            client={client}
            document={item.document}
            scenario={item.scenario_type}
            teamId={teamId}
            title={`${item.label}原始报告`}
          />
        ))}
      </section>
    );
  }

  return (
    <section className="smartperfetto-original-report" aria-label="SmartPerfetto 原始报告内容">
      <OriginalDocument
        analysisId={analysisId}
        client={client}
        document={visibleDocument}
        teamId={teamId}
      />
    </section>
  );
}
