"use client";

import { useState } from "react";

import type { PerfPilotClient } from "../lib/perfpilot-api";

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
  const [failed, setFailed] = useState(false);

  if (!teamId) {
    return <p role="status">当前页面缺少团队上下文，无法读取原始报告。</p>;
  }
  if (failed) {
    return (
      <div role="alert">
        <p>SmartPerfetto 原始 HTML 暂时无法读取。</p>
        <button
          type="button"
          onClick={() => {
            setFailed(false);
            setAttempt((value) => value + 1);
          }}
        >
          重试
        </button>
      </div>
    );
  }
  if (!active) return null;

  const source = client.smartPerfettoOriginalUrl(teamId, analysisId);
  return (
    <section className="smartperfetto-original-report" aria-label="SmartPerfetto 原始报告内容">
      <header className="analysis-report-section smartperfetto-original-actions">
        <div>
          <h2>SmartPerfetto 原始 HTML</h2>
          <p>以下内容由 SmartPerfetto 原样生成，PerfPilot 不改写其中任何文字或结构。</p>
        </div>
        <a
          href={client.smartPerfettoOriginalDownloadUrl(teamId, analysisId)}
          download={`smartperfetto-${analysisId}.html`}
        >
          下载原始 HTML
        </a>
      </header>
      <iframe
        key={attempt}
        className="smartperfetto-original-frame"
        onError={() => setFailed(true)}
        sandbox="allow-scripts"
        src={source}
        title="SmartPerfetto 原始 HTML 报告"
      />
    </section>
  );
}
