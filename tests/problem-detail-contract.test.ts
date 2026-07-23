// @vitest-environment node

import { describe, expect, it } from "vitest";

import { getDashboardData } from "../app/lib/performance-data";
import { getProblemStatusTone } from "../app/lib/problem-status";

describe("problem detail contracts", () => {
  it("maps every problem status to its exact presentation tone", () => {
    expect([
      getProblemStatusTone("已确认问题"),
      getProblemStatusTone("疑似问题"),
      getProblemStatusTone("通过"),
      getProblemStatusTone("数据不足"),
      getProblemStatusTone("本次采集无效"),
    ]).toEqual(["danger", "warning", "success", "neutral", "invalid"]);
  });

  it("keeps reproduced runs within a positive total", () => {
    for (const problem of getDashboardData().problems) {
      expect(problem.totalRuns).toBeGreaterThan(0);
      expect(problem.reproducedRuns).toBeLessThanOrEqual(problem.totalRuns);
    }
  });
});
