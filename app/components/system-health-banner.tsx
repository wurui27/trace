"use client";

import type { AnalysisHealthResponse } from "../lib/perfpilot-api";


interface SystemHealthBannerProps {
  readonly health: AnalysisHealthResponse | null;
}

export function SystemHealthBanner({ health }: SystemHealthBannerProps) {
  if (health === null || health.state === "healthy") return null;
  const affected = health.capabilities.filter((item) => item.state !== "healthy");
  const copyDiagnostics = async (): Promise<void> => {
    const safe = {
      schema_version: health.schema_version,
      state: health.state,
      capabilities: health.capabilities.map((item) => ({
        name: item.name,
        state: item.state,
        last_checked_at: item.last_checked_at,
      })),
    };
    await navigator.clipboard?.writeText(JSON.stringify(safe));
  };

  return (
    <section
      className={`system-health-banner is-${health.state}`}
      role={health.state === "unavailable" ? "alert" : "status"}
    >
      <div className="system-health-summary">
        <div>
          <strong>
            {health.state === "unavailable"
              ? "分析服务暂不可用"
              : "部分分析能力暂不可用"}
          </strong>
          <p>可用功能仍可继续使用，系统不会把等待误报为分析失败。</p>
        </div>
        <button type="button" onClick={() => void copyDiagnostics()}>
          复制诊断信息
        </button>
      </div>
      {affected.length > 0 ? (
        <details>
          <summary>查看受影响能力</summary>
          <ul>
            {affected.map((item) => (
              <li key={item.name}>
                <strong>{item.name}</strong>
                <span>{item.message}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}
