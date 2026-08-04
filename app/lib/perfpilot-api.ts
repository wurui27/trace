import { sha256 } from "@noble/hashes/sha2.js";

const API_PREFIX = "/api/v1/";
const MAX_JSON_BYTES = 10 * 1024 * 1024;
const MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024;
const HASH_CHUNK_BYTES = 4 * 1024 * 1024;
const MIME = /^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/;
const ANALYSIS_STATES = new Set<AnalysisState>([
  "creating",
  "created",
  "uploading",
  "queued",
  "scheduled",
  "running",
  "analyzing",
  "completed",
  "partially_completed",
  "failed",
  "canceled",
  "deleted",
]);
const ACTIVE_ANALYSIS_STATES = new Set<AnalysisState>([
  "creating",
  "created",
  "uploading",
  "queued",
  "scheduled",
  "running",
  "analyzing",
]);

export type TraceInputKind =
  | "trace"
  | "memory_evidence"
  | "apk"
  | "source_archive"
  | "mapping"
  | "native_symbols"
  | "log";
export type TraceProfile = "auto" | "startup" | "scroll";
export type AnalysisState =
  | "creating"
  | "created"
  | "uploading"
  | "queued"
  | "scheduled"
  | "running"
  | "analyzing"
  | "completed"
  | "partially_completed"
  | "failed"
  | "canceled"
  | "deleted";

export type AnalysisStageState =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "canceled"
  | "not_requested";

export interface AnalysisFailure {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
}

export interface AnalysisStage {
  readonly stage: "input_validation" | "smartperfetto" | "perfpilot_ai" | "report";
  readonly state: AnalysisStageState;
  readonly failure: AnalysisFailure | null;
}

export interface AnalysisAiRound {
  readonly round: 1 | 2 | 3;
  readonly role: "extract" | "review" | "finalize";
  readonly state: "pending" | "running" | "completed" | "failed";
  readonly attempts: number;
}

export interface AnalysisSource {
  readonly engine: "smartperfetto";
  readonly rounds: number | null;
  readonly verification: "passed" | "failed" | "unknown";
  readonly session_id: string | null;
  readonly run_id: string | null;
}

export interface ReportMetric {
  readonly metric_id: string;
  readonly name: string;
  readonly status: "available" | "insufficient_data" | "unavailable" | "invalid_capture";
  readonly numeric_value: number | null;
  readonly unit: string | null;
  readonly definition: string;
  readonly threshold: {
    readonly operator: "gt" | "gte" | "lt" | "lte" | "eq";
    readonly value: number;
    readonly unit: string;
  } | null;
}

export interface ReportFinding {
  readonly finding_id: string;
  readonly title: string;
  readonly summary: string;
  readonly severity: "critical" | "warning" | "healthy" | "informational";
  readonly confidence: "high" | "medium" | "low" | "none";
  readonly evidence_ids: readonly string[];
}

export interface ReportEvidence {
  readonly evidence_id: string;
  readonly source: string;
  readonly query_id: string;
  readonly interval_start_ns: number | null;
  readonly interval_end_ns: number | null;
  readonly fields: Readonly<Record<string, unknown>>;
}

export interface ReportScenario {
  readonly scenario_job_id: string;
  readonly scenario_type: "startup" | "scroll" | "memory_cycle";
  readonly result_state: "completed" | "failed" | "canceled";
  readonly device_group_id: string | null;
  readonly device_group_reason: string | null;
  readonly bundle: {
    readonly metrics: readonly ReportMetric[];
    readonly findings: readonly ReportFinding[];
    readonly evidence: readonly ReportEvidence[];
  } | null;
  readonly failure: AnalysisFailure | null;
}

export interface SynthesisOutput {
  readonly schema_version: "1.0";
  readonly executive_summary: string;
  readonly top_findings: ReadonlyArray<{
    readonly finding_id: string;
    readonly evidence_ids: readonly string[];
    readonly user_impact: string;
  }>;
  readonly recommendations: ReadonlyArray<{
    readonly priority: "p0" | "p1" | "p2" | "p3";
    readonly title: string;
    readonly action: string;
    readonly expected_effect: string;
    readonly finding_ids: readonly string[];
    readonly evidence_ids: readonly string[];
  }>;
  readonly retest_plan: ReadonlyArray<{
    readonly mode: "verify_metric" | "collect_evidence";
    readonly scenario_type: "startup" | "scroll" | "memory_cycle";
    readonly metric_ids: readonly string[];
    readonly limitation_ids: readonly string[];
    readonly steps: string;
    readonly success_condition:
      | "meet_existing_threshold"
      | "improve_from_baseline"
      | "evidence_collected";
    readonly failure_condition: "threshold_missed" | "evidence_missing";
  }>;
  readonly limitations: ReadonlyArray<{
    readonly limitation_id: string;
    readonly summary: string;
  }>;
}

export interface SynthesisProvenance {
  readonly provider_protocol: string;
  readonly provider_name: string;
  readonly model: string;
  readonly prompt_template_version: string;
  readonly normalizer_version: string;
  readonly generated_at: string;
  readonly prompt_tokens: number;
  readonly completion_tokens: number;
  readonly total_tokens: number;
  readonly generation: number;
}

