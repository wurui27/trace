// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { NewAnalysisDialog } from "../app/components/new-analysis-dialog";
import type { TraceSubmitter } from "../app/components/trace-upload-form";

afterEach(cleanup);

it("closes after the backend accepts the uploaded Trace", async () => {
  const user = userEvent.setup();
  const submitter: TraceSubmitter = vi.fn().mockResolvedValue({
    teamId: "team-1",
    analysis: {
      schema_version: "1.0",
      analysis_id: "analysis-active-1",
      team_id: "team-1",
      analysis_mode: "trace_upload",
      analysis_profile: "auto",
      question: null,
      state: "analyzing",
      version: 3,
      created_at: "2026-08-04T08:00:00Z",
      cancel_requested_at: null,
      report_available: false,
      input_uploads: [],
      stages: [
        { stage: "input_validation", state: "completed", failure: null },
        { stage: "smartperfetto", state: "running", failure: null },
        { stage: "perfpilot_ai", state: "pending", failure: null },
        { stage: "report", state: "pending", failure: null },
      ],
      failure: null,
    },
  });
  const onSubmitted = vi.fn();
  render(
    <NewAnalysisDialog submitter={submitter} onSubmitted={onSubmitted} />,
  );

  await user.click(screen.getByRole("button", { name: "新建分析" }));
  await user.upload(
    screen.getByLabelText("Trace 文件"),
    new File([new Uint8Array([1, 2, 3])], "startup.trace"),
  );
  await user.click(screen.getByRole("button", { name: "开始分析" }));

  expect(onSubmitted).toHaveBeenCalledOnce();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("prevents a second submission while an analysis is active", () => {
  render(<NewAnalysisDialog disabled />);

  expect(screen.getByRole("button", { name: "分析进行中" })).toBeDisabled();
});
