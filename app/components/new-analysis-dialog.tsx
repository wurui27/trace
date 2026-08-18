"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { Smartphone, Upload, X } from "lucide-react";

import type { PerfPilotClient, SubmittedTraceAnalysis } from "../lib/perfpilot-api";
import { DeviceAnalysisForm, type DeviceSubmitter } from "./device-analysis-form";
import { TraceUploadForm, type TraceSubmitter } from "./trace-upload-form";
import { useOptionalPerfPilotSession } from "./perfpilot-session-provider";

interface NewAnalysisDialogProps {
  readonly disabled?: boolean;
  readonly submitter?: TraceSubmitter;
  readonly deviceSubmitter?: DeviceSubmitter;
  readonly onSubmitted?: (result: SubmittedTraceAnalysis) => void;
  readonly client?: PerfPilotClient;
  readonly teamId?: string | null;
}

export function NewAnalysisDialog({
  disabled = false,
  submitter,
  deviceSubmitter,
  onSubmitted,
  client: providedClient,
  teamId: providedTeamId,
}: NewAnalysisDialogProps = {}) {
  const session = useOptionalPerfPilotSession();
  const client = providedClient ?? session?.client;
  const teamId = providedTeamId ?? session?.team?.id ?? null;
  const [isOpen, setIsOpen] = useState(false);
  const [mode, setMode] = useState<"device" | "trace">("trace");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousBodyOverflowRef = useRef<string | null>(null);
  const titleId = useId();

  const closeDialog = useCallback(() => {
    setIsOpen(false);
    setMode("trace");
  }, []);

  const handleSubmitted = useCallback(
    (result: SubmittedTraceAnalysis) => {
      closeDialog();
      onSubmitted?.(result);
    },
    [closeDialog, onSubmitted],
  );

  useEffect(() => {
    if (!isOpen) return;
    const trigger = triggerRef.current;
    previousBodyOverflowRef.current = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeDialog();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusableElements = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]",
        ),
      ).filter((element) => {
        const style = window.getComputedStyle(element);
        return !element.hidden && style.display !== "none" && style.visibility !== "hidden";
      });
      const first = focusableElements[0];
      const last = focusableElements.at(-1);
      if (!first || !last) return;
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      if (previousBodyOverflowRef.current !== null) {
        document.body.style.overflow = previousBodyOverflowRef.current;
        previousBodyOverflowRef.current = null;
      }
      trigger?.focus();
    };
  }, [closeDialog, isOpen]);

  const handleOverlayClick = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) closeDialog();
  };

  return (
    <div className="new-analysis-dialog-root">
      <button
        ref={triggerRef}
        type="button"
        className="new-analysis-button"
        onClick={() => setIsOpen(true)}
        disabled={disabled}
      >
        {disabled ? "分析进行中" : "新建分析"}
      </button>

      {isOpen ? (
        <div className="new-analysis-dialog-overlay" onClick={handleOverlayClick}>
          <div
            ref={dialogRef}
            className="new-analysis-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
          >
            <header className="new-analysis-dialog-header">
              <div>
                <p className="section-label">分析入口</p>
                <h2 id={titleId}>新建性能分析</h2>
                <p>选择真机测试类别，或直接上传已有 Trace。</p>
              </div>
              <button
                ref={closeButtonRef}
                type="button"
                className="new-analysis-dialog-close"
                aria-label="关闭"
                onClick={closeDialog}
              >
                <X aria-hidden="true" />
              </button>
            </header>

            <div className="new-analysis-mode-grid" role="group" aria-label="分析方式">
              <button
                type="button"
                className={`new-analysis-mode-card${mode === "device" ? " is-selected" : ""}`}
                aria-label="真机性能测试"
                aria-pressed={mode === "device"}
                onClick={() => setMode("device")}
              >
                <span className="new-analysis-mode-icon">
                  <Smartphone aria-hidden="true" />
                </span>
                <span className="new-analysis-mode-copy">
                  <span className="new-analysis-mode-heading">
                    <strong>真机性能测试</strong>
                    <span className="new-analysis-recommended-badge">设备采集</span>
                  </span>
                  <span>选择冷启动、热启动或手动滑动，直接采集设备 Trace。</span>
                </span>
              </button>

              <button
                type="button"
                className={`new-analysis-mode-card${mode === "trace" ? " is-selected" : ""}`}
                aria-label="上传 Trace 分析"
                aria-pressed={mode === "trace"}
                onClick={() => setMode("trace")}
              >
                <span className="new-analysis-mode-icon">
                  <Upload aria-hidden="true" />
                </span>
                <span className="new-analysis-mode-copy">
                  <strong>上传 Trace 分析</strong>
                  <span>Trace 必填，辅助文件按需添加。</span>
                </span>
              </button>
            </div>

            {mode === "trace" ? (
              <TraceUploadForm
                submitter={submitter}
                client={client}
                teamId={teamId}
                onCancel={closeDialog}
                onSubmitted={handleSubmitted}
              />
            ) : (
              <DeviceAnalysisForm
                submitter={deviceSubmitter}
                onCancel={closeDialog}
                onSubmitted={handleSubmitted}
              />
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