export interface AnalysisReport {
  readonly schema_version: "1.1";
  readonly analysis_id: string;
  readonly analysis_mode: "trace_upload";
  readonly state: "completed" | "partially_completed" | "failed" | "canceled";
  readonly report_version: number;
  readonly generated_at: string;
  readonly scenario_reports: readonly ReportScenario[];
  readonly synthesis:
    | {
        readonly state: "completed";
        readonly output: SynthesisOutput;
        readonly synthesis_artifact_id: string;
        readonly failure_code: null;
        readonly provenance: SynthesisProvenance;
      }
    | {
        readonly state: "failed";
        readonly output: null;
        readonly synthesis_artifact_id: null;
        readonly failure_code: string;
        readonly provenance: null;
      };
}

export interface SynthesisRunResponse {
  readonly schema_version: "1.0";
  readonly analysis_id: string;
  readonly generation: number;
  readonly state: "queued";
}

export interface TraceFileSelection {
  readonly kind: TraceInputKind;
  readonly file: File;
}

export interface AnalysisResponse {
  readonly schema_version: "1.0";
  readonly analysis_id: string;
  readonly team_id: string;
  readonly analysis_mode: "trace_upload";
  readonly analysis_profile: TraceProfile;
  readonly question: string | null;
  readonly state: AnalysisState;
  readonly version: number;
  readonly created_at?: string;
  readonly cancel_requested_at?: string | null;
  readonly report_available: boolean;
  readonly stages: readonly AnalysisStage[];
  readonly input_uploads: ReadonlyArray<{
    readonly state: "awaiting_upload" | "pending" | "finalized";
    readonly artifact_kind: TraceInputKind;
    readonly mime: string;
    readonly size: number;
    readonly sha256_b64: string;
    readonly upload_id?: string;
    readonly artifact_id?: string;
  }>;
  readonly failure: AnalysisFailure | null;
  readonly ai_rounds?: readonly AnalysisAiRound[];
  readonly source_analysis?: AnalysisSource;
}

export interface AnalysisListItem extends AnalysisResponse {
  readonly created_at: string;
}

export interface AnalysisListResponse {
  readonly schema_version: "1.0";
  readonly analyses: readonly AnalysisListItem[];
}

export interface MeResponse {
  readonly schema_version: "1.0";
  readonly memberships: ReadonlyArray<{
    readonly team: { readonly id: string; readonly name: string };
    readonly role: string;
  }>;
}

export type LocalDeviceState =
  | "connected"
  | "disconnected"
  | "multiple"
  | "unauthorized"
  | "unavailable";

export interface LocalDeviceStatusResponse {
  readonly schema_version: "1.0";
  readonly state: LocalDeviceState;
  readonly device: {
    readonly serial: string;
    readonly manufacturer: string;
    readonly model: string;
    readonly name: string;
    readonly os: string;
    readonly api_level: number | null;
  } | null;
}

interface UploadSlot {
  readonly schema_version: "1.0";
  readonly upload: {
    readonly state: "pending" | "finalized";
    readonly upload_id: string;
    readonly artifact_id?: string;
    readonly artifact_kind: TraceInputKind;
    readonly mime: string;
    readonly size: number;
    readonly sha256_b64: string;
    readonly put_url?: string;
    readonly required_headers?: Record<string, string>;
  };
}

interface InputDescriptor {
  readonly kind: TraceInputKind;
  readonly file: File;
  readonly mime: string;
  readonly size: number;
  readonly sha256_b64: string;
}

export class PerfPilotApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId: string | null;

  constructor(
    code: string,
    message: string,
    retryable: boolean,
    requestId: string | null,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "PerfPilotApiError";
    this.code = code;
    this.retryable = retryable;
    this.requestId = requestId;
  }
}

