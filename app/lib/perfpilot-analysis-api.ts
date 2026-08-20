const CANONICAL_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const STABLE_CODE = /^[a-z][a-z0-9_]{0,95}$/;

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

export interface AnalysisRuntimeStatus {
  readonly current_stage:
    | "input_validation"
    | "device_claim"
    | "device_capture"
    | "smartperfetto"
    | "source_code"
    | "perfpilot_ai"
    | "report";
  readonly stage_state:
    | "pending"
    | "running"
    | "waiting"
    | "slow"
    | "waiting_for_upstream"
    | "completed"
    | "failed"
    | "canceled"
    | "cancel_requested"
    | "not_requested";
  readonly started_at: string;
  readonly updated_at: string;
  readonly last_progress_at: string;
  readonly attempt: number;
  readonly max_attempts: number;
  readonly generation: number;
  readonly waiting_for:
    | "agent"
    | "device"
    | "smartperfetto"
    | "source_agent"
    | "ai_provider"
    | "storage"
    | "report_publish"
    | null;
  readonly progress_summary: string;
  readonly available_actions: readonly ("cancel" | "retry")[];
}

export type TraceInputKind =
  | "trace"
  | "memory_evidence"
  | "apk"
  | "source_archive"
  | "mapping"
  | "native_symbols"
  | "log";
export type TraceProfile = "auto" | "startup" | "scroll";
export type UploadedTraceTestType = "cold_start" | "hot_start" | "scroll" | "other";

export interface AnalysisAiRound {
  readonly round: 1 | 2 | 3;
  readonly role: "report" | "extract" | "review" | "finalize";
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

export type SourceCodeAnalysis =
  | {
      readonly requested: true;
      readonly provider_kind: "agent_workspace";
      readonly agent_id: string;
      readonly workspace_id: string;
      readonly snapshot_policy: "tracked_worktree";
      readonly validation_profile_id: string | null;
      readonly context_state: "waiting_for_agent" | "extracting" | "available" | "unavailable";
      readonly match_summary: "strong" | "weak" | "none";
      readonly verification_state:
        | "not_requested"
        | "pending"
        | "validating"
        | "verified"
        | "apply_failed"
        | "validation_failed"
        | "source_changed"
        | "not_configured"
        | "timeout"
        | "canceled"
        | "unavailable";
      readonly failure_code: string | null;
    }
  | {
      readonly requested: false;
      readonly provider_kind: null;
      readonly agent_id: null;
      readonly workspace_id: null;
      readonly snapshot_policy: null;
      readonly validation_profile_id: null;
      readonly context_state: "not_requested";
      readonly match_summary: "none";
      readonly verification_state: "not_requested";
      readonly failure_code: null;
    };

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

export interface SampleVerdictCounts {
  readonly valid: number;
  readonly invalid: number;
  readonly pending: number;
  readonly validation_error: number;
  readonly total: number;
}

export interface AnalysisScenario {
  readonly scenario_job_id: string | null;
  readonly scenario_type: "cold_start" | "hot_start" | "scroll" | "memory_cycle";
  readonly state:
    | "awaiting_input"
    | "queued"
    | "scheduled"
    | "running"
    | "analyzing"
    | "completed"
    | "failed"
    | "canceled"
    | "not_requested";
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

export interface DeviceCaptureConfiguration {
  readonly test_type: "cold_start" | "hot_start" | "scroll";
  readonly launch_mode: "automatic" | "manual";
  readonly duration_seconds: number;
  readonly target: {
    readonly package_name: string;
    readonly launch_activity: string;
  } | null;
}

export interface AnalysisResponse {
  readonly schema_version: "1.0" | "1.1" | "1.2" | "1.3";
  readonly analysis_id: string;
  readonly team_id: string;
  readonly analysis_mode: AnalysisMode;
  readonly analysis_profile?: TraceProfile;
  readonly test_type?: UploadedTraceTestType;
  readonly package_name?: string;
  readonly custom_test_name?: string | null;
  readonly custom_test_description?: string | null;
  readonly question?: string | null;
  readonly state: AnalysisState;
  readonly version: number;
  readonly created_at?: string;
  readonly cancel_requested_at?: string | null;
  readonly report_available: boolean;
  readonly failure: AnalysisFailure | null;
  readonly runtime_status?: AnalysisRuntimeStatus;
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
  readonly ai_rounds?: readonly AnalysisAiRound[];
  readonly source_analysis?: AnalysisSource;
  readonly source_code_analysis?: SourceCodeAnalysis;
  readonly device_id?: string;
  readonly application_version_id?: string | null;
  readonly application_metadata?: ApplicationMetadata | null;
  readonly apk_upload?: UploadPayload | null;
  readonly scenarios?: readonly AnalysisScenario[];
  readonly sample_verdict_counts?: SampleVerdictCounts;
  readonly active_lease?: ActiveAnalysisLease | null;
  readonly started_at?: string | null;
  readonly completed_at?: string | null;
  readonly capture_configuration?: DeviceCaptureConfiguration;
}

export interface AnalysisListItem extends AnalysisResponse {
  readonly created_at: string;
}

export interface AnalysisListResponse {
  readonly schema_version: "1.0";
  readonly analyses: readonly AnalysisListItem[];
}

export class AnalysisContractError extends Error {
  readonly code = "invalid_api_response";

