import { readFile } from "node:fs/promises";
import path from "node:path";

import { describe, expect, it } from "vitest";

const root = path.resolve(import.meta.dirname, "..");

async function source(relativePath: string): Promise<string> {
  return readFile(path.join(root, relativePath), "utf8");
}

describe("Ubuntu user deployment", () => {
  it("bootstraps pinned user-local runtimes without sudo or data deletion", async () => {
    const script = await source("scripts/bootstrap-ubuntu-user.sh");

    expect(script).toContain("NODE_VERSION=24.15.0");
    expect(script).toContain("1508f99788bfcf18cc861e4bf4f8b472e84240c3");
    expect(script).toContain("d5514972ced78c3faa7fc17589c1ea9231645056");
    expect(script).toContain("wait_for_url http://127.0.0.1:3001/health");
    expect(script).toContain('wait_for_url "http://$SERVER_IP:8000/v1/health"');
    expect(script).toContain('wait_for_url "http://$SERVER_IP:3000"');
    expect(script).toContain('--editable "$PROJECT_DIR/agents/device-agent"');
    expect(script).toContain("for key in PORT SMARTPERFETTO_BACKEND_PORT");
    expect(script).toContain("printf '%s=3001\\n'");
    expect(script).toContain("PERFPILOT_LOCAL_AI_BASE_URL=https://api.deepseek.com/v1/");
    expect(script).toContain("PERFPILOT_LOCAL_AI_MODEL");
    expect(script).toContain("PERFPILOT_LOCAL_AI_TOKEN");
    expect(script).toContain("PERFPILOT_LOCAL_AI_THINKING=disabled");
    expect(script).toContain("PerfPilot 单轮 AI 报告暂不启用");
    expect(script).not.toMatch(/\bsudo\b/);
    expect(script).not.toMatch(/rm\s+-rf[^\n]*data/);
  });

  it.each([
    ["perfpilot-smartperfetto.service", "PORT=3001"],
    ["perfpilot-api.service", "--host 0.0.0.0 --port 8000"],
    ["perfpilot-web.service", "--port 3000"],
  ])("keeps %s supervised and bound to its declared port", async (unit, marker) => {
    const service = await source(`infra/ubuntu-user/systemd/${unit}`);

    expect(service).toContain("Restart=always");
    expect(service).toContain(marker);
    expect(service).not.toMatch(/\bsudo\b/);
  });
});
