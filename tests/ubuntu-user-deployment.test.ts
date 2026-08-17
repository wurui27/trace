import { execFile as execFileCallback } from "node:child_process";
import { mkdtemp, mkdir, readFile, realpath, stat, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import { describe, expect, it } from "vitest";

const root = path.resolve(import.meta.dirname, "..");
const execFile = promisify(execFileCallback);

async function temporaryDirectory(prefix: string): Promise<string> {
  return mkdtemp(path.join(await realpath(os.tmpdir()), prefix));
}

async function source(relativePath: string): Promise<string> {
  return readFile(path.join(root, relativePath), "utf8");
}

describe("Ubuntu user deployment", () => {
  it("bootstraps pinned user-local runtimes with isolated private state", async () => {
    const script = await source("scripts/bootstrap-ubuntu-user.sh");

    expect(script).toContain("NODE_VERSION=24.15.0");
    expect(script).toContain("1508f99788bfcf18cc861e4bf4f8b472e84240c3");
    expect(script).toContain("d5514972ced78c3faa7fc17589c1ea9231645056");
    expect(script).toContain("wait_for_url http://127.0.0.1:3001/health");
    expect(script).toContain('wait_for_url "https://$SERVER_IP:8443/v1/health"');
    expect(script).toContain('wait_for_url "https://$SERVER_IP:8443"');
    expect(script).toContain('--editable "$PROJECT_DIR/agents/device-agent"');
    expect(script).toContain("for key in PORT SMARTPERFETTO_BACKEND_PORT");
    expect(script).toContain("printf '%s=3001\\n'");
    expect(script).toContain("PERFPILOT_LOCAL_AI_BASE_URL=https://api.deepseek.com/v1/");
    expect(script).toContain("PERFPILOT_LOCAL_AI_MODEL");
    expect(script).toContain("PERFPILOT_LOCAL_AI_TOKEN");
    expect(script).toContain("PERFPILOT_LOCAL_AI_THINKING=disabled");
    expect(script).toContain("PerfPilot 单轮 AI 报告暂不启用");
    expect(script).toContain('STATE_ROOT="$INSTALL_ROOT/state"');
    expect(script).toContain("PERFPILOT_LOCAL_STATE_DIR");
    expect(script).toContain("PERFPILOT_LOCAL_SOURCE_CODE_ANALYSIS_ENABLED=true");
    expect(script).toContain("PERFPILOT_RESET_ANALYSIS_DATA=true");
    expect(script).toContain("PERFPILOT_EXPECTED_ANALYSIS_ROOT");
    expect(script).toContain("bootstrap-local-users.py");
    expect(script).toContain("perfpilot.target");
    expect(script).not.toMatch(/\bsudo\b/);
  });

  it("permanently resets only the exact opted-in analysis root and preserves state", async () => {
    const temporaryRoot = await temporaryDirectory("perfpilot-reset-");
    const analysisRoot = path.join(temporaryRoot, "analysis");
    const stateRoot = path.join(temporaryRoot, "state");
    const analysisFiles = [
      "teams/team-1/analyses/analysis-1/uploads/application.apk",
      "teams/team-1/analyses/analysis-1/device-captures/startup.perfetto-trace",
      "teams/team-1/analyses/analysis-1/agent-artifacts/completed/agent.log",
      "teams/team-1/analyses/analysis-1/documents/report.json",
      "teams/team-1/analyses/analysis-1/documents/smartperfetto-original-startup.json",
    ];
    for (const relativePath of analysisFiles) {
      const target = path.join(analysisRoot, relativePath);
      await mkdir(path.dirname(target), { recursive: true });
      await writeFile(target, "analysis bytes");
    }
    const preservedState = [
      "control.json",
      "agents/agents.json",
    ];
    for (const relativePath of preservedState) {
      const target = path.join(stateRoot, relativePath);
      await mkdir(path.dirname(target), { recursive: true });
      await writeFile(target, "persistent bytes");
    }

    await execFile("bash", [path.join(root, "scripts/reset-ubuntu-analysis-data.sh")], {
      env: {
        ...process.env,
        PERFPILOT_LOCAL_DATA_DIR: analysisRoot,
        PERFPILOT_EXPECTED_ANALYSIS_ROOT: analysisRoot,
        PERFPILOT_LOCAL_STATE_DIR: stateRoot,
        PERFPILOT_RESET_ANALYSIS_DATA: "true",
      },
    });

    for (const relativePath of preservedState) {
      expect(await readFile(path.join(stateRoot, relativePath), "utf8")).toBe(
        "persistent bytes",
      );
    }
    expect((await stat(analysisRoot)).mode & 0o777).toBe(0o700);
    for (const relativePath of analysisFiles) {
      await expect(readFile(path.join(analysisRoot, relativePath))).rejects.toThrow();
    }
    const files = await execFile("find", [temporaryRoot, "-type", "f", "-print"]);
    expect(files.stdout.split("\n").filter(Boolean).sort()).toEqual(
      preservedState.map((relativePath) => path.join(stateRoot, relativePath)).sort(),
    );
    expect(files.stdout).not.toMatch(/backup|archive|\.tar|\.gz/);
  });

  it.each([
    ["unset opt-in", { PERFPILOT_RESET_ANALYSIS_DATA: undefined }],
    ["unset data root", { PERFPILOT_LOCAL_DATA_DIR: undefined }],
    ["mismatched expected root", { PERFPILOT_EXPECTED_ANALYSIS_ROOT: "/different" }],
    ["filesystem root", { PERFPILOT_LOCAL_DATA_DIR: "/", PERFPILOT_EXPECTED_ANALYSIS_ROOT: "/" }],
  ])("rejects unsafe reset configuration: %s", async (_label, override) => {
    const temporaryRoot = await temporaryDirectory("perfpilot-reject-");
    const analysisRoot = path.join(temporaryRoot, "analysis");
    await mkdir(analysisRoot);
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      PERFPILOT_LOCAL_DATA_DIR: analysisRoot,
      PERFPILOT_EXPECTED_ANALYSIS_ROOT: analysisRoot,
      PERFPILOT_LOCAL_STATE_DIR: path.join(temporaryRoot, "state"),
      PERFPILOT_RESET_ANALYSIS_DATA: "true",
      ...override,
    };
    for (const [key, value] of Object.entries(env)) {
      if (value === undefined) delete env[key];
    }
    await expect(
      execFile("bash", [path.join(root, "scripts/reset-ubuntu-analysis-data.sh")], { env }),
    ).rejects.toThrow();
  });

  it("leaves production data untouched when reset is explicitly disabled", async () => {
    const temporaryRoot = await temporaryDirectory("perfpilot-disabled-");
    const analysisRoot = path.join(temporaryRoot, "analysis");
    await mkdir(analysisRoot);
    await writeFile(path.join(analysisRoot, "keep"), "production");
    await execFile("bash", [path.join(root, "scripts/reset-ubuntu-analysis-data.sh")], {
      env: { ...process.env, PERFPILOT_RESET_ANALYSIS_DATA: "false" },
    });
    expect(await readFile(path.join(analysisRoot, "keep"), "utf8")).toBe("production");
  });

  it("rejects a symlink in any analysis-root component", async () => {
    const temporaryRoot = await temporaryDirectory("perfpilot-link-");
    const realParent = path.join(temporaryRoot, "real");
    const linkedParent = path.join(temporaryRoot, "linked");
    await mkdir(path.join(realParent, "analysis"), { recursive: true });
    await symlink(realParent, linkedParent);
    const analysisRoot = path.join(linkedParent, "analysis");
    await expect(
      execFile("bash", [path.join(root, "scripts/reset-ubuntu-analysis-data.sh")], {
        env: {
          ...process.env,
          PERFPILOT_LOCAL_DATA_DIR: analysisRoot,
          PERFPILOT_EXPECTED_ANALYSIS_ROOT: analysisRoot,
          PERFPILOT_LOCAL_STATE_DIR: path.join(temporaryRoot, "state"),
          PERFPILOT_RESET_ANALYSIS_DATA: "true",
        },
      }),
    ).rejects.toThrow();
  });

  it.each(["home", "state", "config", "project"])(
    "rejects a protected-looking %s root",
    async (name) => {
      const temporaryRoot = await temporaryDirectory("perfpilot-protected-");
      const analysisRoot = path.join(temporaryRoot, name);
      await mkdir(analysisRoot);
      await expect(
        execFile("bash", [path.join(root, "scripts/reset-ubuntu-analysis-data.sh")], {
          env: {
            ...process.env,
            PERFPILOT_LOCAL_DATA_DIR: analysisRoot,
            PERFPILOT_EXPECTED_ANALYSIS_ROOT: analysisRoot,
            PERFPILOT_LOCAL_STATE_DIR: path.join(temporaryRoot, "persistent"),
            PERFPILOT_RESET_ANALYSIS_DATA: "true",
          },
        }),
      ).rejects.toThrow();
    },
  );

  it("rejects a non-directory component and nested persistent state", async () => {
    const temporaryRoot = await temporaryDirectory("perfpilot-weird-");
    const regularFile = path.join(temporaryRoot, "regular-file");
    await writeFile(regularFile, "not a directory");
    await expect(
      execFile("bash", [path.join(root, "scripts/reset-ubuntu-analysis-data.sh")], {
        env: {
          ...process.env,
          PERFPILOT_LOCAL_DATA_DIR: regularFile,
          PERFPILOT_EXPECTED_ANALYSIS_ROOT: regularFile,
          PERFPILOT_LOCAL_STATE_DIR: path.join(temporaryRoot, "state"),
          PERFPILOT_RESET_ANALYSIS_DATA: "true",
        },
      }),
    ).rejects.toThrow();

    const analysisRoot = path.join(temporaryRoot, "analysis");
    const nestedState = path.join(analysisRoot, "persistent");
    await mkdir(nestedState, { recursive: true });
    await expect(
      execFile("bash", [path.join(root, "scripts/reset-ubuntu-analysis-data.sh")], {
        env: {
          ...process.env,
          PERFPILOT_LOCAL_DATA_DIR: analysisRoot,
          PERFPILOT_EXPECTED_ANALYSIS_ROOT: analysisRoot,
          PERFPILOT_LOCAL_STATE_DIR: nestedState,
          PERFPILOT_RESET_ANALYSIS_DATA: "true",
        },
      }),
    ).rejects.toThrow();
  });

  it("uses a unified restart target whose reset gate precedes all services", async () => {
    const target = await source("infra/ubuntu-user/systemd/perfpilot.target");
    const reset = await source(
      "infra/ubuntu-user/systemd/perfpilot-reset-analysis-data.service",
    );
    const bootstrap = await source("scripts/bootstrap-ubuntu-user.sh");
    const restart = await source("scripts/restart-ubuntu-perfpilot.sh");

    expect(target).toContain("Requires=perfpilot-reset-analysis-data.service");
    expect(target).toContain(
      "Wants=perfpilot-smartperfetto.service perfpilot-api.service perfpilot-web.service perfpilot-gateway.service",
    );
    expect(reset).toContain("Type=oneshot");
    expect(reset).toContain(
      "Before=perfpilot-smartperfetto.service perfpilot-api.service perfpilot-web.service perfpilot-gateway.service",
    );
    expect(reset).not.toContain("RemainAfterExit=yes");
    expect(bootstrap).toContain("restart-ubuntu-perfpilot.sh");
    expect(restart).toContain("systemctl --user stop perfpilot.target perfpilot-gateway.service");
    expect(restart).toContain("systemctl --user start perfpilot.target");
    expect(bootstrap).not.toContain("systemctl --user restart perfpilot.target");
    expect(restart).not.toContain("systemctl --user restart perfpilot.target");
  });

  it("terminates all LAN traffic through a pinned HTTPS gateway", async () => {
    const [bootstrap, caddyfile, gateway, target, reset, api, web, restart] =
      await Promise.all([
        source("scripts/bootstrap-ubuntu-user.sh"),
        source("infra/ubuntu-user/Caddyfile"),
        source("infra/ubuntu-user/systemd/perfpilot-gateway.service"),
        source("infra/ubuntu-user/systemd/perfpilot.target"),
        source("infra/ubuntu-user/systemd/perfpilot-reset-analysis-data.service"),
        source("infra/ubuntu-user/systemd/perfpilot-api.service"),
        source("infra/ubuntu-user/systemd/perfpilot-web.service"),
        source("scripts/restart-ubuntu-perfpilot.sh"),
      ]);

    expect(bootstrap).toContain("CADDY_VERSION=2.11.4");
    expect(bootstrap).toContain(
      "527fbf917c39189a1e3b31d34fa955601680b2d5c8055d2a87b8b9588dec7bb9",
    );
    expect(bootstrap).toContain("perfpilot-agent-ca.crt");
    expect(bootstrap).toContain('wait_for_url "https://$SERVER_IP:8443/v1/health"');
    expect(bootstrap).toContain("--cacert");
    expect(caddyfile).toContain("https://{$PERFPILOT_SERVER_IP}:8443");
    expect(caddyfile).toContain("tls {$PERFPILOT_TLS_CERT_FILE} {$PERFPILOT_TLS_KEY_FILE}");
    expect(caddyfile).toContain("reverse_proxy 127.0.0.1:8000");
    expect(caddyfile).toContain("reverse_proxy 127.0.0.1:3000");
    expect(gateway).toContain("ExecStart=%h/.local/bin/caddy run");
    expect(gateway).toContain("Restart=always");
    expect(gateway).toContain("PartOf=perfpilot.target");
    expect(target).toContain("perfpilot-gateway.service");
    expect(reset).toContain("perfpilot-gateway.service");
    expect(api).toContain("--host 127.0.0.1 --port 8000");
    expect(web).toContain("--hostname 127.0.0.1 --port 3000");
    expect(restart).toContain("perfpilot-gateway.service");
  });

  it("bootstraps only missing local users without accepting secrets as arguments", async () => {
    const script = await source("scripts/bootstrap-local-users.py");

    expect(script).toContain("LocalControlStore");
    expect(script).toContain('"ray_wu"');
    expect(script).toContain('f"user{index:02d}"');
    expect(script).toContain("range(1, 6)");
    expect(script).toContain("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD_FILE");
    expect(script).not.toMatch(/argparse|sys\.argv|print\([^)]*password/i);
  });

  it.each([
    ["perfpilot-smartperfetto.service", "PORT=3001"],
    ["perfpilot-api.service", "--host 127.0.0.1 --port 8000"],
    ["perfpilot-web.service", "--hostname 127.0.0.1 --port 3000"],
    ["perfpilot-gateway.service", "caddy run"],
  ])("keeps %s supervised and bound to its declared port", async (unit, marker) => {
    const service = await source(`infra/ubuntu-user/systemd/${unit}`);

    expect(service).toContain("Restart=always");
    expect(service).toContain(marker);
    expect(service).toContain("PartOf=perfpilot.target");
    expect(service).toContain("RefuseManualStart=yes");
    expect(service).toMatch(/After=.*perfpilot-reset-analysis-data\.service/);
    expect(service).not.toMatch(/\bsudo\b/);
  });
});
