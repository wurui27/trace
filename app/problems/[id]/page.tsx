import { notFound } from "next/navigation";

interface ProblemPageProps {
  readonly params: Promise<{
    readonly id: string;
  }>;
}

export default async function ProblemPage({ params }: ProblemPageProps) {
  await params;
  notFound();
}
