import { AppShell } from "./components/app-shell";
import { Dashboard } from "./components/dashboard";
import { getDashboardData } from "./lib/performance-data";

export default function Home() {
  const data = getDashboardData();

  return (
    <AppShell activeItem="overview" app={data.app} device={data.device}>
      <Dashboard data={data} />
    </AppShell>
  );
}
