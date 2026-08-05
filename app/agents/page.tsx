import { AgentManagement } from "../components/agent-management";
import { AppShell } from "../components/app-shell";

export default function AgentsPage() {
  return (
    <AppShell activeItem="agents">
      <AgentManagement />
    </AppShell>
  );
}
