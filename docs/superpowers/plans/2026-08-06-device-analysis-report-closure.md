# Device Analysis Report Closure Plan

> **Execution:** Follow the existing TDD and verification gates. Commit and push each runnable boundary directly to `main`.

**Goal:** Turn a completed Device Agent capture into a SmartPerfetto canonical result, a PerfPilot AI synthesis, and a final report that the dashboard can open.

**Architecture:** Reuse the existing Trace orchestration and synthesis pipeline. The authoritative tenant `Analysis.analysis_mode` is carried through normalization and report publication; device work is never converted into a manual `trace_upload` analysis. Parent and scenario states are projected from the immutable report. Android Memory remains a separately versioned engine and is joined in the next boundary.

## Task 1: Make report contracts mode-aware

- Add failing contract, normalizer, and report-writer tests for `analysis_mode=device`.
- Allow normalized SmartPerfetto reports and AnalysisReport 1.1 documents to carry either `trace_upload` or `device`.
- Pass the authoritative mode explicitly into normalization and report composition.
- Keep existing Trace upload bytes and behavior unchanged.

## Task 2: Run device SmartPerfetto through synthesis

- Add failing execution/result repository tests for `device + smartperfetto`.
- Allow that pair to allocate, persist its canonical result, and emit `engine_result_ready`.
- Allow the synthesis request factory, context loader, report writer, and parent projector to operate on device analyses.
- Project terminal parent and scenario states without claiming missing evidence is complete.

## Task 3: Expose the immutable device report

- Add failing Analysis service/API and web parser tests for a device AnalysisReport 1.1.
- Prefer the parent immutable report for device analyses when it is bound to the latest SmartPerfetto execution.
- Set `report_available=true` and serve `GET /report` from that parent report.
- Accept and render device v1.1 in the existing final-report page and dashboard entry.

## Task 4: Verify the vertical slice

- Run focused RED/GREEN tests after every layer.
- Run API lint and full API test suites.
- Run web lint and tests.
- Run a local Device Agent completion-to-report smoke test where the configured kernels are available.
- Commit and push the verified boundary to `main`.

## Task 5: Join Android Memory evidence

- Inspect the external Android Memory kernel input contract against the Agent's current memory evidence archive.
- Stage safe server-owned inputs without copying or vendoring the kernel.
- Execute `device + android_memory`, persist its canonical result, and join supported facts into the final PerfPilot projection/report.
- Preserve a SmartPerfetto-only partial report if memory analysis is unavailable.
