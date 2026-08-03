// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { TraceUploadForm } from "../app/components/trace-upload-form";
import type { SubmitTraceInput } from "../app/lib/perfpilot-api";

afterEach(cleanup);

it("submits the selected Trace profile and question and shows the real analysis id", async () => {
  const user = userEvent.setup();
  const submitter = vi.fn(async (submission: SubmitTraceInput) => {
    void submission;
    return {
      teamId: "team-1",
      analysis: {
        schema_version: "1.0" as const,
        analysis_id: "analysis-real-1",
        team_id: "team-1",
        analysis_mode: "trace_upload" as const,
        analysis_profile: "startup" as const,
        question: "首帧前为什么慢？",
        state: "analyzing" as const,
        version: 3,
        report_available: false,
        input_uploads: [],
        failure: null,
      },
    };
  });
  render(<TraceUploadForm submitter={submitter} />);

  await user.selectOptions(screen.getByLabelText("分析重点"), "startup");
  await user.type(screen.getByLabelText("补充问题（可选）"), "首帧前为什么慢？");
  await user.upload(
    screen.getByLabelText("Trace 文件"),
    new File([new Uint8Array([1, 2, 3])], "startup.trace"),
  );
  const start = screen.getByRole("button", { name: "开始分析" });
  expect(start).toBeEnabled();
  await user.click(start);

  expect(submitter).toHaveBeenCalledOnce();
  expect(submitter.mock.calls[0][0]).toMatchObject({
    profile: "startup",
    question: "首帧前为什么慢？",
    files: [{ kind: "trace" }],
  });
  expect(await screen.findByText("analysis-real-1")).toBeInTheDocument();
  expect(screen.getByText("SmartPerfetto 正在分析")).toBeInTheDocument();
});
