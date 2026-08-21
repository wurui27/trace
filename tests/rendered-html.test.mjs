import assert from "node:assert/strict";
import test from "node:test";

async function render(path = "/", origin = "http://localhost") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const requestUrl = new URL(path, origin);

  return worker.fetch(
    new Request(requestUrl, {
      headers: { accept: "text/html", host: requestUrl.host },
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

test("keeps absolute metadata assets on HTTP for private-network deployments", async () => {
  const response = await render("/", "http://10.166.0.125:3000");
  assert.equal(response.status, 200);

  const html = await response.text();
  assert.match(html, /href="http:\/\/10\.166\.0\.125:3000\/favicon\.svg"/);
  assert.doesNotMatch(html, /https:\/\/10\.166\.0\.125:3000\/favicon\.svg/);
});

test("server-renders the dashboard behind the local session gate", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>PerfPilot · Android 性能诊断<\/title>/);
  assert.match(
    html,
    /<meta[^>]+property="og:description"[^>]+content="问题优先、证据可追溯、优化可复测。"/,
  );
  assert.match(
    html,
    /<meta[^>]+property="og:image"[^>]+content="http:\/\/localhost\/og\.png"/,
  );
  assert.match(
    html,
    /<link[^>]+rel="icon"[^>]+href="(?:http:\/\/localhost)?\/favicon\.svg"/,
  );
  assert.match(html, /PerfPilot/);
  assert.match(html, /正在验证本地会话/);
  assert.doesNotMatch(html, /林墨/);
  assert.doesNotMatch(html, /928295d3-a73a-5c53-93e5-e24debb21b6c/);
  assert.doesNotMatch(html, /Acme Gallery/);
  assert.doesNotMatch(html, /Pixel 8/);
  assert.doesNotMatch(html, /1\.42 s/);
  assert.doesNotMatch(html, /发现 3 个需要关注的问题/);
  assert.doesNotMatch(html, /首页启动慢/);
});

test("does not expose old demo performance-problem details", async () => {
  const response = await render("/problems/startup-main-thread");
  assert.equal(response.status, 404);
});

test("returns 404 for an unknown performance problem", async () => {
  const response = await render("/problems/not-a-real-problem");

  assert.equal(response.status, 404);
});

test("keeps unfinished navigation pages free of demo application data", async () => {
  for (const path of ["/tests", "/scenarios", "/problems", "/comparisons"]) {
    const response = await render(path);
    assert.equal(response.status, 200);
    const html = await response.text();
    assert.match(html, /正在验证本地会话/);
    assert.doesNotMatch(html, /Acme Gallery/);
    assert.doesNotMatch(html, /Pixel 8/);
  }
});

test("server-renders the live analysis route behind the local session gate", async () => {
  const response = await render("/analyses/analysis-live-1");
  assert.equal(response.status, 200);

  const html = await response.text();
  assert.match(html, /正在验证本地会话/);
  assert.doesNotMatch(html, /首页启动慢/);
  assert.doesNotMatch(html, /Acme Gallery/);
  assert.doesNotMatch(html, /执行摘要/);
});

test("server-renders the final report route behind the local session gate", async () => {
  const response = await render("/analyses/analysis-live-1/report");
  assert.equal(response.status, 200);

  const html = await response.text();
  assert.match(html, /正在验证本地会话/);
  assert.doesNotMatch(html, /首页启动慢/);
  assert.doesNotMatch(html, /Acme Gallery/);
});
