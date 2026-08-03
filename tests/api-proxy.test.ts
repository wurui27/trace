import { createHmac, createHash } from "node:crypto";

import { describe, expect, it, vi } from "vitest";

import { proxyApiRequest } from "../worker/api-proxy";

const env = {
  PERFPILOT_API_ORIGIN: "https://api.perfpilot.test",
  PERFPILOT_PROXY_SECRET: "proxy-test-secret-with-at-least-32-bytes",
};

function signature(body: string) {
  const canonical = [
    "1785744000",
    "req-fixed",
    "POST",
    "/v1/teams/team-1/analyses?source=web%20ui",
    createHash("sha256").update(body).digest("hex"),
  ].join("\n");

  return createHmac("sha256", env.PERFPILOT_PROXY_SECRET)
    .update(canonical)
    .digest("base64url");
}

describe("same-origin API proxy", () => {
  it("signs the exact upstream path query and JSON bytes", async () => {
    const body = '{"analysis_mode":"trace_upload"}';
    const upstream = vi.fn(async (request: Request) => {
      expect(request.url).toBe(
        "https://api.perfpilot.test/v1/teams/team-1/analyses?source=web%20ui",
      );
      expect(request.method).toBe("POST");
      expect(await request.text()).toBe(body);
      expect(request.headers.get("x-perfpilot-proxy-timestamp")).toBe(
        "1785744000",
      );
      expect(request.headers.get("x-request-id")).toBe("req-fixed");
      expect(request.headers.get("x-perfpilot-proxy-signature")).toBe(
        signature(body),
      );
      expect(request.headers.get("x-perfpilot-client-identity")).toMatch(
        /^[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}$/,
      );
      expect(request.headers.get("x-forwarded-host")).toBeNull();
      return Response.json({ schema_version: "1.0" });
    });

    const response = await proxyApiRequest(
      new Request(
        "https://web.test/api/v1/teams/team-1/analyses?source=web%20ui",
        {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "cf-connecting-ip": "203.0.113.7",
            "x-forwarded-host": "attacker.invalid",
            "x-perfpilot-proxy-signature": "attacker",
          },
          body,
        },
      ),
      env,
      {
        fetch: upstream,
        nowSeconds: () => 1785744000,
        requestId: () => "req-fixed",
      },
    );

    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledOnce();
  });

  it("rejects unsafe paths methods oversized bodies and missing configuration", async () => {
    const upstream = vi.fn();
    const requests = [
      new Request("https://web.test/api/v2/me"),
      new Request("https://web.test/api/v1/%2e%2e/admin"),
      new Request("https://web.test/api/v1/me", { method: "OPTIONS" }),
      new Request("https://web.test/api/v1/me", {
        method: "POST",
        body: new Uint8Array(1024 * 1024 + 1),
      }),
    ];

    for (const request of requests) {
      const response = await proxyApiRequest(request, env, {
        fetch: upstream,
        nowSeconds: () => 1785744000,
        requestId: () => "req-fixed",
      });
      expect(response.status).toBeGreaterThanOrEqual(400);
    }
    const missing = await proxyApiRequest(
      new Request("https://web.test/api/v1/me"),
      { PERFPILOT_API_ORIGIN: "", PERFPILOT_PROXY_SECRET: "" },
      {
        fetch: upstream,
        nowSeconds: () => 1785744000,
        requestId: () => "req-fixed",
      },
    );
    expect(missing.status).toBe(503);
    expect(upstream).not.toHaveBeenCalled();
  });

  it("removes the upstream cookie Domain and hop-by-hop response headers", async () => {
    const response = await proxyApiRequest(
      new Request("https://web.test/api/v1/auth/csrf"),
      env,
      {
        fetch: async () =>
          new Response("{}", {
            headers: {
              connection: "close",
              "set-cookie":
                "perfpilot_session=abc; Domain=api.test; Path=/; Secure; HttpOnly; SameSite=Lax",
            },
          }),
        nowSeconds: () => 1785744000,
        requestId: () => "req-cookie",
      },
    );

    expect(response.headers.get("set-cookie")).toBe(
      "perfpilot_session=abc; Path=/; Secure; HttpOnly; SameSite=Lax",
    );
    expect(response.headers.get("connection")).toBeNull();
  });
});
