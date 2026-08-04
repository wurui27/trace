import { AppShell } from "../components/app-shell";
import { PlaceholderPage } from "../components/placeholder-page";

export default function TestsPage() {
  return (
    <AppShell activeItem="tests">
      <PlaceholderPage title="测试" />
    </AppShell>
  );
}
