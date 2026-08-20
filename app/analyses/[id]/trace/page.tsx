import { TraceEvidenceLocator } from "../../../components/trace-evidence-locator";

interface TraceEvidencePageProps {
  readonly params: Promise<{ readonly id: string }>;
  readonly searchParams: Promise<{ readonly evidence?: string }>;
}

export default async function TraceEvidencePage({ params, searchParams }: TraceEvidencePageProps) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  return <TraceEvidenceLocator analysisId={id} evidenceId={query.evidence ?? ""} />;
}
