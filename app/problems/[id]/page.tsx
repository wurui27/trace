import { notFound } from "next/navigation";

import { AppShell } from "../../components/app-shell";
import { ProblemDetail } from "../../components/problem-detail";
import { getDashboardData, getProblem } from "../../lib/performance-data";

interface ProblemPageProps {
  readonly params: Promise<{
    readonly id: string;
  }>;
}

export default async function ProblemPage({ params }: ProblemPageProps) {
  const { id } = await params;
  const data = getDashboardData();
  const problem = getProblem(id);

  if (!problem) {
    notFound();
  }

  return (
    <AppShell activeItem="problems" app={data.app} device={data.device}>
      <ProblemDetail app={data.app} device={data.device} problem={problem} />
    </AppShell>
  );
}