export interface PerfPilotClient {
  readonly fetcher: typeof globalThis.fetch;
  device(signal?: AbortSignal): Promise<LocalDeviceStatusResponse>;
  csrf(signal?: AbortSignal): Promise<string>;
  me(signal?: AbortSignal): Promise<MeResponse>;
  createTrace(
    teamId: string,
    profile: TraceProfile,
    question: string | null,
    inputs: readonly InputDescriptor[],
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<AnalysisResponse>;
  reserveInput(
    teamId: string,
    analysisId: string,
    input: InputDescriptor,
    signal?: AbortSignal,
  ): Promise<UploadSlot>;
  finalizeInput(
    teamId: string,
    analysisId: string,
    input: InputDescriptor,
    uploadId: string,
    signal?: AbortSignal,
  ): Promise<UploadSlot>;
  putInput(slot: UploadSlot, input: InputDescriptor, signal?: AbortSignal): Promise<void>;
  analyses(teamId: string, limit?: number, signal?: AbortSignal): Promise<AnalysisListResponse>;
  activeAnalyses(
    teamId: string,
    limit?: number,
    signal?: AbortSignal,
  ): Promise<AnalysisListResponse>;
  analysis(teamId: string, analysisId: string, signal?: AbortSignal): Promise<AnalysisResponse>;
  cancelAnalysis(
    teamId: string,
    analysisId: string,
    signal?: AbortSignal,
  ): Promise<AnalysisResponse>;
  report(teamId: string, analysisId: string, signal?: AbortSignal): Promise<AnalysisReport>;
  createSynthesisRun(
    teamId: string,
    analysisId: string,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<SynthesisRunResponse>;
}

interface ClientOptions {
  readonly fetcher?: typeof globalThis.fetch;
}

function aborted(signal?: AbortSignal): void {
  if (signal?.aborted) {
    throw signal.reason ?? new DOMException("操作已取消", "AbortError");
  }
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

export async function sha256Base64(file: File, signal?: AbortSignal): Promise<string> {
  if (!(file instanceof File) || file.size <= 0 || file.size > MAX_UPLOAD_BYTES) {
    throw new PerfPilotApiError("invalid_file", "文件为空或超过大小限制", false, null);
  }
  aborted(signal);
  const digest = sha256.create();
  const reader = file.stream().getReader();
  try {
    while (true) {
      aborted(signal);
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      for (let offset = 0; offset < value.byteLength; offset += HASH_CHUNK_BYTES) {
        aborted(signal);
        digest.update(value.subarray(offset, Math.min(value.byteLength, offset + HASH_CHUNK_BYTES)));
      }
    }
  } catch (error) {
    try {
      await reader.cancel(error);
    } catch {
      // Keep the original hashing or cancellation error.
    }
    throw error;
  } finally {
    reader.releaseLock();
  }
  aborted(signal);
  return bytesToBase64(digest.digest());
}

function fallbackMime(kind: TraceInputKind, filename: string): string {
  const lower = filename.toLowerCase();
  if (kind === "trace") return "application/octet-stream";
  if (kind === "apk") return "application/vnd.android.package-archive";
  if (kind === "mapping" || kind === "log") return "text/plain";
  if (lower.endsWith(".zip")) return "application/zip";
  if (lower.endsWith(".tar.gz") || lower.endsWith(".tgz")) return "application/gzip";
  if (lower.endsWith(".tar")) return "application/x-tar";
  if (lower.endsWith(".json")) return "application/json";
  return "application/octet-stream";
}

function inputMime(selection: TraceFileSelection): string {
  if (selection.kind === "trace") {
    return selection.file.type === "application/x-perfetto-trace"
      ? selection.file.type
      : "application/octet-stream";
  }
  return MIME.test(selection.file.type)
    ? selection.file.type
    : fallbackMime(selection.kind, selection.file.name);
}

async function readJson(response: Response): Promise<unknown> {
  const declared = response.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_JSON_BYTES)) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  if (response.body === null) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_JSON_BYTES) {
        await reader.cancel();
        throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const encoded = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    encoded.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(encoded);
  } catch {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function validUploadUrl(value: string): boolean {
  try {
    const url = new URL(value);
    if (url.username || url.password || url.hash) return false;
    if (url.protocol === "https:") return true;
    const loopback =
      url.hostname === "localhost" ||
      url.hostname === "127.0.0.1" ||
      url.hostname === "[::1]";
    return (
      url.protocol === "http:" &&
      loopback &&
      url.pathname.startsWith("/local/v1/uploads/")
    );
  } catch {
    return false;
  }
}

function exactKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  const allowedKeys = new Set(allowed);
  return Object.keys(value).every((key) => allowedKeys.has(key));
}

function stringArray(value: unknown, maximum: number, minimum = 0): value is string[] {
  return (
    Array.isArray(value) &&
    value.length >= minimum &&
    value.length <= maximum &&
    value.every((item) => typeof item === "string")
  );
}

function validFailure(value: unknown): value is AnalysisFailure {
  return (
    object(value) &&
    exactKeys(value, ["code", "message", "retryable"]) &&
    typeof value.code === "string" &&
    typeof value.message === "string" &&
    typeof value.retryable === "boolean"
  );
}

const STAGE_ORDER: readonly AnalysisStage["stage"][] = [
  "input_validation",
  "smartperfetto",
  "perfpilot_ai",
  "report",
];
const STAGE_STATES: readonly AnalysisStageState[] = [
  "pending",
  "running",
  "completed",
  "failed",
  "canceled",
  "not_requested",
];

function validStages(value: unknown): value is AnalysisStage[] {
  return (
    Array.isArray(value) &&
    value.length === STAGE_ORDER.length &&
    value.every(
      (item, index) =>
        object(item) &&
        exactKeys(item, ["stage", "state", "failure"]) &&
        item.stage === STAGE_ORDER[index] &&
        STAGE_STATES.includes(item.state as AnalysisStageState) &&
        (item.failure === null || validFailure(item.failure)),
    )
  );
}

const AI_ROUND_ROLES: readonly AnalysisAiRound["role"][] = [
  "extract",
  "review",
  "finalize",
];

function validAiRounds(value: unknown): value is AnalysisAiRound[] {
  return (
    Array.isArray(value) &&
    value.length === 3 &&
    value.every(
      (item, index) =>
        object(item) &&
        exactKeys(item, ["round", "role", "state", "attempts"]) &&
        item.round === index + 1 &&
        item.role === AI_ROUND_ROLES[index] &&
        ["pending", "running", "completed", "failed"].includes(String(item.state)) &&
        Number.isSafeInteger(item.attempts) &&
        Number(item.attempts) >= 0,
    )
  );
}

function validAnalysisSource(value: unknown): value is AnalysisSource {
  return (
    object(value) &&
    exactKeys(value, ["engine", "rounds", "verification", "session_id", "run_id"]) &&
    value.engine === "smartperfetto" &&
    (value.rounds === null ||
      (Number.isSafeInteger(value.rounds) && Number(value.rounds) >= 0)) &&
    ["passed", "failed", "unknown"].includes(String(value.verification)) &&
    (value.session_id === null || typeof value.session_id === "string") &&
    (value.run_id === null || typeof value.run_id === "string")
  );
}

const PRIVATE_TRANSPORT_KEYS = new Set([
  "credential_reference",
  "endpoint",
  "object_key",
  "provider_endpoint",
  "put_url",
  "required_headers",
  "signed_url",
  "version_id",
]);

function containsPrivateTransportData(value: unknown): boolean {
  const pending: unknown[] = [value];
  while (pending.length > 0) {
    const current = pending.pop();
    if (typeof current === "string") {
      if (/x-amz-(?:algorithm|credential|signature)=/i.test(current)) return true;
      continue;
    }
    if (Array.isArray(current)) {
      pending.push(...current);
      continue;
    }
    if (!object(current)) continue;
    for (const [key, nested] of Object.entries(current)) {
      if (PRIVATE_TRANSPORT_KEYS.has(key.toLowerCase())) return true;
      pending.push(nested);
    }
  }
  return false;
}

function validReportMetric(value: unknown): boolean {
  return (
    object(value) &&
    exactKeys(value, [
      "metric_id",
      "name",
      "status",
      "numeric_value",
      "unit",
      "definition",
      "threshold",
      "sample_ids",
    ]) &&
    typeof value.metric_id === "string" &&
    typeof value.name === "string" &&
    ["available", "insufficient_data", "unavailable", "invalid_capture"].includes(
      String(value.status),
    ) &&
    (value.numeric_value === null || typeof value.numeric_value === "number") &&
    (value.unit === null || typeof value.unit === "string") &&
    typeof value.definition === "string"
  );
}

function validReportFinding(value: unknown): boolean {
  return (
    object(value) &&
    exactKeys(value, [
      "finding_id",
      "rule_id",
      "kind",
      "status",
      "severity",
      "confidence",
      "confidence_ceiling",
      "title",
      "summary",
      "evidence_ids",
      "exclusions",
      "recommendation",
      "retest",
    ]) &&
    typeof value.finding_id === "string" &&
    typeof value.title === "string" &&
    typeof value.summary === "string" &&
    ["critical", "warning", "healthy", "informational"].includes(String(value.severity)) &&
    ["high", "medium", "low", "none"].includes(String(value.confidence)) &&
    stringArray(value.evidence_ids, 20)
  );
}

function validReportEvidence(value: unknown): boolean {
  return (
    object(value) &&
    exactKeys(value, [
      "evidence_id",
      "source",
      "query_id",
      "interval_start_ns",
      "interval_end_ns",
      "artifact_id",
      "fields",
    ]) &&
    typeof value.evidence_id === "string" &&
    typeof value.source === "string" &&
    typeof value.query_id === "string" &&
    (value.interval_start_ns === null || typeof value.interval_start_ns === "number") &&
    (value.interval_end_ns === null || typeof value.interval_end_ns === "number") &&
    object(value.fields)
  );
}

function validReportBundle(value: unknown): boolean {
  return (
    object(value) &&
    exactKeys(value, [
      "schema_version",
      "bundle_id",
      "scenario_job_id",
      "scenario_type",
      "bundle_state",
      "valid_measurement",
      "validity_reasons",
      "sample_ids",
      "generated_at",
      "metrics",
      "findings",
      "evidence",
      "artifacts",
      "trace_health",
      "trace_capabilities",
      "provenance",
    ]) &&
    Array.isArray(value.metrics) &&
    value.metrics.every(validReportMetric) &&
    Array.isArray(value.findings) &&
    value.findings.every(validReportFinding) &&
    Array.isArray(value.evidence) &&
    value.evidence.every(validReportEvidence)
  );
}

function validScenarioReport(value: unknown): boolean {
  return (
    object(value) &&
    exactKeys(value, [
      "scenario_job_id",
      "scenario_type",
      "result_state",
      "device_group_id",
      "device_group_reason",
      "bundle",
      "failure",
    ]) &&
    typeof value.scenario_job_id === "string" &&
    ["startup", "scroll", "memory_cycle"].includes(String(value.scenario_type)) &&
    ["completed", "failed", "canceled"].includes(String(value.result_state)) &&
    (value.bundle === null || validReportBundle(value.bundle)) &&
    (value.failure === null || validFailure(value.failure))
  );
}

function validSynthesisOutput(value: unknown): value is SynthesisOutput {
  if (
    !object(value) ||
    !exactKeys(value, [
      "schema_version",
      "executive_summary",
      "top_findings",
      "recommendations",
      "retest_plan",
      "limitations",
    ]) ||
    value.schema_version !== "1.0" ||
    typeof value.executive_summary !== "string" ||
    !Array.isArray(value.top_findings) ||
    value.top_findings.length > 5 ||
    !Array.isArray(value.recommendations) ||
    value.recommendations.length > 10 ||
    !Array.isArray(value.retest_plan) ||
    value.retest_plan.length > 5 ||
    !Array.isArray(value.limitations) ||
    value.limitations.length > 20
  ) {
    return false;
  }
  const findingsValid = value.top_findings.every(
    (item) =>
      object(item) &&
      exactKeys(item, ["finding_id", "evidence_ids", "user_impact"]) &&
      typeof item.finding_id === "string" &&
      stringArray(item.evidence_ids, 20, 1) &&
      typeof item.user_impact === "string",
  );
  const recommendationsValid = value.recommendations.every(
    (item) =>
      object(item) &&
      exactKeys(item, [
        "priority",
        "title",
        "action",
        "expected_effect",
        "finding_ids",
        "evidence_ids",
      ]) &&
      ["p0", "p1", "p2", "p3"].includes(String(item.priority)) &&
      typeof item.title === "string" &&
      typeof item.action === "string" &&
      typeof item.expected_effect === "string" &&
      stringArray(item.finding_ids, 20, 1) &&
      stringArray(item.evidence_ids, 20, 1),
  );
  const retestsValid = value.retest_plan.every(
    (item) =>
      object(item) &&
      exactKeys(item, [
        "mode",
        "scenario_type",
        "metric_ids",
        "limitation_ids",
        "steps",
        "success_condition",
        "failure_condition",
      ]) &&
      ["verify_metric", "collect_evidence"].includes(String(item.mode)) &&
      ["startup", "scroll", "memory_cycle"].includes(String(item.scenario_type)) &&
      stringArray(item.metric_ids, 20) &&
      stringArray(item.limitation_ids, 20) &&
      typeof item.steps === "string",
  );
  const limitationsValid = value.limitations.every(
    (item) =>
      object(item) &&
      exactKeys(item, ["limitation_id", "summary"]) &&
      typeof item.limitation_id === "string" &&
      typeof item.summary === "string",
  );
  return findingsValid && recommendationsValid && retestsValid && limitationsValid;
}

function validSynthesisProvenance(value: unknown): value is SynthesisProvenance {
  return (
    object(value) &&
    exactKeys(value, [
      "provider_protocol",
      "provider_name",
      "model",
      "prompt_template_version",
      "prompt_template_sha256_b64",
      "normalizer_version",
      "report_worker_image_digest",
      "projection_artifact_id",
      "projection_sha256_b64",
      "generated_at",
      "prompt_tokens",
      "completion_tokens",
      "total_tokens",
      "generation",
    ]) &&
    typeof value.provider_protocol === "string" &&
    typeof value.provider_name === "string" &&
    typeof value.model === "string" &&
    typeof value.prompt_template_version === "string" &&
    typeof value.normalizer_version === "string" &&
    typeof value.generated_at === "string" &&
    Number.isInteger(value.prompt_tokens) &&
    Number.isInteger(value.completion_tokens) &&
    Number.isInteger(value.total_tokens) &&
    Number.isInteger(value.generation)
  );
}

function analysisReportResponse(value: unknown): AnalysisReport {
  if (
    !object(value) ||
    containsPrivateTransportData(value) ||
    !exactKeys(value, [
      "schema_version",
      "analysis_id",
      "analysis_mode",
      "state",
      "report_version",
      "generated_at",
      "scenario_reports",
      "synthesis",
    ]) ||
    value.schema_version !== "1.1" ||
    value.analysis_mode !== "trace_upload" ||
    typeof value.analysis_id !== "string" ||
    !["completed", "partially_completed", "failed", "canceled"].includes(
      String(value.state),
    ) ||
    !Number.isSafeInteger(value.report_version) ||
    Number(value.report_version) < 1 ||
    typeof value.generated_at !== "string" ||
    !Array.isArray(value.scenario_reports) ||
    value.scenario_reports.length < 1 ||
    value.scenario_reports.length > 3 ||
    !value.scenario_reports.every(validScenarioReport) ||
    !object(value.synthesis) ||
    !exactKeys(value.synthesis, [
      "state",
      "output",
      "synthesis_artifact_id",
      "failure_code",
      "provenance",
    ])
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  const synthesis = value.synthesis;
  const completed =
    synthesis.state === "completed" &&
    validSynthesisOutput(synthesis.output) &&
    typeof synthesis.synthesis_artifact_id === "string" &&
    synthesis.failure_code === null &&
    validSynthesisProvenance(synthesis.provenance);
  const failed =
    synthesis.state === "failed" &&
    synthesis.output === null &&
    synthesis.synthesis_artifact_id === null &&
    typeof synthesis.failure_code === "string" &&
    synthesis.provenance === null;
  if (!completed && !failed) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as AnalysisReport;
}

function synthesisRunResponse(value: unknown): SynthesisRunResponse {
  if (
    !object(value) ||
    !exactKeys(value, ["schema_version", "analysis_id", "generation", "state"]) ||
    value.schema_version !== "1.0" ||
    typeof value.analysis_id !== "string" ||
    !Number.isSafeInteger(value.generation) ||
    Number(value.generation) < 1 ||
    value.state !== "queued"
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as SynthesisRunResponse;
}

function localDeviceResponse(value: unknown): LocalDeviceStatusResponse {
  const states: readonly LocalDeviceState[] = [
    "connected",
    "disconnected",
    "multiple",
    "unauthorized",
    "unavailable",
  ];
  if (
    !object(value) ||
    !exactKeys(value, ["schema_version", "state", "device"]) ||
    value.schema_version !== "1.0" ||
    !states.includes(value.state as LocalDeviceState)
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  if (value.state !== "connected") {
    if (value.device !== null) {
      throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
    }
    return value as unknown as LocalDeviceStatusResponse;
  }
  if (
    !object(value.device) ||
    !exactKeys(value.device, [
      "serial",
      "manufacturer",
      "model",
      "name",
      "os",
      "api_level",
    ]) ||
    typeof value.device.serial !== "string" ||
    typeof value.device.manufacturer !== "string" ||
    typeof value.device.model !== "string" ||
    typeof value.device.name !== "string" ||
    typeof value.device.os !== "string" ||
    (value.device.api_level !== null && !Number.isSafeInteger(value.device.api_level))
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as LocalDeviceStatusResponse;
}

function analysisResponse(value: unknown): AnalysisResponse {
  const hasAiRounds = object(value) && "ai_rounds" in value;
  const hasSource = object(value) && "source_analysis" in value;
  const createdAt = object(value) ? value.created_at : undefined;
  const cancelRequestedAt = object(value) ? value.cancel_requested_at : undefined;
  if (
    !object(value) ||
    value.schema_version !== "1.0" ||
    value.analysis_mode !== "trace_upload" ||
    typeof value.analysis_id !== "string" ||
    typeof value.team_id !== "string" ||
    !["auto", "startup", "scroll"].includes(String(value.analysis_profile)) ||
    !Array.isArray(value.input_uploads) ||
    !ANALYSIS_STATES.has(value.state as AnalysisState) ||
    typeof value.version !== "number" ||
    typeof value.report_available !== "boolean" ||
    (createdAt !== undefined &&
      (typeof createdAt !== "string" ||
        createdAt.length > 64 ||
        Number.isNaN(Date.parse(createdAt)))) ||
    (cancelRequestedAt !== undefined &&
      cancelRequestedAt !== null &&
      (typeof cancelRequestedAt !== "string" ||
        cancelRequestedAt.length > 64 ||
        Number.isNaN(Date.parse(cancelRequestedAt)))) ||
    !validStages(value.stages) ||
    hasAiRounds !== hasSource ||
    (hasAiRounds && !validAiRounds(value.ai_rounds)) ||
    (hasSource && !validAnalysisSource(value.source_analysis))
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as AnalysisResponse;
}

function analysisListResponse(
  value: unknown,
  context: {
    readonly teamId: string;
    readonly limit: number;
    readonly filter: "report" | "active";
  },
): AnalysisListResponse {
  const { teamId, limit, filter } = context;
  if (
    !object(value) ||
    !exactKeys(value, ["schema_version", "analyses"]) ||
    value.schema_version !== "1.0" ||
    !Array.isArray(value.analyses) ||
    value.analyses.length > limit
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  const ids = new Set<string>();
  for (const rawAnalysis of value.analyses) {
    const parsed = analysisResponse(rawAnalysis);
    const createdAt = object(rawAnalysis) ? rawAnalysis.created_at : null;
    if (
      parsed.team_id !== teamId ||
      (filter === "report" && !parsed.report_available) ||
      (filter === "active" && !ACTIVE_ANALYSIS_STATES.has(parsed.state)) ||
      typeof createdAt !== "string" ||
      createdAt.length > 64 ||
      Number.isNaN(Date.parse(createdAt)) ||
      ids.has(parsed.analysis_id)
    ) {
      throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
    }
    ids.add(parsed.analysis_id);
  }
  return value as unknown as AnalysisListResponse;
}

function uploadSlot(value: unknown): UploadSlot {
  if (
    !object(value) ||
    value.schema_version !== "1.0" ||
    !object(value.upload) ||
    typeof value.upload.upload_id !== "string" ||
    typeof value.upload.artifact_kind !== "string" ||
    typeof value.upload.mime !== "string" ||
    typeof value.upload.size !== "number" ||
    typeof value.upload.sha256_b64 !== "string"
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as UploadSlot;
}

export function createPerfPilotClient(options: ClientOptions = {}): PerfPilotClient {
  const fetcher = options.fetcher ?? globalThis.fetch.bind(globalThis);
  let csrfToken: string | null = null;

  async function requestJson(
    path: string,
    init: RequestInit = {},
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<unknown> {
    if (!path.startsWith(API_PREFIX) || path.startsWith("//")) {
      throw new PerfPilotApiError("invalid_api_request", "请求地址无效", false, null);
    }
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers);
    headers.set("accept", "application/json");
    if (init.body !== undefined && init.body !== null) {
      headers.set("content-type", "application/json");
    }
    if (csrfToken !== null) {
      headers.set("x-csrf-token", csrfToken);
    } else if (!["GET", "HEAD"].includes(method)) {
      throw new PerfPilotApiError("csrf_unavailable", "会话尚未初始化", true, null);
    }
    if (idempotencyKey !== undefined) {
      headers.set("idempotency-key", idempotencyKey);
    }
    aborted(signal);
    let response: Response;
    try {
      response = await fetcher(path, {
        ...init,
        headers,
        credentials: "same-origin",
        redirect: "error",
        signal,
      });
    } catch (error) {
      aborted(signal);
      throw new PerfPilotApiError("network_unavailable", "网络连接不可用", true, null, {
        cause: error,
      });
    }
    const payload = await readJson(response);
    if (!response.ok) {
      const error = object(payload) && object(payload.error) ? payload.error : null;
      throw new PerfPilotApiError(
        typeof error?.code === "string" ? error.code : "invalid_api_response",
        typeof error?.message === "string" ? error.message : "服务返回内容无效",
        error?.retryable === true,
        typeof error?.request_id === "string" ? error.request_id : null,
      );
    }
    return payload;
  }

  return {
    fetcher,
    async device(signal) {
      return localDeviceResponse(await requestJson("/api/v1/device", {}, signal));
    },
    async csrf(signal) {
      const payload = await requestJson("/api/v1/auth/csrf", {}, signal);
      if (!object(payload) || payload.schema_version !== "1.0" || typeof payload.csrf_token !== "string") {
        throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
      }
      csrfToken = payload.csrf_token;
      return csrfToken;
    },
    async me(signal) {
      const payload = await requestJson("/api/v1/me", {}, signal);
      if (!object(payload) || payload.schema_version !== "1.0" || !Array.isArray(payload.memberships)) {
        throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
      }
      return payload as unknown as MeResponse;
    },
    async createTrace(teamId, profile, question, inputs, idempotencyKey, signal) {
      const payload = await requestJson(
        `/api/v1/teams/${encodeURIComponent(teamId)}/analyses`,
        {
          method: "POST",
          body: JSON.stringify({
            schema_version: "1.0",
            analysis_mode: "trace_upload",
            analysis_profile: profile,
            question,
            inputs: inputs.map((input) => ({
              kind: input.kind,
              mime: input.mime,
              size: input.size,
              sha256_b64: input.sha256_b64,
            })),
          }),
        },
        signal,
        idempotencyKey,
      );
      return analysisResponse(payload);
    },
    async reserveInput(teamId, analysisId, input, signal) {
      const payload = await requestJson(
        `/api/v1/teams/${encodeURIComponent(teamId)}/analyses/${encodeURIComponent(analysisId)}/uploads`,
        {
          method: "POST",
          body: JSON.stringify({
            artifact_kind: input.kind,
            mime: input.mime,
            size: input.size,
            sha256_b64: input.sha256_b64,
          }),
        },
        signal,
        `input-${input.kind}`,
      );
      return uploadSlot(payload);
    },
    async finalizeInput(teamId, analysisId, input, uploadId, signal) {
      const payload = await requestJson(
        `/api/v1/teams/${encodeURIComponent(teamId)}/analyses/${encodeURIComponent(analysisId)}/finalize-upload`,
        {
          method: "POST",
          body: JSON.stringify({
            upload_id: uploadId,
            sha256_b64: input.sha256_b64,
            size: input.size,
          }),
        },
        signal,
      );
      return uploadSlot(payload);
    },
    async putInput(slot, input, signal) {
      const upload = slot.upload;
      if (
        upload.state !== "pending" ||
        upload.artifact_kind !== input.kind ||
        upload.mime !== input.mime ||
        upload.size !== input.size ||
        upload.sha256_b64 !== input.sha256_b64 ||
        typeof upload.put_url !== "string" ||
        !validUploadUrl(upload.put_url) ||
        !object(upload.required_headers)
      ) {
        throw new PerfPilotApiError("invalid_upload_authorization", "上传授权无效", false, null);
      }
      const required = new Headers(upload.required_headers);
      if (
        required.get("content-type") !== input.mime ||
        required.get("x-amz-checksum-sha256") !== input.sha256_b64
      ) {
        throw new PerfPilotApiError("invalid_upload_authorization", "上传授权无效", false, null);
      }
      aborted(signal);
      let response: Response;
      try {
        response = await fetcher(upload.put_url, {
          method: "PUT",
          headers: required,
          body: input.file,
          credentials: "omit",
          redirect: "error",
          signal,
        });
      } catch (error) {
        aborted(signal);
        throw new PerfPilotApiError("object_upload_failed", "文件上传失败", true, null, {
          cause: error,
        });
      }
      if (!response.ok) {
        throw new PerfPilotApiError(
          response.status === 401 || response.status === 403
            ? "upload_authorization_expired"
            : "object_upload_failed",
          "文件上传失败",
          response.status >= 500,
          null,
        );
      }
    },
    async analysis(teamId, analysisId, signal) {
      return analysisResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/analyses/${encodeURIComponent(analysisId)}`,
          {},
          signal,
        ),
      );
    },
    async analyses(teamId, limit = 1, signal) {
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 20) {
        throw new PerfPilotApiError("invalid_api_request", "请求参数无效", false, null);
      }
      return analysisListResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/analyses?report_available=true&limit=${limit}`,
          {},
          signal,
        ),
        { teamId, limit, filter: "report" },
      );
    },
    async activeAnalyses(teamId, limit = 1, signal) {
      if (!Number.isSafeInteger(limit) || limit < 1 || limit > 20) {
        throw new PerfPilotApiError("invalid_api_request", "请求参数无效", false, null);
      }
      return analysisListResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/analyses?status=active&limit=${limit}`,
          {},
          signal,
        ),
        { teamId, limit, filter: "active" },
      );
    },
    async cancelAnalysis(teamId, analysisId, signal) {
      const canceled = analysisResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/analyses/${encodeURIComponent(analysisId)}/cancel`,
          { method: "POST" },
          signal,
        ),
      );
      if (canceled.analysis_id !== analysisId || canceled.team_id !== teamId) {
        throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
      }
      return canceled;
    },
    async report(teamId, analysisId, signal) {
      const report = analysisReportResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/analyses/${encodeURIComponent(analysisId)}/report`,
          {},
          signal,
        ),
      );
      if (report.analysis_id !== analysisId) {
        throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
      }
      return report;
    },
    async createSynthesisRun(teamId, analysisId, idempotencyKey, signal) {
      const run = synthesisRunResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/analyses/${encodeURIComponent(analysisId)}/synthesis-runs`,
          { method: "POST" },
          signal,
          idempotencyKey,
        ),
      );
      if (run.analysis_id !== analysisId) {
        throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
      }
      return run;
    },
  };
}

async function mapConcurrent<T, R>(
  values: readonly T[],
  limit: number,
  task: (value: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(values.length);
  let cursor = 0;
  async function worker(): Promise<void> {
    while (cursor < values.length) {
      const index = cursor++;
      results[index] = await task(values[index], index);
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, values.length) }, worker));
  return results;
}

export type TraceSubmissionPhase =
  | "session"
  | "hashing"
  | "creating"
  | "uploading"
  | "submitted";

export interface SubmitTraceInput {
  readonly profile: TraceProfile;
  readonly question?: string;
  readonly files: readonly TraceFileSelection[];
  readonly signal?: AbortSignal;
  readonly onProgress?: (phase: TraceSubmissionPhase, detail?: string) => void;
}

export interface SubmitTraceDependencies {
  readonly client?: PerfPilotClient;
  readonly randomUUID?: () => string;
  readonly sleep?: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
}

export interface SubmittedTraceAnalysis {
  readonly teamId: string;
  readonly analysis: AnalysisResponse;
}

export async function enqueueTraceAnalysis(
  submission: SubmitTraceInput,
  dependencies: SubmitTraceDependencies = {},
): Promise<SubmittedTraceAnalysis> {
  const client = dependencies.client ?? createPerfPilotClient();
  const randomUUID = dependencies.randomUUID ?? (() => crypto.randomUUID());
  const { signal, onProgress } = submission;
  if (!["auto", "startup", "scroll"].includes(submission.profile)) {
    throw new PerfPilotApiError("invalid_profile", "请选择分析重点", false, null);
  }
  const kinds = new Set<TraceInputKind>();
  for (const selection of submission.files) {
    if (kinds.has(selection.kind)) {
      throw new PerfPilotApiError("duplicate_input", "同类文件只能选择一个", false, null);
    }
    kinds.add(selection.kind);
  }
  if (!kinds.has("trace")) {
    throw new PerfPilotApiError("trace_required", "请选择 Trace 文件", false, null);
  }
  const question = submission.question?.trim() || null;
  if (question !== null && Array.from(question).length > 2_000) {
    throw new PerfPilotApiError("question_too_long", "补充问题不能超过 2000 字", false, null);
  }

  onProgress?.("session");
  await client.csrf(signal);
  const me = await client.me(signal);
  const teamId = me.memberships[0]?.team.id;
  if (!teamId) {
    throw new PerfPilotApiError("team_required", "当前账号尚未加入团队", false, null);
  }

  onProgress?.("hashing");
  const descriptors = await mapConcurrent(submission.files, 2, async (selection) => ({
    kind: selection.kind,
    file: selection.file,
    mime: inputMime(selection),
    size: selection.file.size,
    sha256_b64: await sha256Base64(selection.file, signal),
  }));
  const order: TraceInputKind[] = [
    "trace",
    "memory_evidence",
    "apk",
    "source_archive",
    "mapping",
    "native_symbols",
    "log",
  ];
  descriptors.sort((left, right) => order.indexOf(left.kind) - order.indexOf(right.kind));

  onProgress?.("creating");
  const created = await client.createTrace(
    teamId,
    submission.profile,
    question,
    descriptors,
    randomUUID(),
    signal,
  );
  if (created.team_id !== teamId) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }

  onProgress?.("uploading");
  await mapConcurrent(descriptors, 2, async (input) => {
    onProgress?.("uploading", input.kind);
    const slot = await client.reserveInput(teamId, created.analysis_id, input, signal);
    if (slot.upload.state === "finalized") {
      return;
    }
    await client.putInput(slot, input, signal);
    await client.finalizeInput(teamId, created.analysis_id, input, slot.upload.upload_id, signal);
  });

  const current = await client.analysis(teamId, created.analysis_id, signal);
  onProgress?.("submitted", created.analysis_id);
  return { teamId, analysis: current };
}

export const submitTraceAnalysis = enqueueTraceAnalysis;
