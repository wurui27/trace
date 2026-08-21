import { AppShell } from "../components/app-shell";
import { AnalysisHistory } from "../components/analysis-history";

export default function TestsPage() {
  return (
    <AppShell activeItem="tests">
      <AnalysisHistory />
    </AppShell>
  );
}
