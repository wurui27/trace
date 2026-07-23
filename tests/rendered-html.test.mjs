import assert from "node:assert/strict";
import test from "node:test";

async function render(path = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${path}`, {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the PerfPilot dashboard", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /PerfPilot/);
  assert.match(html, /发现 3 个需要关注的问题/);
  assert.match(html, /启动体验/);
  assert.match(html, /数据可信度/);
});

test("server-renders a performance problem evidence detail", async () => {
  const response = await render("/problems/startup-main-thread");
  assert.equal(response.status, 200);

  const html = await response.text();
  const expectedEvidenceStages = [
    "确认症状",
    "锁定场景窗口",
    "确认线程状态",
    "追踪依赖",
    "排除系统条件",
  ];

  for (const text of [
    "首页启动慢",
    "用户平均要多等待 217 ms 才看到首页",
    "影响首屏 217 ms",
    "结论",
    "5 个有效样本",
    "复现 4 / 5 轮",
    "CV 4.8%",
    "证据链",
    "确认症状",
    "锁定场景窗口",
    "确认线程状态",
    "追踪依赖",
    "排除系统条件",
    "优化建议",
    "源码位置",
    "验收标准",
    "同条件复测",
  ]) {
    assert.ok(html.includes(text), `expected HTML to contain "${text}"`);
  }

  const visibleEvidenceStages = Array.from(
    html.matchAll(/<h3\b[^>]*>([^<]+)<\/h3>/g),
    (match) => match[1],
  ).filter((heading) => expectedEvidenceStages.includes(heading));
  assert.deepEqual(visibleEvidenceStages, expectedEvidenceStages);

  const perfettoButtons = Array.from(
    html.matchAll(
      /<button\b[^>]*aria-label="在 Perfetto 中查看：([^"]+)（待接入）"[^>]*>/g,
    ),
  );
  assert.equal(perfettoButtons.length, 5);
  assert.deepEqual(
    perfettoButtons.map((match) => match[1]),
    expectedEvidenceStages,
  );
  assert.equal(
    new Set(perfettoButtons.map((match) => match[1])).size,
    perfettoButtons.length,
  );
  for (const [buttonTag] of perfettoButtons) {
    assert.match(buttonTag, /\sdisabled(?:=""|(?=\s|>))/);
  }

  const retestButton = html.match(
    /<button\b[^>]*aria-describedby="[^"]+"[^>]*>/,
  );
  assert.ok(retestButton, "expected a retest button with aria-describedby");
  assert.match(retestButton[0], /\sdisabled(?:=""|(?=\s|>))/);
});

test("returns 404 for an unknown performance problem", async () => {
  const response = await render("/problems/not-a-real-problem");

  assert.equal(response.status, 404);
});
