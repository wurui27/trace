import { describe, expect, it } from "vitest";

import eslintConfig from "../eslint.config.mjs";

describe("ESLint workspace boundaries", () => {
  it("never traverses generated files in local Git worktrees", () => {
    const ignoredPatterns = eslintConfig.flatMap((entry) => entry.ignores ?? []);

    expect(ignoredPatterns).toContain(".worktrees/**");
  });
});
