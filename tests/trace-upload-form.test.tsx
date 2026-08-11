// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { TraceUploadForm } from "../app/components/trace-upload-form";
import {
  PerfPilotApiError,
  type PerfPilotClient,
  type SubmitTraceInput,
} from "../app/lib/perfpilot-api";

afterEach(cleanup);

it("selects an Agent workspace without rendering or reading an Android device", async () => {
  const user = userEvent.setup();
  const devices = vi.fn();
  const client = {
    devices,
    sourceWorkspaces: vi.fn().mockResolvedValue({
      schema_version: "1.0",
      workspaces: [
        {
          provider_kind: "agent_workspace",
          agent_id: "73000000-0000-4000-8000-000000000001",
          agent_name: "Mac Agent",
          workspace_id: "92000000-0000-4000-8000-000000000001",
          name: "Demo App",
          state: "ready",
          git_branch: "main",
          git_head: "a".repeat(40),
          tracked_dirty_count: 0,
          snapshot_policy: "tracked_worktree",
          validation_profiles: [],
        },
      ],
    }),
  } as unknown as PerfPilotClient;
  const submitter = vi.fn().mockResolvedValue({
    teamId: "team-1",
    analysis: { analysis_id: "analysis-1" },
  });
  render(
    <TraceUploadForm
      client={client}
      teamId="team-1"
      submitter={submitter}
    />,
  );

  expect(screen.queryByText(/Pixel 8/)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/Android 设备/)).not.toBeInTheDocument();
  expect(screen.queryByLabelText(/源码压缩包/)).not.toBeInTheDocument();
  await user.selectOptions(
    await screen.findByLabelText("源码工作区"),
    "92000000-0000-4000-8000-000000000001",
  );
  await user.upload(
    screen.getByLabelText("Trace 文件"),
    new File([new Uint8Array([1])], "startup.trace"),
  );
  await user.click(screen.getByRole("button", { name: "开始分析" }));

  expect(devices).not.toHaveBeenCalled();
  expect(submitter).toHaveBeenCalledWith(
    expect.objectContaining({
      sourceBinding: {
        provider_kind: "agent_workspace",
        agent_id: "73000000-0000-4000-8000-000000000001",
        workspace_id: "92000000-0000-4000-8000-000000000001",
        snapshot_policy: "tracked_worktree",
        validation_profile_id: null,
      },
    }),
  );
});

it("hands the accepted analysis to the dashboard without keeping a result panel", async () => {
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
        stages: [
          { stage: "input_validation" as const, state: "completed" as const, failure: null },
          { stage: "smartperfetto" as const, state: "running" as const, failure: null },
          { stage: "perfpilot_ai" as const, state: "pending" as const, failure: null },
          { stage: "report" as const, state: "pending" as const, failure: null },
        ],
        failure: null,
      },
    };
  });
  const onSubmitted = vi.fn();
  render(<TraceUploadForm submitter={submitter} onSubmitted={onSubmitted} />);

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
  expect(onSubmitted).toHaveBeenCalledWith(
    expect.objectContaining({
      teamId: "team-1",
      analysis: expect.objectContaining({ analysis_id: "analysis-real-1" }),
    }),
  );
  expect(screen.queryByText("analysis-real-1")).not.toBeInTheDocument();
});

it("explains when the local analysis service is not configured", async () => {
  const user = userEvent.setup();
  const submitter = vi.fn(async () => {
    throw new PerfPilotApiError(
      "proxy_configuration_invalid",
      "Proxy configuration is unavailable",
      false,
      "request-1",
    );
  });
  render(<TraceUploadForm submitter={submitter} />);

  await user.upload(
    screen.getByLabelText("Trace 文件"),
    new File([new Uint8Array([1, 2, 3])], "startup.trace"),
  );
  await user.click(screen.getByRole("button", { name: "开始分析" }));

  expect(
    await screen.findByText("本地分析服务未配置或未启动，请先启动本地服务。"),
  ).toBeInTheDocument();
});
