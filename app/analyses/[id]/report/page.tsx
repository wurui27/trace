import { FullAnalysisReport } from "../../../components/full-analysis-report";

interface FinalReportPageProps {
  readonly params: Promise<{ readonly id: string }>;
}

export default async function FinalReportPage({ params }: FinalReportPageProps) {
  const { id } = await params;
  return <FullAnalysisReport analysisId={id} />;
}
