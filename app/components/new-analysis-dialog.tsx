"use client";

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type MouseEvent,
} from "react";
import { ShieldCheck, Smartphone, Upload, X } from "lucide-react";

type AnalysisMode = "agent" | "upload";

export function NewAnalysisDialog() {
  const [isOpen, setIsOpen] = useState(false);
  const [mode, setMode] = useState<AnalysisMode>("agent");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousBodyOverflowRef = useRef<string | null>(null);

  const titleId = useId();
  const agentApkId = useId();
  const deviceId = useId();
  const scenarioId = useId();
  const traceId = useId();
  const uploadApkId = useId();
  const sourceArchiveId = useId();
  const mappingId = useId();
  const nativeSymbolsId = useId();

  const closeDialog = useCallback(() => {
    setIsOpen(false);
  }, []);

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const trigger = triggerRef.current;
    previousBodyOverflowRef.current = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButtonRef.current?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeDialog();
        return;
      }

      if (event.key !== "Tab") {
        return;
      }

      const dialog = dialogRef.current;

      if (!dialog) {
        return;
      }

      const focusableElements = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href]",
        ),
      ).filter((element) => {
        const style = window.getComputedStyle(element);

        return (
          !element.hidden &&
          style.display !== "none" &&
          style.visibility !== "hidden"
        );
      });
      const firstFocusableElement = focusableElements[0];
      const lastFocusableElement = focusableElements.at(-1);

      if (!firstFocusableElement || !lastFocusableElement) {
        return;
      }

      if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        firstFocusableElement.focus();
        return;
      }

      if (event.shiftKey && document.activeElement === firstFocusableElement) {
        event.preventDefault();
        lastFocusableElement.focus();
      } else if (
        !event.shiftKey &&
        document.activeElement === lastFocusableElement
      ) {
        event.preventDefault();
        firstFocusableElement.focus();
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

  const openDialog = () => {
    setMode("agent");
    setIsOpen(true);
  };

  const handleOverlayClick = (event: MouseEvent<HTMLDivElement>) => {
    if (event.target === event.currentTarget) {
      closeDialog();
    }
  };

  return (
    <div className="new-analysis-dialog-root">
      <button
        ref={triggerRef}
        type="button"
        className="new-analysis-button"
        onClick={openDialog}
      >
        新建分析
      </button>

      {isOpen ? (
        <div
          className="new-analysis-dialog-overlay"
          onClick={handleOverlayClick}
        >
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
                <p>选择自动采集，或上传已有 Trace 与辅助文件。</p>
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

            <div
              className="new-analysis-mode-grid"
              role="group"
              aria-label="分析方式"
            >
              <button
                type="button"
                className={`new-analysis-mode-card${
                  mode === "agent" ? " is-selected" : ""
                }`}
                aria-label="真机自动测试"
                aria-pressed={mode === "agent"}
                onClick={() => setMode("agent")}
              >
                <span className="new-analysis-mode-icon">
                  <Smartphone aria-hidden="true" />
                </span>
                <span className="new-analysis-mode-copy">
                  <span className="new-analysis-mode-heading">
                    <strong>真机自动测试</strong>
                    <span className="new-analysis-recommended-badge">推荐</span>
                  </span>
                  <span>
                    Agent 将安装 APK、运行所选场景、采集 Trace 并上传到分析环境。
                  </span>
                </span>
              </button>

              <button
                type="button"
                className={`new-analysis-mode-card${
                  mode === "upload" ? " is-selected" : ""
                }`}
                aria-label="上传 Trace 分析"
                aria-pressed={mode === "upload"}
                onClick={() => setMode("upload")}
              >
                <span className="new-analysis-mode-icon">
                  <Upload aria-hidden="true" />
                </span>
                <span className="new-analysis-mode-copy">
                  <strong>上传 Trace 分析</strong>
                  <span>
                    Trace 文件必填；附加代码产物可提升符号与源码映射质量。
                  </span>
                </span>
              </button>
            </div>

            <div className="new-analysis-fields">
              {mode === "agent" ? (
                <>
                  <div className="new-analysis-field">
                    <label htmlFor={agentApkId}>选择 APK</label>
                    <input
                      id={agentApkId}
                      type="file"
                      accept=".apk"
                      required
                    />
                  </div>

                  <div className="new-analysis-field">
                    <label htmlFor={deviceId}>选择真机</label>
                    <select id={deviceId} defaultValue="pixel-8">
                      <option value="pixel-8">Pixel 8 · Android 15</option>
                    </select>
                  </div>

                  <div className="new-analysis-field">
                    <label htmlFor={scenarioId}>选择场景</label>
                    <select id={scenarioId} defaultValue="cold-start">
                      <option value="cold-start">冷启动 · 首页首帧</option>
                      <option value="gallery-scroll">
                        相册网格 · 连续滚动
                      </option>
                      <option value="detail-navigation">
                        详情页 · 打开与返回
                      </option>
                    </select>
                  </div>
                </>
              ) : (
                <>
                  <div className="new-analysis-field">
                    <label htmlFor={traceId}>Trace 文件</label>
                    <input
                      id={traceId}
                      type="file"
                      accept=".perfetto-trace,.trace,.ctrace,.pb"
                      required
                    />
                  </div>

                  <div className="new-analysis-field">
                    <label htmlFor={uploadApkId}>APK 文件（可选）</label>
                    <input id={uploadApkId} type="file" accept=".apk" />
                  </div>

                  <div className="new-analysis-field">
                    <label htmlFor={sourceArchiveId}>
                      源码压缩包（可选）
                    </label>
                    <input
                      id={sourceArchiveId}
                      type="file"
                      accept=".zip,.tar,.tar.gz,.tgz"
                    />
                  </div>

                  <div className="new-analysis-field">
                    <label htmlFor={mappingId}>Mapping 文件（可选）</label>
                    <input id={mappingId} type="file" accept=".txt" />
                  </div>

                  <div className="new-analysis-field">
                    <label htmlFor={nativeSymbolsId}>
                      Native Symbols（可选）
                    </label>
                    <input
                      id={nativeSymbolsId}
                      type="file"
                      accept=".zip,.tar,.tar.gz,.tgz,.so"
                    />
                  </div>
                </>
              )}
            </div>

            <footer className="new-analysis-dialog-footer">
              <p className="new-analysis-privacy-note">
                <ShieldCheck aria-hidden="true" />
                所有上传数据与采集结果仅保留在内部环境中。
              </p>
              <div className="new-analysis-dialog-actions">
                <button type="button" onClick={closeDialog}>
                  取消
                </button>
                <button type="button" className="primary-action" disabled>
                  接入任务服务后可用
                </button>
              </div>
            </footer>
          </div>
        </div>
      ) : null}
    </div>
  );
}
