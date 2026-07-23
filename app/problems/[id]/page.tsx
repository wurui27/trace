import { AppShell } from "../../components/app-shell";
import { PlaceholderPage } from "../../components/placeholder-page";
import { getDashboardData } from "../../lib/performance-data";

export default function ProblemPlaceholderPage() {
  const data = getDashboardData();

  return (
    <AppShell activeItem="problems" app={data.app} device={data.device}>
      <PlaceholderPage title="问题详情" />
    </AppShell>
  );
}
