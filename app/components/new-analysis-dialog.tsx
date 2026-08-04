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

import type { SubmittedTraceAnalysis } from "../lib/perfpilot-api";
import { TraceUploadForm, type TraceSubmitter } from "./trace-upload-form";

interface NewAnalysisDialogProps {
  readonly disabled?: boolean;
  readonly submitter?: TraceSubmitter;
  readonly onSubmitted?: (result: SubmittedTraceAnalysis) => void;
}

export function NewAnalysisDialog({
  disabled = false,
  submitter,
  onSubmitted,
}: NewAnalysisDialogProps = {}) {
  const [isOpen, setIsOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousBodyOverflowRef = useRef<string | null>(null);
  const titleId = useId();

  const closeDialog = useCallback(() => {
    setIsOpen(false);
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
                <p>上传 Trace，SmartPerfetto 将自动解析并生成优化建议。</p>
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
                className="new-analysis-mode-card is-unavailable"
                aria-label="真机自动测试"
                disabled
              >
                <span className="new-analysis-mode-icon">
                  <Smartphone aria-hidden="true" />
                </span>
                <span className="new-analysis-mode-copy">
                  <span className="new-analysis-mode-heading">
                    <strong>真机自动测试</strong>
                    <span className="new-analysis-recommended-badge">待接入</span>
                  </span>
                  <span>ADB Agent 与设备调度完成后开放。</span>
                </span>
              </button>

              <button
                type="button"
                className="new-analysis-mode-card is-selected"
                aria-label="上传 Trace 分析"
                aria-pressed="true"
              >
                <span className="new-analysis-mode-icon">
                  <Upload aria-hidden="true" />
                </span>
                <span className="new-analysis-mode-copy">
                  <strong>上传 Trace 分析</strong>
                  <span>当前可用 · Trace 必填，辅助文件按需添加。</span>
                </span>
              </button>
            </div>

            <TraceUploadForm
              submitter={submitter}
              onCancel={closeDialog}
              onSubmitted={handleSubmitted}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}
