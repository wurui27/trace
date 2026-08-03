import { sha256 } from "@noble/hashes/sha2.js";

const API_PREFIX = "/api/v1/";
const MAX_JSON_BYTES = 10 * 1024 * 1024;
const MAX_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024;
const HASH_CHUNK_BYTES = 4 * 1024 * 1024;
const MIME = /^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/;
const TERMINAL_STATES = new Set([
  "completed",
  "partially_completed",
  "failed",
  "canceled",
  "deleted",
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
  readonly report_available: boolean;
  readonly input_uploads: ReadonlyArray<{
    readonly state: "awaiting_upload" | "pending" | "finalized";
    readonly artifact_kind: TraceInputKind;
    readonly mime: string;
    readonly size: number;
    readonly sha256_b64: string;
    readonly upload_id?: string;
    readonly artifact_id?: string;
  }>;
  readonly failure: {
    readonly code: string;
    readonly message: string;
    readonly retryable: boolean;
  } | null;
}

export interface MeResponse {
  readonly schema_version: "1.0";
  readonly memberships: ReadonlyArray<{
    readonly team: { readonly id: string; readonly name: string };
    readonly role: string;
  }>;
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
  analysis(teamId: string, analysisId: string, signal?: AbortSignal): Promise<AnalysisResponse>;
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

function analysisResponse(value: unknown): AnalysisResponse {
  if (
    !object(value) ||
    value.schema_version !== "1.0" ||
    value.analysis_mode !== "trace_upload" ||
    typeof value.analysis_id !== "string" ||
    typeof value.team_id !== "string" ||
    !["auto", "startup", "scroll"].includes(String(value.analysis_profile)) ||
    !Array.isArray(value.input_uploads) ||
    typeof value.state !== "string" ||
    typeof value.version !== "number" ||
    typeof value.report_available !== "boolean"
  ) {
    throw new PerfPilotApiError("invalid_api_response", "服务返回内容无效", false, null);
  }
  return value as unknown as AnalysisResponse;
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
        !upload.put_url.startsWith("https://") ||
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

function defaultSleep(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    aborted(signal);
    const cancel = () => {
      clearTimeout(timeout);
      reject(signal?.reason ?? new DOMException("操作已取消", "AbortError"));
    };
    const finish = () => {
      signal?.removeEventListener("abort", cancel);
      resolve();
    };
    const timeout = setTimeout(finish, milliseconds);
    signal?.addEventListener("abort", cancel, { once: true });
  });
}

export type TraceSubmissionPhase =
  | "session"
  | "hashing"
  | "creating"
  | "uploading"
  | "analyzing"
  | "completed";

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

export async function submitTraceAnalysis(
  submission: SubmitTraceInput,
  dependencies: SubmitTraceDependencies = {},
): Promise<SubmittedTraceAnalysis> {
  const client = dependencies.client ?? createPerfPilotClient();
  const randomUUID = dependencies.randomUUID ?? (() => crypto.randomUUID());
  const sleep = dependencies.sleep ?? defaultSleep;
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

  onProgress?.("analyzing", created.analysis_id);
  let current = await client.analysis(teamId, created.analysis_id, signal);
  let retryDelay = 2_000;
  while (!TERMINAL_STATES.has(current.state)) {
    await sleep(retryDelay, signal);
    try {
      current = await client.analysis(teamId, created.analysis_id, signal);
      retryDelay = 2_000;
    } catch (error) {
      if (!(error instanceof PerfPilotApiError) || !error.retryable) {
        throw error;
      }
      retryDelay = Math.min(retryDelay * 2, 15_000);
    }
  }
  onProgress?.("completed");
  return { teamId, analysis: current };
}
