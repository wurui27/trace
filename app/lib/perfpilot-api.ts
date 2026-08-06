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
export type AnalysisMode = "device" | "trace_upload" | "memory_upload";
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
  readonly scenario_type: "cold_start" | "startup" | "scroll" | "memory_cycle";
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
  readonly schema_version: "1.0" | "1.1";
  readonly analysis_id: string;
  readonly analysis_mode: "device" | "trace_upload";
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
      }
    | {
        readonly state: "not_requested";
        readonly output: null;
        readonly synthesis_artifact_id: null;
        readonly failure_code: null;
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
  readonly analysis_mode: AnalysisMode;
  readonly analysis_profile?: TraceProfile;
  readonly question?: string | null;
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
  readonly device_id?: string;
  readonly application_version_id?: string | null;
  readonly application_metadata?: ApplicationMetadata | null;
  readonly apk_upload?: UploadPayload | null;
  readonly scenarios?: readonly AnalysisScenario[];
  readonly sample_verdict_counts?: SampleVerdictCounts;
  readonly active_lease?: ActiveAnalysisLease | null;
  readonly started_at?: string | null;
  readonly completed_at?: string | null;
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

export interface ApplicationMetadata {
  readonly package_name: string;
  readonly version_name: string | null;
  readonly version_code: number;
  readonly launch_activity: string | null;
  readonly min_sdk: number | null;
  readonly target_sdk: number | null;
  readonly supported_abis: readonly string[];
  readonly has_native_libraries: boolean;
}

export interface SampleVerdictCounts {
  readonly valid: number;
  readonly invalid: number;
  readonly pending: number;
  readonly validation_error: number;
  readonly total: number;
}

export interface AnalysisScenario {
  readonly scenario_job_id: string | null;
  readonly scenario_type: "cold_start" | "scroll" | "memory_cycle";
  readonly state:
    | "awaiting_input"
    | "queued"
    | "scheduled"
    | "running"
    | "analyzing"
    | "completed"
    | "failed"
    | "canceled";
  readonly version: number | null;
  readonly device_group_id: string | null;
  readonly sample_verdict_counts: SampleVerdictCounts;
  readonly started_at: string | null;
  readonly completed_at: string | null;
  readonly failure: AnalysisFailure | null;
}

export interface ActiveAnalysisLease {
  readonly lease_id: string;
  readonly device_id: string;
  readonly state: "active";
  readonly expires_at: string;
}

export type AgentPlatform = "macos" | "windows" | "linux";
export type AgentState = "pending" | "online" | "offline" | "revoked";

export interface AgentView {
  readonly agent_id: string;
  readonly name: string;
  readonly platform: AgentPlatform | null;
  readonly agent_version: string | null;
  readonly hostname: string | null;
  readonly os_version: string | null;
  readonly state: AgentState;
  readonly last_heartbeat_at: string | null;
  readonly created_at: string;
  readonly updated_at: string;
}

export interface AgentListResponse {
  readonly schema_version: "1.0";
  readonly agents: readonly AgentView[];
}

export interface AgentRegistrationCodeResponse {
  readonly schema_version: "1.0";
  readonly agent_id: string;
  readonly registration_code: string;
  readonly expires_at: string;
}

export type RemoteDeviceState =
  | "ready"
  | "busy"
  | "unauthorized"
  | "booting"
  | "quarantined"
  | "offline";

export interface RemoteDeviceView {
  readonly device_id: string;
  readonly agent_id: string;
  readonly agent_name: string;
  readonly serial_suffix: string;
  readonly manufacturer: string | null;
  readonly model: string | null;
  readonly android_release: string | null;
  readonly api_level: number | null;
  readonly connection_type: "usb" | "wifi" | "unknown";
  readonly adb_state: "device" | "unauthorized" | "offline" | "booting";
  readonly state: RemoteDeviceState;
  readonly last_seen_at: string | null;
}

export interface RemoteDeviceListResponse {
  readonly schema_version: "1.0";
  readonly devices: readonly RemoteDeviceView[];
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

export interface UploadPayload {
  readonly state: "pending" | "finalized";
  readonly upload_id: string;
  readonly artifact_id?: string;
  readonly artifact_kind: TraceInputKind;
  readonly mime: string;
  readonly size: number;
  readonly sha256_b64: string;
  readonly expires_at?: string;
  readonly finalized_at?: string;
  readonly put_url?: string;
  readonly required_headers?: Record<string, string>;
}

interface UploadSlot {
  readonly schema_version: "1.0";
  readonly upload: UploadPayload;
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
  devices(teamId: string, signal?: AbortSignal): Promise<RemoteDeviceListResponse>;
  agents(teamId: string, signal?: AbortSignal): Promise<AgentListResponse>;
  createAgentRegistrationCode(
    teamId: string,
    name: string,
    signal?: AbortSignal,
  ): Promise<AgentRegistrationCodeResponse>;
  renameAgent(
    teamId: string,
    agentId: string,
    name: string,
    signal?: AbortSignal,
  ): Promise<AgentView>;
  revokeAgent(teamId: string, agentId: string, signal?: AbortSignal): Promise<AgentView>;
  createDeviceAnalysis(
    teamId: string,
    deviceId: string,
    apk: InputDescriptor,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<AnalysisResponse>;
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

interface RandomUuidSource {
  getRandomValues(target: Uint8Array): Uint8Array;
}

export function createRandomUuid(
  source: RandomUuidSource = globalThis.crypto,
): string {
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
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

function privateLanHostname(value: string): boolean {
  const hostname = value.toLowerCase().replace(/^\[|\]$/g, "");
  if (hostname === "localhost" || hostname === "::1") return true;
  if (hostname.startsWith("fc") || hostname.startsWith("fd") || hostname.startsWith("fe80:")) {
    return true;
  }
  const octets = hostname.split(".").map(Number);
  if (
    octets.length !== 4 ||
    octets.some((octet) => !Number.isInteger(octet) || octet < 0 || octet > 255)
  ) {
    return false;
  }
  return (
    octets[0] === 10 ||
    (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
    (octets[0] === 192 && octets[1] === 168) ||
    octets[0] === 127
  );
}

export function validUploadUrl(
  value: string,
  pageOrigin = typeof location === "undefined" ? undefined : location.origin,
): boolean {
  try {
    const url = new URL(value);
    if (url.username || url.password || url.hash) return false;
    if (url.protocol === "https:") return true;
    if (
      url.protocol !== "http:" ||
      !url.pathname.startsWith("/local/v1/uploads/") ||
      !privateLanHostname(url.hostname)
    ) {
      return false;
    }
    if (["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)) return true;
    if (pageOrigin === undefined) return false;
    const page = new URL(pageOrigin);
    return (
      page.protocol === "http:" &&
      privateLanHostname(page.hostname) &&
      page.hostname === url.hostname
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
    ["cold_start", "startup", "scroll", "memory_cycle"].includes(
      String(value.scenario_type),
    ) &&
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
    !["1.0", "1.1"].includes(String(value.schema_version)) ||
    !["device", "trace_upload"].includes(String(value.analysis_mode)) ||
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
    !value.scenario_reports.every(validScenarioReport)
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  if (value.schema_version === "1.0") {
    if (value.synthesis !== undefined) {
      throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
    }
    if (value.analysis_mode === "device") {
      const expected = ["cold_start", "scroll", "memory_cycle"];
      if (
        value.scenario_reports.length !== expected.length ||
        !value.scenario_reports.every(
          (scenario, index) =>
            object(scenario) && scenario.scenario_type === expected[index],
        )
      ) {
        throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
      }
    }
    return {
      ...value,
      synthesis: {
        state: "not_requested",
        output: null,
        synthesis_artifact_id: null,
        failure_code: null,
        provenance: null,
      },
    } as unknown as AnalysisReport;
  }
  if (
    !["trace_upload", "device"].includes(String(value.analysis_mode)) ||
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

function validDateTime(value: unknown, nullable = false): boolean {
  return (
    (nullable && value === null) ||
    (typeof value === "string" && value.length <= 64 && !Number.isNaN(Date.parse(value)))
  );
}

function validNullableString(value: unknown, maximum: number): boolean {
  return value === null || (typeof value === "string" && value.length <= maximum);
}

function agentView(value: unknown): AgentView {
  if (
    !object(value) ||
    !exactKeys(value, [
      "agent_id",
      "name",
      "platform",
      "agent_version",
      "hostname",
      "os_version",
      "state",
      "last_heartbeat_at",
      "created_at",
      "updated_at",
    ]) ||
    typeof value.agent_id !== "string" ||
    typeof value.name !== "string" ||
    value.name.length < 1 ||
    value.name.length > 200 ||
    ![null, "macos", "windows", "linux"].includes(value.platform as string | null) ||
    !validNullableString(value.agent_version, 64) ||
    !validNullableString(value.hostname, 200) ||
    !validNullableString(value.os_version, 128) ||
    !["pending", "online", "offline", "revoked"].includes(String(value.state)) ||
    !validDateTime(value.last_heartbeat_at, true) ||
    !validDateTime(value.created_at) ||
    !validDateTime(value.updated_at)
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as AgentView;
}

function agentListResponse(value: unknown): AgentListResponse {
  if (
    !object(value) ||
    !exactKeys(value, ["schema_version", "agents"]) ||
    value.schema_version !== "1.0" ||
    !Array.isArray(value.agents) ||
    value.agents.length > 256
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  const agents = value.agents.map(agentView);
  if (new Set(agents.map((agent) => agent.agent_id)).size !== agents.length) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return { schema_version: "1.0", agents };
}

function registrationCodeResponse(value: unknown): AgentRegistrationCodeResponse {
  if (
    !object(value) ||
    !exactKeys(value, ["schema_version", "agent_id", "registration_code", "expires_at"]) ||
    value.schema_version !== "1.0" ||
    typeof value.agent_id !== "string" ||
    typeof value.registration_code !== "string" ||
    !/^ppreg_[A-Za-z0-9_-]{43}$/.test(value.registration_code) ||
    !validDateTime(value.expires_at)
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as AgentRegistrationCodeResponse;
}

function agentMutationResponse(value: unknown): AgentView {
  if (
    !object(value) ||
    !exactKeys(value, ["schema_version", "agent"]) ||
    value.schema_version !== "1.0"
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return agentView(value.agent);
}

function normalizedAgentName(name: string): string {
  const normalized = name.trim();
  if (normalized.length < 1 || normalized.length > 200 || /[\u0000-\u001f\u007f]/.test(normalized)) {
    throw new PerfPilotApiError("invalid_agent_name", "请输入有效的 Agent 名称", false, null);
  }
  return normalized;
}

function remoteDeviceView(value: unknown): RemoteDeviceView {
  if (
    !object(value) ||
    !exactKeys(value, [
      "device_id",
      "agent_id",
      "agent_name",
      "serial_suffix",
      "manufacturer",
      "model",
      "android_release",
      "api_level",
      "connection_type",
      "adb_state",
      "state",
      "last_seen_at",
    ]) ||
    typeof value.device_id !== "string" ||
    typeof value.agent_id !== "string" ||
    typeof value.agent_name !== "string" ||
    value.agent_name.length < 1 ||
    value.agent_name.length > 200 ||
    typeof value.serial_suffix !== "string" ||
    !/^[!-~]{1,4}$/.test(value.serial_suffix) ||
    !validNullableString(value.manufacturer, 128) ||
    !validNullableString(value.model, 128) ||
    !validNullableString(value.android_release, 64) ||
    (value.api_level !== null &&
      (!Number.isSafeInteger(value.api_level) || Number(value.api_level) < 1)) ||
    !["usb", "wifi", "unknown"].includes(String(value.connection_type)) ||
    !["device", "unauthorized", "offline", "booting"].includes(String(value.adb_state)) ||
    !["ready", "busy", "unauthorized", "booting", "quarantined", "offline"].includes(
      String(value.state),
    ) ||
    !validDateTime(value.last_seen_at, true)
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as RemoteDeviceView;
}

function remoteDeviceListResponse(value: unknown): RemoteDeviceListResponse {
  if (
    !object(value) ||
    !exactKeys(value, ["schema_version", "devices"]) ||
    value.schema_version !== "1.0" ||
    !Array.isArray(value.devices) ||
    value.devices.length > 256
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  const devices = value.devices.map(remoteDeviceView);
  if (new Set(devices.map((device) => device.device_id)).size !== devices.length) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return { schema_version: "1.0", devices };
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

function validVerdictCounts(value: unknown): value is SampleVerdictCounts {
  if (
    !object(value) ||
    !exactKeys(value, ["valid", "invalid", "pending", "validation_error", "total"])
  ) {
    return false;
  }
  const counts = [
    value.valid,
    value.invalid,
    value.pending,
    value.validation_error,
    value.total,
  ];
  return (
    counts.every((count) => Number.isSafeInteger(count) && Number(count) >= 0) &&
    Number(value.valid) +
      Number(value.invalid) +
      Number(value.pending) +
      Number(value.validation_error) ===
      Number(value.total)
  );
}

function validApplicationMetadata(value: unknown): value is ApplicationMetadata {
  return (
    object(value) &&
    exactKeys(value, [
      "package_name",
      "version_name",
      "version_code",
      "launch_activity",
      "min_sdk",
      "target_sdk",
      "supported_abis",
      "has_native_libraries",
    ]) &&
    typeof value.package_name === "string" &&
    validNullableString(value.version_name, 255) &&
    Number.isSafeInteger(value.version_code) &&
    validNullableString(value.launch_activity, 512) &&
    (value.min_sdk === null || Number.isSafeInteger(value.min_sdk)) &&
    (value.target_sdk === null || Number.isSafeInteger(value.target_sdk)) &&
    stringArray(value.supported_abis, 32) &&
    typeof value.has_native_libraries === "boolean"
  );
}

function validUploadPayload(value: unknown): value is UploadPayload {
  if (
    !object(value) ||
    !exactKeys(value, [
      "state",
      "upload_id",
      "artifact_id",
      "artifact_kind",
      "mime",
      "size",
      "sha256_b64",
      "expires_at",
      "finalized_at",
      "put_url",
      "required_headers",
    ]) ||
    !["pending", "finalized"].includes(String(value.state)) ||
    typeof value.upload_id !== "string" ||
    typeof value.artifact_kind !== "string" ||
    typeof value.mime !== "string" ||
    !Number.isSafeInteger(value.size) ||
    Number(value.size) < 1 ||
    typeof value.sha256_b64 !== "string"
  ) {
    return false;
  }
  if (value.state === "pending") {
    const hasPutUrl = value.put_url !== undefined;
    const hasHeaders = value.required_headers !== undefined;
    return (
      value.artifact_id === undefined &&
      value.finalized_at === undefined &&
      validDateTime(value.expires_at) &&
      hasPutUrl === hasHeaders &&
      (!hasPutUrl ||
        (typeof value.put_url === "string" && object(value.required_headers)))
    );
  }
  return (
    typeof value.artifact_id === "string" &&
    value.expires_at === undefined &&
    value.put_url === undefined &&
    value.required_headers === undefined &&
    validDateTime(value.finalized_at)
  );
}

function validAnalysisScenario(value: unknown, expectedType: string): value is AnalysisScenario {
  return (
    object(value) &&
    exactKeys(value, [
      "scenario_job_id",
      "scenario_type",
      "state",
      "version",
      "device_group_id",
      "sample_verdict_counts",
      "started_at",
      "completed_at",
      "failure",
    ]) &&
    (value.scenario_job_id === null || typeof value.scenario_job_id === "string") &&
    value.scenario_type === expectedType &&
    [
      "awaiting_input",
      "queued",
      "scheduled",
      "running",
      "analyzing",
      "completed",
      "failed",
      "canceled",
    ].includes(String(value.state)) &&
    (value.version === null ||
      (Number.isSafeInteger(value.version) && Number(value.version) >= 1)) &&
    (value.device_group_id === null || typeof value.device_group_id === "string") &&
    validVerdictCounts(value.sample_verdict_counts) &&
    validDateTime(value.started_at, true) &&
    validDateTime(value.completed_at, true) &&
    (value.failure === null || validFailure(value.failure))
  );
}

function validActiveLease(value: unknown): value is ActiveAnalysisLease {
  return (
    object(value) &&
    exactKeys(value, ["lease_id", "device_id", "state", "expires_at"]) &&
    typeof value.lease_id === "string" &&
    typeof value.device_id === "string" &&
    value.state === "active" &&
    validDateTime(value.expires_at)
  );
}

function analysisResponse(value: unknown): AnalysisResponse {
  const hasAiRounds = object(value) && "ai_rounds" in value;
  const hasSource = object(value) && "source_analysis" in value;
  const createdAt = object(value) ? value.created_at : undefined;
  const cancelRequestedAt = object(value) ? value.cancel_requested_at : undefined;
  if (
    !object(value) ||
    value.schema_version !== "1.0" ||
    !["device", "trace_upload", "memory_upload"].includes(String(value.analysis_mode)) ||
    typeof value.analysis_id !== "string" ||
    typeof value.team_id !== "string" ||
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
    (value.failure !== null && !validFailure(value.failure))
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  if (value.analysis_mode === "trace_upload") {
    if (
      !["auto", "startup", "scroll"].includes(String(value.analysis_profile)) ||
      (value.question !== null && typeof value.question !== "string") ||
      !Array.isArray(value.input_uploads) ||
      !validStages(value.stages) ||
      hasAiRounds !== hasSource ||
      (hasAiRounds && !validAiRounds(value.ai_rounds)) ||
      (hasSource && !validAnalysisSource(value.source_analysis))
    ) {
      throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
    }
    return value as unknown as AnalysisResponse;
  }
  if (hasAiRounds || hasSource) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  if (value.analysis_mode === "device") {
    const scenarioTypes = ["cold_start", "scroll", "memory_cycle"] as const;
    if (
      typeof value.device_id !== "string" ||
      (value.application_version_id !== null &&
        typeof value.application_version_id !== "string") ||
      (value.application_metadata !== null &&
        !validApplicationMetadata(value.application_metadata)) ||
      !validUploadPayload(value.apk_upload) ||
      !Array.isArray(value.scenarios) ||
      value.scenarios.length !== scenarioTypes.length ||
      !value.scenarios.every((scenario, index) =>
        validAnalysisScenario(scenario, scenarioTypes[index]),
      ) ||
      !validVerdictCounts(value.sample_verdict_counts) ||
      (value.active_lease !== null && !validActiveLease(value.active_lease)) ||
      !validDateTime(value.started_at, true) ||
      !validDateTime(value.completed_at, true)
    ) {
      throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
    }
    return {
      ...value,
      stages: [],
      input_uploads: [],
    } as unknown as AnalysisResponse;
  }
  if (
    (value.application_version_id !== null && typeof value.application_version_id !== "string") ||
    !validApplicationMetadata(value.application_metadata) ||
    (value.question !== null && typeof value.question !== "string")
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return {
    ...value,
    stages: [],
    input_uploads: [],
  } as unknown as AnalysisResponse;
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
  const analyses: AnalysisListItem[] = [];
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
    analyses.push({ ...parsed, created_at: createdAt });
  }
  return { schema_version: "1.0", analyses };
}

function uploadSlot(value: unknown): UploadSlot {
  if (
    !object(value) ||
    value.schema_version !== "1.0" ||
    !validUploadPayload(value.upload)
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
    async devices(teamId, signal) {
      return remoteDeviceListResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/devices`,
          {},
          signal,
        ),
      );
    },
    async agents(teamId, signal) {
      return agentListResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/agents`,
          {},
          signal,
        ),
      );
    },
    async createAgentRegistrationCode(teamId, name, signal) {
      return registrationCodeResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/agents/registration-codes`,
          {
            method: "POST",
            body: JSON.stringify({
              schema_version: "1.0",
              name: normalizedAgentName(name),
            }),
          },
          signal,
        ),
      );
    },
    async renameAgent(teamId, agentId, name, signal) {
      return agentMutationResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/agents/${encodeURIComponent(agentId)}`,
          {
            method: "PATCH",
            body: JSON.stringify({
              schema_version: "1.0",
              name: normalizedAgentName(name),
            }),
          },
          signal,
        ),
      );
    },
    async revokeAgent(teamId, agentId, signal) {
      return agentMutationResponse(
        await requestJson(
          `/api/v1/teams/${encodeURIComponent(teamId)}/agents/${encodeURIComponent(agentId)}/revoke`,
          {
            method: "POST",
            body: JSON.stringify({ schema_version: "1.0" }),
          },
          signal,
        ),
      );
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
    async createDeviceAnalysis(teamId, deviceId, apk, idempotencyKey, signal) {
      const payload = await requestJson(
        `/api/v1/teams/${encodeURIComponent(teamId)}/analyses`,
        {
          method: "POST",
          body: JSON.stringify({
            schema_version: "1.0",
            analysis_mode: "device",
            device_id: deviceId,
            scenarios: ["cold_start", "scroll", "memory_cycle"],
            apk: {
              artifact_kind: "apk",
              mime: apk.mime,
              size: apk.size,
              sha256_b64: apk.sha256_b64,
            },
          }),
        },
        signal,
        idempotencyKey,
      );
      const created = analysisResponse(payload);
      if (
        created.analysis_mode !== "device" ||
        created.team_id !== teamId ||
        created.device_id !== deviceId
      ) {
        throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
      }
      return created;
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
  const randomUUID = dependencies.randomUUID ?? createRandomUuid;
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

export type DeviceSubmissionPhase =
  | "session"
  | "hashing"
  | "creating"
  | "uploading"
  | "submitted";

export interface SubmitDeviceAnalysisInput {
  readonly teamId: string;
  readonly deviceId: string;
  readonly apk: File;
  readonly signal?: AbortSignal;
  readonly onProgress?: (phase: DeviceSubmissionPhase) => void;
}

export interface SubmitDeviceAnalysisDependencies {
  readonly client?: PerfPilotClient;
  readonly randomUUID?: () => string;
}

export async function enqueueDeviceAnalysis(
  submission: SubmitDeviceAnalysisInput,
  dependencies: SubmitDeviceAnalysisDependencies = {},
): Promise<SubmittedTraceAnalysis> {
  const client = dependencies.client ?? createPerfPilotClient();
  const randomUUID = dependencies.randomUUID ?? createRandomUuid;
  const { signal, onProgress } = submission;
  if (!submission.teamId || !submission.deviceId) {
    throw new PerfPilotApiError("device_required", "请选择可用的 Android 设备", false, null);
  }
  if (!(submission.apk instanceof File) || !submission.apk.name.toLowerCase().endsWith(".apk")) {
    throw new PerfPilotApiError("apk_required", "请选择 APK 文件", false, null);
  }

  onProgress?.("session");
  await client.csrf(signal);
  onProgress?.("hashing");
  const apk: InputDescriptor = {
    kind: "apk",
    file: submission.apk,
    mime: "application/vnd.android.package-archive",
    size: submission.apk.size,
    sha256_b64: await sha256Base64(submission.apk, signal),
  };

  onProgress?.("creating");
  const created = await client.createDeviceAnalysis(
    submission.teamId,
    submission.deviceId,
    apk,
    randomUUID(),
    signal,
  );
  const authorization = created.apk_upload;
  if (created.analysis_mode !== "device" || authorization === null || authorization === undefined) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }

  if (authorization.state === "pending") {
    onProgress?.("uploading");
    const slot: UploadSlot = { schema_version: "1.0", upload: authorization };
    await client.putInput(slot, apk, signal);
    await client.finalizeInput(
      submission.teamId,
      created.analysis_id,
      apk,
      authorization.upload_id,
      signal,
    );
  }

  const current = await client.analysis(submission.teamId, created.analysis_id, signal);
  if (
    current.analysis_mode !== "device" ||
    current.team_id !== submission.teamId ||
    current.device_id !== submission.deviceId
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  onProgress?.("submitted");
  return { teamId: submission.teamId, analysis: current };
}
