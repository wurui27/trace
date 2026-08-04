// @vitest-environment node

import { describe, expect, it } from "vitest";

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
});
