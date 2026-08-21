"use client";

import Link from "next/link";
import { ArrowUpRight, Clock3, FileCheck2 } from "lucide-react";
import { useEffect, useState } from "react";

import {
  createPerfPilotClient,
  type AnalysisListItem,
  type PerfPilotClient,
} from "../lib/perfpilot-api";
import { useOptionalPerfPilotSession } from "./perfpilot-session-provider";

const HISTORY_LIMIT = 10;
const defaultClient = createPerfPilotClient();
const testTypeLabels = {
  cold_start: "冷启动",
  hot_start: "热启动",
  scroll: "滑动",
  other: "其他",
} as const;

type HistoryView =
  | { readonly state: "loading" }
  | { readonly state: "empty" }
  | { readonly state: "error" }
  | {
      readonly state: "ready";
      readonly analyses: readonly AnalysisListItem[];
    };

interface AnalysisHistoryProps {
  readonly client?: PerfPilotClient;
  readonly teamId?: string;
}

function historyTypeLabel(analysis: AnalysisListItem): string {
  if (analysis.test_type === "other" && analysis.custom_test_name?.trim()) {
    return analysis.custom_test_name.trim();
  }
  return analysis.test_type
    ? testTypeLabels[analysis.test_type]
    : "未记录测试类型";
}

function historyPackageName(analysis: AnalysisListItem): string {
  return (
    analysis.package_name?.trim() ||
    analysis.application_metadata?.package_name?.trim() ||
    "未记录包名"
  );
}

function formatHistoryTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

export function AnalysisHistory({
  client: providedClient,
  teamId: providedTeamId,
}: AnalysisHistoryProps) {
  const session = useOptionalPerfPilotSession();
  const client = providedClient ?? session?.client ?? defaultClient;
  const teamId = providedTeamId ?? session?.team?.id ?? null;
  const [view, setView] = useState<HistoryView>({ state: "loading" });

  useEffect(() => {
    if (teamId === null) return;
    const controller = new AbortController();
    setView({ state: "loading" });
    client
      .analyses(teamId, HISTORY_LIMIT, controller.signal)
      .then(({ analyses }) => {
        if (controller.signal.aborted) return;
        setView(
          analyses.length === 0
            ? { state: "empty" }
            : { state: "ready", analyses },
        );
      })
      .catch(() => {
        if (!controller.signal.aborted) setView({ state: "error" });
      });
    return () => controller.abort();
  }, [client, teamId]);

  return (
    <section className="analysis-history-page" aria-labelledby="analysis-history-title">
      <header className="analysis-history-header">
        <p className="page-eyebrow">成功分析记录</p>
        <h1 id="analysis-history-title">测试历史</h1>
        <p>查看当前账号最近完成的性能分析和最终报告。</p>
      </header>

      <p className="analysis-history-notice">
        历史数据仅保留最近 10 份，超过后最旧数据将自动丢弃。
      </p>

      {view.state === "loading" ? (
        <div className="analysis-history-state" role="status">
          <Clock3 aria-hidden="true" />
          <strong>正在读取测试历史</strong>
          <p>请稍候，正在获取当前账号的成功分析。</p>
        </div>
      ) : view.state === "empty" ? (
        <div className="analysis-history-state">
          <FileCheck2 aria-hidden="true" />
          <strong>还没有成功的测试记录</strong>
          <p>完成一次分析后，报告会显示在这里。</p>
          <Link href="/" prefetch={false}>返回总览</Link>
        </div>
      ) : view.state === "error" ? (
        <div className="analysis-history-state is-error" role="alert">
          <FileCheck2 aria-hidden="true" />
          <strong>暂时无法读取测试历史</strong>
          <p>请稍后刷新页面，已有报告不会被演示数据替换。</p>
        </div>
      ) : (
        <ol className="analysis-history-list">
          {view.analyses.map((analysis) => {
            const createdAt = formatHistoryTime(analysis.created_at);
            const completedAt = formatHistoryTime(analysis.completed_at);
            return (
              <li className="analysis-history-item" key={analysis.analysis_id}>
                <div className="analysis-history-copy">
                  <div className="analysis-history-heading">
                    <h2>{historyTypeLabel(analysis)}</h2>
                    <span className="analysis-history-status">分析完成</span>
                  </div>
                  <code className="analysis-history-package">
                    {historyPackageName(analysis)}
                  </code>
                  <div className="analysis-history-meta">
                    <span>
                      创建时间：{createdAt ? (
                        <time dateTime={analysis.created_at}>{createdAt}</time>
                      ) : "未记录创建时间"}
                    </span>
                    <span>
                      完成时间：{completedAt ? (
                        <time dateTime={analysis.completed_at ?? undefined}>
                          {completedAt}
                        </time>
                      ) : "未记录完成时间"}
                    </span>
                  </div>
                </div>
                <Link
                  className="analysis-history-link"
                  href={`/analyses/${analysis.analysis_id}/report`}
                  prefetch={false}
                >
                  查看报告
                  <ArrowUpRight aria-hidden="true" />
                </Link>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
