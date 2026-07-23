import Link from "next/link";
import {
  ArrowLeft,
  CheckCircle2,
  CircleAlert,
  ExternalLink,
  MapPin,
  RotateCcw,
  ShieldCheck,
} from "lucide-react";

import type {
  DashboardData,
  PerformanceProblem,
} from "../lib/performance-data";

interface ProblemDetailProps {
  readonly problem: PerformanceProblem;
  readonly app: DashboardData["app"];
  readonly device: DashboardData["device"];
}

function getConfidenceLevel(confidence: number): "高" | "中" | "低" {
  if (confidence >= 85) {
    return "高";
  }

  if (confidence >= 70) {
    return "中";
  }

  return "低";
}

export function ProblemDetail({
  problem,
  app,
  device,
}: ProblemDetailProps) {
  const isConfirmed = problem.status === "已确认问题";
  const confidenceLevel = getConfidenceLevel(problem.confidence);
  const scenarioWindow = problem.evidence.find(
    (item) => item.step === "锁定场景窗口",
  );
  const retestNoteId = `retest-note-${problem.id}`;

  return (
    <article className="mx-auto w-full max-w-6xl space-y-8">
      <nav aria-label="面包屑">
        <ol className="flex flex-wrap items-center gap-2 text-sm text-zinc-500">
          <li>
            <Link
              className="inline-flex items-center gap-1.5 font-medium text-zinc-700 transition-colors hover:text-zinc-950"
              href="/"
            >
              <ArrowLeft aria-hidden="true" className="size-4" />
              性能总览
            </Link>
          </li>
          <li aria-hidden="true">/</li>
          <li aria-current="page">{problem.title}</li>
        </ol>
      </nav>

      <header className="space-y-5 border-b border-zinc-200 pb-8">
        <div className="space-y-2">
          <p className="text-sm font-medium text-zinc-500">
            {app.name} · {app.version}
          </p>
          <h1 className="text-3xl font-semibold tracking-tight text-zinc-950">
            {problem.title}
          </h1>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span
              className={
                isConfirmed
                  ? "inline-flex items-center gap-1.5 rounded-full bg-red-50 px-3 py-1 font-medium text-red-700"
                  : "inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-3 py-1 font-medium text-amber-700"
              }
            >
              {isConfirmed ? (
                <CheckCircle2 aria-hidden="true" className="size-4" />
              ) : (
                <CircleAlert aria-hidden="true" className="size-4" />
              )}
              {problem.status}
            </span>
            <span className="text-zinc-500">{problem.area}</span>
          </div>
        </div>

        <div className="space-y-3">
          <p className="max-w-3xl text-lg leading-8 text-zinc-700">
            {problem.impact}
          </p>
          <span className="inline-flex rounded-md border border-red-200 bg-red-50 px-2.5 py-1 text-sm font-semibold text-red-700">
            {problem.impactLabel}
          </span>
        </div>

        <dl
          className="grid gap-4 rounded-xl border border-zinc-200 bg-zinc-50 p-5 text-sm sm:grid-cols-3"
          aria-label="分析上下文"
        >
          <div className="space-y-1">
            <dt className="font-medium text-zinc-500">应用与构建</dt>
            <dd className="text-zinc-900">
              <code>{app.packageName}</code> · {app.version}
            </dd>
          </div>
          <div className="space-y-1">
            <dt className="font-medium text-zinc-500">测试设备</dt>
            <dd className="text-zinc-900">
              {device.name} · {device.os}
            </dd>
          </div>
          <div className="space-y-1">
            <dt className="font-medium text-zinc-500">场景窗口</dt>
            <dd className="text-zinc-900">
              {scenarioWindow ? (
                <>
                  {scenarioWindow.value} ·{" "}
                  <code>{scenarioWindow.interval}</code>
                </>
              ) : (
                "未记录"
              )}
            </dd>
          </div>
        </dl>
      </header>

      <section className="space-y-5" aria-labelledby="problem-conclusion">
        <div className="space-y-2">
          <p className="text-sm font-semibold text-red-700">诊断摘要</p>
          <h2
            className="text-2xl font-semibold text-zinc-950"
            id="problem-conclusion"
          >
            结论
          </h2>
          <p className="max-w-4xl leading-7 text-zinc-700">
            {problem.conclusion}
          </p>
        </div>

        <div className="rounded-2xl border border-zinc-200 bg-white p-6 shadow-sm">
          <dl className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
            <div className="space-y-1">
              <dt className="text-sm text-zinc-500">当前值</dt>
              <dd className="text-2xl font-semibold text-zinc-950">
                {problem.currentValue}
              </dd>
            </div>
            <div className="space-y-1">
              <dt className="text-sm text-zinc-500">目标</dt>
              <dd className="text-2xl font-semibold text-zinc-950">
                {problem.targetValue}
              </dd>
            </div>
            <div className="space-y-1">
              <dt className="text-sm text-zinc-500">差距</dt>
              <dd className="text-2xl font-semibold text-red-700">
                {problem.delta}
              </dd>
            </div>
            <div className="space-y-1">
              <dt className="text-sm text-zinc-500">可信度</dt>
              <dd className="inline-flex items-center gap-2 text-2xl font-semibold text-zinc-950">
                <ShieldCheck aria-hidden="true" className="size-5 text-emerald-600" />
                {confidenceLevel} · {problem.confidence}%
              </dd>
            </div>
          </dl>

          <div className="mt-6 border-t border-zinc-200 pt-5">
            <ul className="flex flex-wrap gap-x-6 gap-y-2 text-sm font-medium text-zinc-700">
              <li>{`${problem.validSamples} 个有效样本`}</li>
              <li>{`复现 ${problem.reproducedRuns} / 5 轮`}</li>
              <li>{problem.variability}</li>
            </ul>
            <p className="mt-3 text-sm leading-6 text-zinc-500">
              {problem.comparisonBasis}
            </p>
          </div>
        </div>
      </section>

      <section className="space-y-5" aria-labelledby="evidence-chain">
        <div className="space-y-2">
          <p className="text-sm font-semibold text-indigo-700">
            从现象到依赖
          </p>
          <h2
            className="text-2xl font-semibold text-zinc-950"
            id="evidence-chain"
          >
            证据链
          </h2>
        </div>

        <ol className="space-y-4">
          {problem.evidence.map((evidence, index) => (
            <li
              className="grid gap-4 rounded-2xl border border-zinc-200 bg-white p-5 sm:grid-cols-[auto_1fr_auto] sm:items-start"
              key={evidence.step}
            >
              <span
                className="flex size-8 items-center justify-center rounded-full bg-indigo-50 text-sm font-semibold text-indigo-700"
                aria-hidden="true"
              >
                {index + 1}
              </span>
              <div className="min-w-0 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="font-semibold text-zinc-950">
                    {evidence.step}
                  </h3>
                  <code className="rounded bg-zinc-100 px-2 py-1 text-xs text-zinc-600">
                    {evidence.interval}
                  </code>
                </div>
                <p className="font-medium text-zinc-900">{evidence.value}</p>
                <p className="text-sm leading-6 text-zinc-600">
                  {evidence.explanation}
                </p>
              </div>
              <button
                aria-label={`在 Perfetto 中查看：${evidence.step}（待接入）`}
                className="inline-flex cursor-not-allowed items-center justify-center gap-2 rounded-lg border border-zinc-200 px-3 py-2 text-sm text-zinc-400"
                disabled
                type="button"
              >
                <ExternalLink aria-hidden="true" className="size-4" />
                <span>在 Perfetto 中查看</span>
                <span className="text-xs">待接入</span>
              </button>
            </li>
          ))}
        </ol>
      </section>

      <section className="space-y-5" aria-labelledby="optimization-advice">
        <div className="space-y-2">
          <p className="text-sm font-semibold text-emerald-700">下一步行动</p>
          <h2
            className="text-2xl font-semibold text-zinc-950"
            id="optimization-advice"
          >
            优化建议
          </h2>
          <p className="max-w-4xl leading-7 text-zinc-700">
            {problem.suggestion}
          </p>
        </div>

        <dl className="rounded-2xl border border-zinc-200 bg-zinc-50 p-5">
          <div className="space-y-2">
            <dt className="inline-flex items-center gap-2 font-semibold text-zinc-950">
              <MapPin aria-hidden="true" className="size-4 text-zinc-500" />
              源码位置
            </dt>
            <dd className="leading-7 text-zinc-700">
              {problem.sourceLocation}
            </dd>
          </div>
        </dl>
      </section>

      <section
        className="space-y-5 border-t border-zinc-200 pt-8"
        aria-labelledby="retest-acceptance"
      >
        <h2
          className="text-2xl font-semibold text-zinc-950"
          id="retest-acceptance"
        >
          复测与验收
        </h2>
        <div className="flex flex-wrap items-center gap-3">
          <button
            aria-describedby={retestNoteId}
            className="inline-flex cursor-not-allowed items-center gap-2 rounded-lg bg-zinc-200 px-4 py-2.5 font-medium text-zinc-500"
            disabled
            type="button"
          >
            <RotateCcw aria-hidden="true" className="size-4" />
            同条件复测
          </button>
          <p className="text-sm text-zinc-500" id={retestNoteId}>
            接入任务服务后可用
          </p>
        </div>

        <div className="space-y-2 rounded-2xl border border-zinc-200 bg-white p-5">
          <h3 className="font-semibold text-zinc-950">验收标准</h3>
          <p className="leading-7 text-zinc-700">
            {problem.acceptanceCriteria}
          </p>
        </div>
      </section>
    </article>
  );
}
