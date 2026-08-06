import type { ProxyEnv } from "./api-proxy";

type ProcessEnvironment = Readonly<Record<string, string | undefined>>;

export function resolveRuntimeProxyEnv(
  workerEnv: ProxyEnv | undefined,
  processEnv: ProcessEnvironment | undefined,
): ProxyEnv {
  if (workerEnv !== undefined) {
    return {
      PERFPILOT_API_ORIGIN: workerEnv.PERFPILOT_API_ORIGIN,
      PERFPILOT_PROXY_SECRET: workerEnv.PERFPILOT_PROXY_SECRET,
    };
  }
  return {
    PERFPILOT_API_ORIGIN: processEnv?.PERFPILOT_API_ORIGIN ?? "",
    PERFPILOT_PROXY_SECRET: processEnv?.PERFPILOT_PROXY_SECRET ?? "",
  };
}
