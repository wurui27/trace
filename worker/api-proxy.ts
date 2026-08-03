const MAX_BODY_BYTES = 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 15_000;
const REQUEST_ID = /^[A-Za-z0-9._:-]{1,128}$/;
const SAFE_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]);
const FORWARDED_REQUEST_HEADERS = new Set([
  "accept",
  "content-type",
  "cookie",
  "origin",
  "referer",
  "user-agent",
  "x-csrf-token",
  "idempotency-key",
]);
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

export interface ProxyEnv {
  PERFPILOT_API_ORIGIN: string;
  PERFPILOT_PROXY_SECRET: string;
}

export interface ProxyDependencies {
  fetch: (request: Request) => Promise<Response>;
  nowSeconds: () => number;
  requestId: () => string;
}

const defaultDependencies: ProxyDependencies = {
  fetch: (request) => globalThis.fetch(request),
  nowSeconds: () => Math.floor(Date.now() / 1000),
  requestId: () => crypto.randomUUID(),
};

function errorResponse(status: number, code: string, requestId: string): Response {
  return Response.json(
    {
      schema_version: "1.0",
      error: {
        code,
        message: "API 代理暂时不可用",
        retryable: status >= 500,
        request_id: requestId,
      },
    },
    { status, headers: { "cache-control": "no-store" } },
  );
}

function configuredOrigin(env: ProxyEnv): URL | null {
  if (
    typeof env.PERFPILOT_API_ORIGIN !== "string" ||
    typeof env.PERFPILOT_PROXY_SECRET !== "string" ||
    env.PERFPILOT_PROXY_SECRET.length < 32
  ) {
    return null;
  }
  try {
    const origin = new URL(env.PERFPILOT_API_ORIGIN);
    const loopback = origin.hostname === "127.0.0.1" || origin.hostname === "localhost";
    if (
      !((origin.protocol === "https:" && !loopback) || (origin.protocol === "http:" && loopback)) ||
      origin.username ||
      origin.password ||
      origin.pathname !== "/" ||
      origin.search ||
      origin.hash
    ) {
      return null;
    }
    return origin;
  } catch {
    return null;
  }
}

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function ownedBuffer(bytes: Uint8Array): ArrayBuffer {
  return Uint8Array.from(bytes).buffer;
}

async function hmac(secret: string, payload: Uint8Array): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    ownedBuffer(encoder.encode(secret)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, ownedBuffer(payload));
  return bytesToBase64Url(new Uint8Array(digest));
}

