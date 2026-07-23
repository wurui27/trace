import type { ProblemStatus } from "./performance-data";

export type StatusTone =
  | "danger"
  | "warning"
  | "success"
  | "neutral"
  | "invalid";

const statusToneByStatus: Record<ProblemStatus, StatusTone> = {
  已确认问题: "danger",
  疑似问题: "warning",
  通过: "success",
  数据不足: "neutral",
  本次采集无效: "invalid",
};

export function getProblemStatusTone(status: ProblemStatus): StatusTone {
  return statusToneByStatus[status];
}
