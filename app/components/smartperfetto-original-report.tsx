"use client";

import { useMemo, useState } from "react";

import type { PerfPilotClient } from "../lib/perfpilot-api";

export function SmartPerfettoOriginalReport({
  active,
  analysisId,
  teamId,
  client,
  onReady,
  printFallback = false,
}: {
  readonly active: boolean;
  readonly analysisId: string;
  readonly teamId?: string;
  readonly client: PerfPilotClient;
  readonly onReady?: (ready: boolean) => void;
  readonly printFallback?: boolean;
}) {
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const source = useMemo(
    () => active && teamId ? client.smartPerfettoOriginalUrl(teamId, analysisId) : null,
    [active, analysisId, client, teamId],
  );
  const download = useMemo(
    () => active && teamId ? client.smartPerfettoOriginalDownloadUrl(teamId, analysisId) : null,
    [active, analysisId, client, teamId],
  );

  if (!teamId) {
    return <p role="status">当前页面缺少团队上下文，无法读取原始报告。</p>;
  }
  if (failed || printFallback) {
    return (
      <div role="alert">
        <p>SmartPerfetto 原始 HTML 暂时无法读取，本次打印保留了稳定的错误说明。</p>
        {!printFallback ? <button
          type="button"
          onClick={() => {
            setFailed(false);
            setAttempt((value) => value + 1);
          }}
        >
          重试
        </button> : null}
      </div>
    );
  }
  if (!active) return null;

  if (source === null || download === null) return null;
  return (
    <section className="smartperfetto-original-report" aria-label="SmartPerfetto 原始报告内容">
      <header className="analysis-report-section smartperfetto-original-actions">
        <div>
          <h2>SmartPerfetto 原始 HTML</h2>
          <p>以下内容由 SmartPerfetto 原样生成，PerfPilot 不改写其中任何文字或结构。</p>
        </div>
        <a
          href={download}
          download={`smartperfetto-${analysisId}.html`}
        >
          下载原始 HTML
        </a>
      </header>
      <iframe
        key={attempt}
        className="smartperfetto-original-frame"
        onError={() => {
          setFailed(true);
          onReady?.(false);
        }}
        onLoad={() => onReady?.(true)}
        sandbox="allow-scripts"
        src={source}
        title="SmartPerfetto 原始 HTML 报告"
      />
    </section>
  );
}
