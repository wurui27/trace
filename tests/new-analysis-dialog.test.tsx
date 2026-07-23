// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { NewAnalysisDialog } from "../app/components/new-analysis-dialog";

afterEach(cleanup);

describe("NewAnalysisDialog", () => {
  it("opens, switches modes, and closes while managing page focus and scroll", async () => {
    const user = userEvent.setup();
    const initialOverflow = document.body.style.overflow;

    render(<NewAnalysisDialog />);

    expect(
      screen.queryByRole("dialog", { name: "新建性能分析" }),
    ).not.toBeInTheDocument();

    const trigger = screen.getByRole("button", { name: "新建分析" });
    trigger.focus();
    expect(trigger).toHaveFocus();

    await user.click(trigger);

    expect(
      screen.getByRole("dialog", { name: "新建性能分析" }),
    ).toBeInTheDocument();
    expect(document.body).toHaveStyle({ overflow: "hidden" });
    expect(screen.getByText("选择 APK")).toBeInTheDocument();
    expect(screen.getByText("选择真机")).toBeInTheDocument();
    expect(screen.getByText("选择场景")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "上传 Trace 分析" }),
    );

    expect(screen.getByLabelText("Trace 文件")).toBeRequired();
    expect(screen.getByLabelText("APK 文件（可选）")).toBeInTheDocument();
    expect(screen.getByLabelText("源码压缩包（可选）")).toBeInTheDocument();
    expect(screen.getByLabelText("Mapping 文件（可选）")).toBeInTheDocument();
    expect(screen.getByLabelText("Native Symbols（可选）")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "关闭" }));

    expect(
      screen.queryByRole("dialog", { name: "新建性能分析" }),
    ).not.toBeInTheDocument();
    expect(document.body.style.overflow).toBe(initialOverflow);
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("closes with Escape and restores focus to the trigger", async () => {
    const user = userEvent.setup();

    render(<NewAnalysisDialog />);

    const trigger = screen.getByRole("button", { name: "新建分析" });
    await user.click(trigger);
    expect(
      screen.getByRole("dialog", { name: "新建性能分析" }),
    ).toBeInTheDocument();

    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("dialog", { name: "新建性能分析" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