async function requestSignature(
  secret: string,
  timestamp: number,
  requestId: string,
  method: string,
  pathAndQuery: string,
  body: Uint8Array,
): Promise<string> {
  const bodyDigest = await crypto.subtle.digest("SHA-256", ownedBuffer(body));
  const bodyHash = Array.from(new Uint8Array(bodyDigest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return hmac(
    secret,
    new TextEncoder().encode(
      [timestamp, requestId, method.toUpperCase(), pathAndQuery, bodyHash].join("\n"),
    ),
  );
}

async function clientIdentity(
  secret: string,
  timestamp: number,
  requestId: string,
  clientAddress: string,
): Promise<string> {
  const encoder = new TextEncoder();
  const safeAddress = /^[0-9A-Fa-f:.]{2,64}$/.test(clientAddress)
    ? clientAddress.toLowerCase()
    : "0.0.0.0";
  const clientId = await hmac(
    secret,
    encoder.encode(`perfpilot-client-id-v1\n${safeAddress}`),
  );
  const attestation = await hmac(
    secret,
    encoder.encode(
      `perfpilot-client-attestation-v1\n${timestamp}\n${requestId}\n${clientId}`,
    ),
  );
  return `${clientId}.${attestation}`;
}

function stripCookieDomain(cookie: string): string {
  return cookie
    .split(";")
    .filter((part) => !/^\s*domain\s*=/i.test(part))
    .join(";");
}

function responseHeaders(upstream: Headers): Headers {
  const headers = new Headers();
  for (const [name, value] of upstream) {
    if (!HOP_BY_HOP_HEADERS.has(name.toLowerCase()) && name.toLowerCase() !== "set-cookie") {
      headers.append(name, value);
    }
  }
  const cookieHeaders = upstream as Headers & { getSetCookie?: () => string[] };
  const cookies = cookieHeaders.getSetCookie?.() ??
    (upstream.get("set-cookie") ? [upstream.get("set-cookie") as string] : []);
  for (const cookie of cookies) {
    headers.append("set-cookie", stripCookieDomain(cookie));
  }
  return headers;
}

function safeApiPath(url: URL): string | null {
  if (
    !url.pathname.startsWith("/api/v1/") ||
    /\\|%(?:2e|2f|5c)/i.test(url.pathname)
  ) {
    return null;
  }
  const upstreamPath = url.pathname.slice(4);
  return `${upstreamPath}${url.search}`;
}

export async function proxyApiRequest(
  request: Request,
  env: ProxyEnv,
  dependencies: Partial<ProxyDependencies> = {},
): Promise<Response> {
  const deps = { ...defaultDependencies, ...dependencies };
  const fallbackRequestId = deps.requestId();
  const requestId = REQUEST_ID.test(request.headers.get("x-request-id") ?? "")
    ? (request.headers.get("x-request-id") as string)
    : fallbackRequestId;
  if (!REQUEST_ID.test(requestId)) {
    return errorResponse(503, "proxy_configuration_invalid", "unavailable");
  }
  const origin = configuredOrigin(env);
  if (origin === null) {
    return errorResponse(503, "proxy_configuration_invalid", requestId);
  }
  const url = new URL(request.url);
  const pathAndQuery = safeApiPath(url);
  if (pathAndQuery === null) {
    return errorResponse(404, "resource_not_found", requestId);
  }
  const method = request.method.toUpperCase();
  if (!SAFE_METHODS.has(method)) {
    return errorResponse(405, "method_not_allowed", requestId);
  }
  const declaredLength = request.headers.get("content-length");
  if (declaredLength !== null && (!/^\d+$/.test(declaredLength) || Number(declaredLength) > MAX_BODY_BYTES)) {
    return errorResponse(413, "request_body_too_large", requestId);
  }
  const body = method === "GET" || method === "HEAD"
    ? new Uint8Array()
    : new Uint8Array(await request.arrayBuffer());
  if (body.byteLength > MAX_BODY_BYTES) {
    return errorResponse(413, "request_body_too_large", requestId);
  }

  const timestamp = Math.floor(deps.nowSeconds());
  if (!Number.isSafeInteger(timestamp) || timestamp < 0) {
    return errorResponse(503, "proxy_configuration_invalid", requestId);
  }
  const headers = new Headers();
  for (const [name, value] of request.headers) {
    if (FORWARDED_REQUEST_HEADERS.has(name.toLowerCase())) {
      headers.append(name, value);
    }
  }
  headers.set("x-request-id", requestId);
  headers.set("x-perfpilot-proxy-timestamp", String(timestamp));
  headers.set(
    "x-perfpilot-proxy-signature",
    await requestSignature(
      env.PERFPILOT_PROXY_SECRET,
      timestamp,
      requestId,
      method,
      pathAndQuery,
      body,
    ),
  );
  headers.set(
    "x-perfpilot-client-identity",
    await clientIdentity(
      env.PERFPILOT_PROXY_SECRET,
      timestamp,
      requestId,
      request.headers.get("cf-connecting-ip") ?? "0.0.0.0",
    ),
  );

  const controller = new AbortController();
  const abort = () => controller.abort();
  if (request.signal.aborted) {
    abort();
  } else {
    request.signal.addEventListener("abort", abort, { once: true });
  }
  const timeout = setTimeout(abort, UPSTREAM_TIMEOUT_MS);
  try {
    const upstreamRequest = new Request(new URL(pathAndQuery, origin), {
      method,
      headers,
      body: body.byteLength > 0 ? body : undefined,
      redirect: "manual",
      signal: controller.signal,
    });
    const upstream = await deps.fetch(upstreamRequest);
    return new Response(method === "HEAD" ? null : upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders(upstream.headers),
    });
  } catch {
    return errorResponse(
      controller.signal.aborted ? 504 : 502,
      controller.signal.aborted ? "upstream_timeout" : "upstream_unavailable",
      requestId,
    );
  } finally {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", abort);
  }
}
