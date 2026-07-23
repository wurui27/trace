import { AppShell } from "../components/app-shell";
import { PlaceholderPage } from "../components/placeholder-page";
import { getDashboardData } from "../lib/performance-data";

export default function ComparisonsPage() {
  const data = getDashboardData();

  return (
    <AppShell activeItem="comparisons" app={data.app} device={data.device}>
      <PlaceholderPage title="对比" />
    </AppShell>
  );
}
