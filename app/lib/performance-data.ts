export type Severity = "critical" | "warning" | "healthy";

export type MetricState = "measured" | "missing" | "failed";

export type ProblemStatus =
  | "已确认问题"
  | "疑似问题"
  | "通过"
  | "数据不足"
  | "本次采集无效";

export type EvidenceStep =
  | "确认症状"
  | "锁定场景窗口"
  | "确认线程状态"
  | "追踪依赖"
  | "排除系统条件";

export interface EvidenceItem {
  readonly step: EvidenceStep;
  readonly interval: string;
  readonly value: string;
  readonly explanation: string;
}

export interface PerformanceProblem {
  readonly id: string;
  readonly title: string;
  readonly area: string;
  readonly severity: Severity;
  readonly status: ProblemStatus;
  readonly impact: string;
  readonly impactLabel: string;
  readonly summary: string;
  readonly conclusion: string;
  readonly suggestion: string;
  readonly confidence: number;
  readonly validSamples: number;
  readonly reproducedRuns: number;
  readonly totalRuns: number;
  readonly variability: string;
  readonly currentValue: string;
  readonly targetValue: string;
  readonly delta: string;
  readonly comparisonBasis: string;
  readonly sourceLocation: string;
  readonly acceptanceCriteria: string;
  readonly evidence: ReadonlyArray<EvidenceItem>;
}

export interface DashboardData {
  readonly app: {
    readonly name: string;
    readonly packageName: string;
    readonly version: string;
  };
  readonly device: {
    readonly name: string;
    readonly os: string;
    readonly serial: string;
    readonly verified: boolean;
  };
  readonly startup: {
    readonly value: string;
    readonly target: string;
    readonly state: MetricState;
    readonly context: string;
    readonly cold: string;
    readonly warm: string;
    readonly hot: string;
  };
  readonly secondaryMetrics: ReadonlyArray<{
    readonly id: string;
    readonly label: string;
    readonly value: string;
    readonly unit: string;
    readonly state: MetricState;
    readonly context: string;
  }>;
  readonly problems: ReadonlyArray<PerformanceProblem>;
  readonly credibility: {
    readonly runs: number;
    readonly deviceConsistency: string;
    readonly thermalState: string;
    readonly failures: number;
  };
}
