import { describe, expect, it } from "vitest";

import { resolveRuntimeProxyEnv } from "../worker/runtime-env";

describe("production worker runtime environment", () => {
  it("uses process environment values when vinext does not provide Worker bindings", () => {
    expect(
      resolveRuntimeProxyEnv(undefined, {
        PERFPILOT_API_ORIGIN: "http://127.0.0.1:8000",
        PERFPILOT_PROXY_SECRET: "server-proxy-secret-with-at-least-32-bytes",
      }),
    ).toEqual({
      PERFPILOT_API_ORIGIN: "http://127.0.0.1:8000",
      PERFPILOT_PROXY_SECRET: "server-proxy-secret-with-at-least-32-bytes",
    });
  });

  it("keeps explicit Worker bindings authoritative", () => {
    expect(
      resolveRuntimeProxyEnv(
        {
          PERFPILOT_API_ORIGIN: "https://api.perfpilot.example",
          PERFPILOT_PROXY_SECRET: "worker-proxy-secret-with-at-least-32-bytes",
        },
        {
          PERFPILOT_API_ORIGIN: "http://127.0.0.1:8000",
          PERFPILOT_PROXY_SECRET: "server-proxy-secret-with-at-least-32-bytes",
        },
      ),
    ).toEqual({
      PERFPILOT_API_ORIGIN: "https://api.perfpilot.example",
      PERFPILOT_PROXY_SECRET: "worker-proxy-secret-with-at-least-32-bytes",
    });
  });
});
