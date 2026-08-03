import Link from "next/link";

import { AnalysisProgress } from "../../components/analysis-progress";

interface AnalysisPageProps {
  readonly params: Promise<{ id: string }>;
}

export default async function AnalysisPage({ params }: AnalysisPageProps) {
  const { id } = await params;

  return (
    <div className="analysis-page">
      <header className="analysis-page-header">
        <Link className="analysis-page-brand" href="/" aria-label="返回 PerfPilot 首页">
          <span className="brand-mark" aria-hidden="true">
            <span className="brand-mark-bar brand-mark-bar-short" />
            <span className="brand-mark-bar brand-mark-bar-medium" />
            <span className="brand-mark-bar brand-mark-bar-tall" />
          </span>
          <span>PerfPilot</span>
        </Link>
        <Link href="/">返回总览</Link>
      </header>
      <main className="analysis-page-main">
        <div className="analysis-page-heading">
          <p className="section-label">实时任务</p>
          <h1>分析进度</h1>
          <p>页面只展示来自当前团队数据库的真实状态。</p>
        </div>
        <AnalysisProgress analysisId={id} />
      </main>
    </div>
  );
}
