import { AppShell } from "../components/app-shell";
import { PlaceholderPage } from "../components/placeholder-page";
import { getDashboardData } from "../lib/performance-data";

export default function TestsPage() {
  const data = getDashboardData();

  return (
    <AppShell activeItem="tests" app={data.app} device={data.device}>
      <PlaceholderPage title="测试" />
    </AppShell>
  );
}
