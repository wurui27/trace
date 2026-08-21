import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import {
  access,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const projectRoot = path.resolve(import.meta.dirname, "..");
const restartScript = path.join(projectRoot, "scripts", "restart-local.sh");

test("npm exposes a documented one-command local restart", async () => {
  const packageJson = JSON.parse(
    await readFile(path.join(projectRoot, "package.json"), "utf8"),
  );

  assert.equal(
    packageJson.scripts["dev:restart"],
    "bash scripts/restart-local.sh",
  );
  await access(restartScript);

  const { stdout } = await execFileAsync("bash", [restartScript, "--help"], {
    cwd: projectRoot,
  });

  assert.match(stdout, /npm run dev:restart/);
  assert.match(stdout, /http:\/\/localhost:3000/);
  assert.match(stdout, /http:\/\/127\.0\.0\.1:8000\/v1\/health/);
  assert.match(stdout, /默认保留本地分析历史/);
  assert.match(stdout, /--reset-only/);

  const script = await readFile(restartScript, "utf8");
  const clearCalls = script.match(/^\s*clear_analysis_history\s*$/gm) ?? [];
  assert.equal(clearCalls.length, 1);
});

test("restart reset removes all local analysis history", async (t) => {
  const testRoot = path.join(
    projectRoot,
    ".perfpilot",
    `restart-test-${process.pid}-${Date.now()}`,
  );
  const dataDir = path.join(testRoot, "local-runtime");
  const siblingFile = path.join(testRoot, "keep.txt");

  await mkdir(path.join(dataDir, "analyses", "analysis-1"), {
    recursive: true,
  });
  await mkdir(path.join(dataDir, "uploads"), { recursive: true });
  await writeFile(
    path.join(dataDir, "analyses", "analysis-1", "report.json"),
    "{}",
  );
  await writeFile(path.join(dataDir, "uploads", "trace.bin"), "trace");
  await writeFile(siblingFile, "keep");
  t.after(() => rm(testRoot, { recursive: true, force: true }));

  const { stdout } = await execFileAsync(
    "bash",
    [restartScript, "--reset-only"],
    {
      cwd: projectRoot,
      env: {
        ...process.env,
        PERFPILOT_LOCAL_DATA_DIR: dataDir,
      },
    },
  );

  assert.match(stdout, /已清空历史分析数据/);
  assert.deepEqual(await readdir(dataDir), []);
  assert.equal(await readFile(siblingFile, "utf8"), "keep");
});
