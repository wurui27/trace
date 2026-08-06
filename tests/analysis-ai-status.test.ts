import { describe, expect, it } from "vitest";

import {
  aiCompletionBadge,
  completedAiProcessCopy,
  runningAiProcessLabel,
} from "../app/lib/analysis-ai-status";
import type { AnalysisAiRound } from "../app/lib/perfpilot-api";

const singleCompleted: readonly AnalysisAiRound[] = [
  { round: 1, role: "report", state: "completed", attempts: 1 },
];

const legacyCompleted: readonly AnalysisAiRound[] = [
  { round: 1, role: "extract", state: "completed", attempts: 1 },
  { round: 2, role: "review", state: "completed", attempts: 1 },
  { round: 3, role: "finalize", state: "completed", attempts: 1 },
];

describe("analysis AI status copy", () => {
  it("describes a completed single-pass report without inventing rounds", () => {
    expect(completedAiProcessCopy(singleCompleted)).toEqual({
      title: "单轮 PerfPilot AI 深度分析已完成",
      detail: "证据核验、归因、建议与复测计划",
    });
    expect(aiCompletionBadge(singleCompleted)).toBe("PerfPilot AI 单轮完成");
  });

  it("preserves completed legacy three-round copy", () => {
    expect(completedAiProcessCopy(legacyCompleted)).toEqual({
      title: "3 轮 PerfPilot AI 已完成",
      detail: "提取、复核、定稿",
    });
    expect(aiCompletionBadge(legacyCompleted)).toBe("PerfPilot AI 3/3");
  });

  it("describes the running round from its recognized protocol layout", () => {
    const singleRunning: readonly AnalysisAiRound[] = [
      { round: 1, role: "report", state: "running", attempts: 1 },
    ];
    const legacyRunning: readonly AnalysisAiRound[] = [
      { round: 1, role: "extract", state: "completed", attempts: 1 },
      { round: 2, role: "review", state: "running", attempts: 1 },
      { round: 3, role: "finalize", state: "pending", attempts: 0 },
    ];

    expect(runningAiProcessLabel(singleRunning)).toBe(
      "PerfPilot AI 正在生成最终报告",
    );
    expect(runningAiProcessLabel(legacyRunning)).toBe("PerfPilot AI 第 2/3 轮");
  });

  it.each([
    ["missing", undefined],
    [
      "unrecognized",
      [{ round: 1, role: "review", state: "completed", attempts: 1 }] as const,
    ],
  ])("uses honest generic fallbacks for %s rounds", (_case, rounds) => {
    const completed = completedAiProcessCopy(rounds);

    expect(completed).toEqual({
      title: "PerfPilot AI 已完成",
      detail: "证据核验、归因、建议与复测计划",
    });
    expect(aiCompletionBadge(rounds)).toBe("PerfPilot AI 已完成");
    expect(runningAiProcessLabel(rounds)).toBe(
      "PerfPilot AI 正在生成最终报告",
    );
    expect(`${completed.title} ${completed.detail} ${aiCompletionBadge(rounds)}`).not.toContain(
      "3 轮",
    );
  });
});
