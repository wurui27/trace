import { AppShell } from "../components/app-shell";
import { PlaceholderPage } from "../components/placeholder-page";

export default function ScenariosPage() {
  return (
    <AppShell activeItem="scenarios">
      <PlaceholderPage title="场景" />
    </AppShell>
  );
}
