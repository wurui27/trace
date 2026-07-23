import { AppShell } from "../components/app-shell";
import { PlaceholderPage } from "../components/placeholder-page";
import { getDashboardData } from "../lib/performance-data";

export default function ScenariosPage() {
  const data = getDashboardData();

  return (
    <AppShell activeItem="scenarios" app={data.app} device={data.device}>
      <PlaceholderPage title="场景" />
    </AppShell>
  );
}
