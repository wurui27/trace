"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import type {
  PerfPilotClient,
  SourceBinding,
  SourceWorkspaceView,
} from "../lib/perfpilot-api";
import { useOptionalPerfPilotSession } from "./perfpilot-session-provider";

interface SourceWorkspaceFieldProps {
  readonly client?: PerfPilotClient;
  readonly teamId?: string | null;
  readonly value: SourceBinding | null;
  readonly onChange: (binding: SourceBinding | null) => void;
  readonly disabled?: boolean;
}

const EMPTY_WORKSPACES: readonly SourceWorkspaceView[] = [];

export function SourceWorkspaceField({
  client: providedClient,
  teamId: providedTeamId,
  value,
  onChange,
  disabled = false,
}: SourceWorkspaceFieldProps) {
  const session = useOptionalPerfPilotSession();
  const client = providedClient ?? session?.client;
  const teamId = providedTeamId ?? session?.team?.id ?? null;
  const [loaded, setLoaded] = useState<{
    readonly client: PerfPilotClient | null;
    readonly teamId: string | null;
    readonly workspaces: readonly SourceWorkspaceView[];
    readonly status: "ready" | "error";
  } | null>(null);
  const canLoad = Boolean(
    client && teamId && typeof client.sourceWorkspaces === "function",
  );
  const current =
    canLoad && loaded?.client === client && loaded.teamId === teamId ? loaded : null;
  const workspaces = current?.workspaces ?? EMPTY_WORKSPACES;
  const status = canLoad ? current?.status ?? "loading" : "idle";

  useEffect(() => {
    if (!client || !teamId || typeof client.sourceWorkspaces !== "function") {
      return;
    }
    const controller = new AbortController();
    void client
      .sourceWorkspaces(teamId, controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return;
        setLoaded({
          client,
          teamId,
          workspaces: response.workspaces.filter((item) => item.state === "ready"),
          status: "ready",
        });
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setLoaded({ client, teamId, workspaces: [], status: "error" });
        }
      });
    return () => controller.abort();
  }, [client, teamId]);

  const selected = useMemo(
    () =>
      value === null
        ? null
        : workspaces.find(
            (item) =>
              item.agent_id === value.agent_id &&
              item.workspace_id === value.workspace_id,
          ) ?? null,
    [value, workspaces],
  );

  const chooseWorkspace = (workspaceId: string) => {
    const workspace = workspaces.find((item) => item.workspace_id === workspaceId);
    if (!workspace) {
      onChange(null);
      return;
    }
    onChange({
      provider_kind: workspace.provider_kind,
      agent_id: workspace.agent_id,
      workspace_id: workspace.workspace_id,
      snapshot_policy: workspace.snapshot_policy,
      validation_profile_id: null,
    });
  };

  return (
    <fieldset className="new-analysis-field source-workspace-field" disabled={disabled}>
      <legend>源码工作区（可选）</legend>
      <label htmlFor="source-workspace-select" className="sr-only">
        源码工作区
      </label>
      <select
        id="source-workspace-select"
        aria-label="源码工作区"
        value={selected?.workspace_id ?? ""}
        onChange={(event) => chooseWorkspace(event.target.value)}
      >
        <option value="">暂不关联源码</option>
        {workspaces.map((workspace) => (
          <option key={workspace.workspace_id} value={workspace.workspace_id}>
            {workspace.agent_name} · {workspace.name} · {workspace.git_branch ?? "detached"} ·{" "}
            {workspace.git_head.slice(0, 8)}
            {workspace.tracked_dirty_count > 0
              ? ` · ${workspace.tracked_dirty_count} 项未提交修改`
              : ""}
          </option>
        ))}
      </select>

      {selected && selected.validation_profiles.length > 0 ? (
        <>
          <label htmlFor="source-validation-profile">源码验证方案（可选）</label>
          <select
            id="source-validation-profile"
            value={value?.validation_profile_id ?? ""}
            onChange={(event) =>
              onChange(
                value === null
                  ? null
                  : {
                      ...value,
                      validation_profile_id: event.target.value || null,
                    },
              )
            }
          >
            <option value="">只分析，不自动验证</option>
            {selected.validation_profiles.map((profile) => (
              <option key={profile.profile_id} value={profile.profile_id}>
                {profile.name}
              </option>
            ))}
          </select>
        </>
      ) : null}

      {status === "loading" ? <span>正在读取 Agent 源码工作区…</span> : null}
      {status === "error" ? <span>源码工作区暂时不可用，不影响本次分析。</span> : null}
      {status === "ready" && workspaces.length === 0 ? (
        <span>
          尚无可用工作区。请在开发机运行 Agent 工作区注册命令，或前往{" "}
          <Link href="/agents" prefetch={false}>Agent 管理</Link>。
        </span>
      ) : null}
      <span className="new-analysis-field-hint">
        网页只保存 Agent 和工作区 ID，本地源码路径不会上传。
      </span>
    </fieldset>
  );
}
