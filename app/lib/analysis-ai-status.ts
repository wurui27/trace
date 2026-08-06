import type { AnalysisAiRound } from "./perfpilot-api";

const LEGACY_ROLES: readonly AnalysisAiRound["role"][] = [
  "extract",
  "review",
  "finalize",
];

function singlePass(
  rounds: readonly AnalysisAiRound[] | undefined,
): rounds is readonly [AnalysisAiRound] {
  return rounds?.length === 1 && rounds[0]?.round === 1 && rounds[0].role === "report";
}

function legacyPass(
  rounds: readonly AnalysisAiRound[] | undefined,
): rounds is readonly [AnalysisAiRound, AnalysisAiRound, AnalysisAiRound] {
  return (
    rounds?.length === LEGACY_ROLES.length &&
    rounds.every(
      (round, index) =>
        round.round === index + 1 && round.role === LEGACY_ROLES[index],
    )
  );
}

export function completedAiProcessCopy(
  rounds: readonly AnalysisAiRound[] | undefined,
): { readonly title: string; readonly detail: string } {
  if (singlePass(rounds)) {
    return {
      title: "单轮 PerfPilot AI 深度分析已完成",
      detail: "证据核验、归因、建议与复测计划",
    };
  }
  if (legacyPass(rounds)) {
    const completed = rounds.filter((round) => round.state === "completed").length;
    return {
      title: `${completed} 轮 PerfPilot AI 已完成`,
      detail: "提取、复核、定稿",
    };
  }
  return {
    title: "PerfPilot AI 已完成",
    detail: "证据核验、归因、建议与复测计划",
  };
}

export function aiCompletionBadge(
  rounds: readonly AnalysisAiRound[] | undefined,
): string {
  if (singlePass(rounds)) return "PerfPilot AI 单轮完成";
  if (legacyPass(rounds)) {
    const completed = rounds.filter((round) => round.state === "completed").length;
    return `PerfPilot AI ${completed}/${rounds.length}`;
  }
  return "PerfPilot AI 已完成";
}

export function runningAiProcessLabel(
  rounds: readonly AnalysisAiRound[] | undefined,
): string {
  const running = rounds?.find((round) => round.state === "running");
  if (singlePass(rounds) && running?.role === "report") {
    return "PerfPilot AI 正在生成最终报告";
  }
  if (legacyPass(rounds) && running !== undefined) {
    return `PerfPilot AI 第 ${running.round}/${rounds.length} 轮`;
  }
  return "PerfPilot AI 正在生成最终报告";
}