  constructor() {
    super("服务返回内容无效");
    this.name = "AnalysisContractError";
  }
}

type ErrorFactory = () => Error;

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

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  const allowedKeys = new Set(allowed);
  return Object.keys(value).every((key) => allowedKeys.has(key));
}

function validDateTime(value: unknown, nullable = false): boolean {
  return (
    (nullable && value === null) ||
    (typeof value === "string" && value.length <= 64 && !Number.isNaN(Date.parse(value)))
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

function validSourceCodeAnalysis(value: unknown): boolean {
  if (
    !object(value) ||
    !exactKeys(value, [
      "requested",
      "provider_kind",
      "agent_id",
      "workspace_id",
      "snapshot_policy",
      "validation_profile_id",
      "context_state",
      "match_summary",
      "verification_state",
      "failure_code",
    ])
  ) {
    return false;
  }
  if (value.requested === false) {
    return (
      value.provider_kind === null &&
      value.agent_id === null &&
      value.workspace_id === null &&
      value.snapshot_policy === null &&
      value.validation_profile_id === null &&
      value.context_state === "not_requested" &&
      value.match_summary === "none" &&
      value.verification_state === "not_requested" &&
      value.failure_code === null
    );
  }
  return (
    value.requested === true &&
    value.provider_kind === "agent_workspace" &&
    typeof value.agent_id === "string" &&
    CANONICAL_UUID.test(value.agent_id) &&
    typeof value.workspace_id === "string" &&
    CANONICAL_UUID.test(value.workspace_id) &&
    value.snapshot_policy === "tracked_worktree" &&
    (value.validation_profile_id === null ||
      (typeof value.validation_profile_id === "string" &&
        CANONICAL_UUID.test(value.validation_profile_id))) &&
    ["waiting_for_agent", "extracting", "available", "unavailable"].includes(
      String(value.context_state),
    ) &&
    ["strong", "weak", "none"].includes(String(value.match_summary)) &&
    [
      "not_requested",
      "pending",
      "validating",
      "verified",
      "apply_failed",
      "validation_failed",
      "source_changed",
      "not_configured",
      "timeout",
      "canceled",
      "unavailable",
    ].includes(String(value.verification_state)) &&
    (value.failure_code === null ||
      (typeof value.failure_code === "string" && STABLE_CODE.test(value.failure_code)))
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

function validAiRounds(value: unknown): boolean {
  if (!Array.isArray(value)) return false;
  const layouts = [["report"], ["extract", "review", "finalize"]] as const;
  const layout = layouts.find((candidate) => candidate.length === value.length);
  return (
    layout !== undefined &&
    value.every(
      (item, index) =>
        object(item) &&
        exactKeys(item, ["round", "role", "state", "attempts"]) &&
        item.round === index + 1 &&
        item.role === layout[index] &&
        ["pending", "running", "completed", "failed"].includes(String(item.state)) &&
        Number.isSafeInteger(item.attempts) &&
        Number(item.attempts) >= 0,
    )
  );
}

function validAnalysisSource(value: unknown): boolean {
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

function validVerdictCounts(value: unknown): boolean {
  if (
    !object(value) ||
    !exactKeys(value, ["valid", "invalid", "pending", "validation_error", "total"])
  ) {
    return false;
  }
  const counts = [value.valid, value.invalid, value.pending, value.validation_error, value.total];
  return (
    counts.every((count) => Number.isSafeInteger(count) && Number(count) >= 0) &&
    Number(value.valid) +
      Number(value.invalid) +
      Number(value.pending) +
      Number(value.validation_error) ===
      Number(value.total)
  );
}

function validNullableString(value: unknown, maximum: number): boolean {
  return value === null || (typeof value === "string" && value.length <= maximum);
}

function validStringArray(value: unknown, maximum: number): boolean {
  return (
    Array.isArray(value) &&
    value.length <= maximum &&
    value.every((item) => typeof item === "string")
  );
}

function validApplicationMetadata(value: unknown): boolean {
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
    validStringArray(value.supported_abis, 32) &&
    typeof value.has_native_libraries === "boolean"
  );
}

function validDeviceCaptureConfiguration(value: unknown): boolean {
  if (
    !object(value) ||
    !exactKeys(value, ["test_type", "launch_mode", "duration_seconds", "target"]) ||
    !["cold_start", "hot_start", "scroll"].includes(String(value.test_type)) ||
    !["automatic", "manual"].includes(String(value.launch_mode)) ||
    !Number.isSafeInteger(value.duration_seconds) ||
    Number(value.duration_seconds) < 1 ||
    Number(value.duration_seconds) > 300
  ) {
    return false;
  }
  const requiresTarget = value.launch_mode === "automatic" || value.test_type === "scroll";
  const validTarget =
    object(value.target) &&
    exactKeys(value.target, ["package_name", "launch_activity"]) &&
    typeof value.target.package_name === "string" &&
    /^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$/.test(
      value.target.package_name,
    ) &&
    typeof value.target.launch_activity === "string" &&
    /^[A-Za-z0-9_.$]+\/[A-Za-z0-9_.$]+$/.test(value.target.launch_activity) &&
    value.target.launch_activity.split("/", 1)[0] === value.target.package_name;
  return (
    (value.test_type !== "scroll" || value.launch_mode === "manual") &&
    (requiresTarget ? validTarget : value.target === null)
  );
}

function validUploadPayload(value: unknown): boolean {
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
      (!hasPutUrl || (typeof value.put_url === "string" && object(value.required_headers)))
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

function validAnalysisScenario(
  value: unknown,
  expectedType: string,
  allowNotRequested = false,
): boolean {
  if (
    !object(value) ||
    !exactKeys(value, [
      "scenario_job_id",
      "scenario_type",
      "state",
      "version",
      "device_group_id",
      "sample_verdict_counts",
      "started_at",
      "completed_at",
      "failure",
    ]) ||
    value.scenario_type !== expectedType
  ) {
    return false;
  }
  if (value.state === "not_requested") {
    return (
      allowNotRequested &&
      value.scenario_job_id === null &&
      value.version === null &&
      value.device_group_id === null &&
      validVerdictCounts(value.sample_verdict_counts) &&
      object(value.sample_verdict_counts) &&
      Object.values(value.sample_verdict_counts).every((count) => count === 0) &&
      value.started_at === null &&
      value.completed_at === null &&
      value.failure === null
    );
  }
  return (
    (value.scenario_job_id === null || typeof value.scenario_job_id === "string") &&
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

function validActiveLease(value: unknown): boolean {
  return (
    object(value) &&
    exactKeys(value, ["lease_id", "device_id", "state", "expires_at"]) &&
    typeof value.lease_id === "string" &&
    typeof value.device_id === "string" &&
    value.state === "active" &&
    validDateTime(value.expires_at)
  );
}

function validAnalysisRuntimeStatus(value: unknown): value is AnalysisRuntimeStatus {
  const stages = [
    "input_validation",
    "device_claim",
    "device_capture",
    "smartperfetto",
    "source_code",
    "perfpilot_ai",
    "report",
  ];
  const states = [
    "pending",
    "running",
    "waiting",
    "slow",
    "waiting_for_upstream",
    "completed",
    "failed",
    "canceled",
    "cancel_requested",
    "not_requested",
  ];
  const waitingFor = [
    "agent",
    "device",
    "smartperfetto",
    "source_agent",
    "ai_provider",
    "storage",
    "report_publish",
  ];
  return (
    object(value) &&
    exactKeys(value, [
      "current_stage",
      "stage_state",
      "started_at",
      "updated_at",
      "last_progress_at",
      "attempt",
      "max_attempts",
      "generation",
      "waiting_for",
      "progress_summary",
      "available_actions",
    ]) &&
    stages.includes(String(value.current_stage)) &&
    states.includes(String(value.stage_state)) &&
    validDateTime(value.started_at) &&
    validDateTime(value.updated_at) &&
    validDateTime(value.last_progress_at) &&
    Number.isSafeInteger(value.attempt) &&
    Number(value.attempt) >= 1 &&
    Number.isSafeInteger(value.max_attempts) &&
    Number(value.max_attempts) >= 1 &&
    Number(value.attempt) <= Number(value.max_attempts) &&
    Number.isSafeInteger(value.generation) &&
    Number(value.generation) >= 1 &&
    (value.waiting_for === null || waitingFor.includes(String(value.waiting_for))) &&
    typeof value.progress_summary === "string" &&
    value.progress_summary.length <= 240 &&
    Array.isArray(value.available_actions) &&
    value.available_actions.length <= 2 &&
    new Set(value.available_actions).size === value.available_actions.length &&
    value.available_actions.every((item) => ["cancel", "retry"].includes(String(item)))
  );
}

function invalid(factory: ErrorFactory): never {
  throw factory();
}

export function parseAnalysisResponse(
  value: unknown,
  errorFactory: ErrorFactory = () => new AnalysisContractError(),
): AnalysisResponse {
  const hasAiRounds = object(value) && "ai_rounds" in value;
  const hasSource = object(value) && "source_analysis" in value;
  const hasSourceCode = object(value) && "source_code_analysis" in value;
  const hasRuntimeStatus = object(value) && "runtime_status" in value;
  const createdAt = object(value) ? value.created_at : undefined;
  const cancelRequestedAt = object(value) ? value.cancel_requested_at : undefined;
  const commonKeys = [
    "schema_version",
    "analysis_id",
    "team_id",
    "analysis_mode",
    "state",
    "version",
    "created_at",
    "cancel_requested_at",
    "report_available",
    "failure",
    "source_code_analysis",
    "runtime_status",
  ] as const;
  const modeKeys = object(value)
    ? value.analysis_mode === "trace_upload"
      ? [
          "analysis_profile",
          ...("test_type" in value
            ? ["test_type", "package_name", "custom_test_name", "custom_test_description"]
            : []),
          "question",
          "input_uploads",
          "stages",
          "ai_rounds",
          "source_analysis",
        ]
      : value.analysis_mode === "device"
        ? value.schema_version === "1.2" ||
          (value.schema_version === "1.3" && "capture_configuration" in value)
          ? [
              "device_id",
              "application_version_id",
              "application_metadata",
              "capture_configuration",
              "scenarios",
              "sample_verdict_counts",
              "active_lease",
              "started_at",
              "completed_at",
            ]
          : [
              "device_id",
              "application_version_id",
              "application_metadata",
              "apk_upload",
              "scenarios",
              "sample_verdict_counts",
              "active_lease",
              "started_at",
              "completed_at",
            ]
        : ["application_version_id", "application_metadata", "question"]
    : [];
  if (
    !object(value) ||
    !exactKeys(value, [...commonKeys, ...modeKeys]) ||
    !["1.0", "1.1", "1.2", "1.3"].includes(String(value.schema_version)) ||
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
    (value.failure !== null && !validFailure(value.failure)) ||
    (["1.1", "1.2", "1.3"].includes(String(value.schema_version))) !== hasSourceCode ||
    (hasSourceCode && !validSourceCodeAnalysis(value.source_code_analysis)) ||
    (value.schema_version === "1.3") !== hasRuntimeStatus ||
    (hasRuntimeStatus && !validAnalysisRuntimeStatus(value.runtime_status))
  ) {
    return invalid(errorFactory);
  }
  if (value.analysis_mode === "trace_upload") {
    const hasTraceTarget = "test_type" in value;
    const validTraceTarget =
      !hasTraceTarget ||
      (["cold_start", "hot_start", "scroll", "other"].includes(String(value.test_type)) &&
        typeof value.package_name === "string" &&
        /^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$/.test(value.package_name) &&
        (value.test_type === "other"
          ? typeof value.custom_test_name === "string" &&
            value.custom_test_name.length > 0 &&
            typeof value.custom_test_description === "string" &&
            value.custom_test_description.length > 0
          : value.custom_test_name === null && value.custom_test_description === null));
    if (
      !["auto", "startup", "scroll"].includes(String(value.analysis_profile)) ||
      !validTraceTarget ||
      (value.question !== null && typeof value.question !== "string") ||
      !Array.isArray(value.input_uploads) ||
      !validStages(value.stages) ||
      hasAiRounds !== hasSource ||
      (hasAiRounds && !validAiRounds(value.ai_rounds)) ||
      (hasSource && !validAnalysisSource(value.source_analysis))
    ) {
      return invalid(errorFactory);
    }
    return value as unknown as AnalysisResponse;
  }
  if (hasAiRounds || hasSource) return invalid(errorFactory);
  if (value.analysis_mode === "device") {
    if (
      value.schema_version === "1.2" ||
      (value.schema_version === "1.3" && "capture_configuration" in value)
    ) {
      if (
        typeof value.device_id !== "string" ||
        value.application_version_id !== null ||
        value.application_metadata !== null ||
        !validDeviceCaptureConfiguration(value.capture_configuration) ||
        !Array.isArray(value.scenarios) ||
        value.scenarios.length !== 1 ||
        !validAnalysisScenario(value.scenarios[0], String(value.capture_configuration && object(value.capture_configuration) ? value.capture_configuration.test_type : "")) ||
        !validVerdictCounts(value.sample_verdict_counts) ||
        (value.active_lease !== null && !validActiveLease(value.active_lease)) ||
        !validDateTime(value.started_at, true) ||
        !validDateTime(value.completed_at, true)
      ) {
        return invalid(errorFactory);
      }
      return { ...value, stages: [], input_uploads: [] } as unknown as AnalysisResponse;
    }
    const scenarioTypes = ["cold_start", "scroll", "memory_cycle"] as const;
    if (
      typeof value.device_id !== "string" ||
      (value.application_version_id !== null && typeof value.application_version_id !== "string") ||
      (value.application_metadata !== null && !validApplicationMetadata(value.application_metadata)) ||
      !validUploadPayload(value.apk_upload) ||
      !Array.isArray(value.scenarios) ||
      value.scenarios.length !== scenarioTypes.length ||
      !value.scenarios.every((scenario, index) =>
        validAnalysisScenario(
          scenario,
          scenarioTypes[index],
          value.schema_version === "1.1" && scenarioTypes[index] === "memory_cycle",
        ),
      ) ||
      !validVerdictCounts(value.sample_verdict_counts) ||
      (value.active_lease !== null && !validActiveLease(value.active_lease)) ||
      !validDateTime(value.started_at, true) ||
      !validDateTime(value.completed_at, true)
    ) {
      return invalid(errorFactory);
    }
    return { ...value, stages: [], input_uploads: [] } as unknown as AnalysisResponse;
  }
  if (
    (value.application_version_id !== null && typeof value.application_version_id !== "string") ||
    !validApplicationMetadata(value.application_metadata) ||
    (value.question !== null && typeof value.question !== "string")
  ) {
    return invalid(errorFactory);
  }
  return { ...value, stages: [], input_uploads: [] } as unknown as AnalysisResponse;
}

export function parseAnalysisListResponse(
  value: unknown,
  context: {
    readonly teamId: string;
    readonly limit: number;
    readonly filter: "report" | "active";
  },
  errorFactory: ErrorFactory = () => new AnalysisContractError(),
): AnalysisListResponse {
  if (
    !object(value) ||
    !exactKeys(value, ["schema_version", "analyses"]) ||
    value.schema_version !== "1.0" ||
    !Array.isArray(value.analyses) ||
    value.analyses.length > context.limit
  ) {
    return invalid(errorFactory);
  }
  const ids = new Set<string>();
  const analyses: AnalysisListItem[] = [];
  for (const raw of value.analyses) {
    const parsed = parseAnalysisResponse(raw, errorFactory);
    const createdAt = object(raw) ? raw.created_at : null;
    if (
      parsed.team_id !== context.teamId ||
      (context.filter === "report" && !parsed.report_available) ||
      (context.filter === "active" && !ACTIVE_ANALYSIS_STATES.has(parsed.state)) ||
      typeof createdAt !== "string" ||
      createdAt.length > 64 ||
      Number.isNaN(Date.parse(createdAt)) ||
      ids.has(parsed.analysis_id)
    ) {
      return invalid(errorFactory);
    }
    ids.add(parsed.analysis_id);
    analyses.push({ ...parsed, created_at: createdAt });
  }
  return { schema_version: "1.0", analyses };
}

export function analysisIsTerminal(analysis: Pick<AnalysisResponse, "state">): boolean {
  return !ACTIVE_ANALYSIS_STATES.has(analysis.state);
}
