"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Pencil, RefreshCw, ShieldOff } from "lucide-react";

import type {
  AgentState,
  AgentView,
} from "../lib/perfpilot-api";
import { usePerfPilotSession } from "./perfpilot-session-provider";

const stateLabel: Record<AgentState, string> = {
  pending: "等待注册",
  online: "在线",
  offline: "离线",
  revoked: "已撤销",
};

const platformLabel = {
  macos: "macOS",
  windows: "Windows",
  linux: "Linux",
} as const;

function timeLabel(value: string | null): string {
  if (value === null) return "尚未连接";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

export function AgentManagement() {
  const { client, status, team, refreshDevices } = usePerfPilotSession();
  const [agents, setAgents] = useState<readonly AgentView[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [mutationError, setMutationError] = useState<string | null>(null);
  const mutationController = useRef<AbortController | null>(null);
  const canManage = team?.role === "owner" || team?.role === "team_owner";

  const loadAgents = useCallback(
    async (signal: AbortSignal) => {
      if (team === null) return;
      try {
        const response = await client.agents(team.id, signal);
        if (signal.aborted) return;
        setAgents(response.agents);
      } catch {
        if (!signal.aborted) setLoadError("Agent 列表读取失败，请稍后重试。");
      } finally {
        if (!signal.aborted) setLoading(false);
      }
    },
    [client, team],
  );

  useEffect(() => {
    if (status !== "ready" || team === null) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await client.agents(team.id, controller.signal);
        if (controller.signal.aborted) return;
        setAgents(response.agents);
      } catch {
        if (!controller.signal.aborted) {
          setLoadError("Agent 列表读取失败，请稍后重试。");
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [client, status, team]);

  useEffect(
    () => () => {
      mutationController.current?.abort();
    },
    [],
  );

  const rename = async (agent: AgentView) => {
    if (!canManage || team === null || !editingName.trim()) return;
    const controller = new AbortController();
    mutationController.current?.abort();
    mutationController.current = controller;
    setMutationError(null);
    try {
      const updated = await client.renameAgent(
        team.id,
        agent.agent_id,
        editingName,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setAgents((current) =>
        current.map((item) => (item.agent_id === updated.agent_id ? updated : item)),
      );
      setEditingId(null);
      refreshDevices();
    } catch {
      if (!controller.signal.aborted) setMutationError("Agent 改名失败，请稍后重试。");
    } finally {
      if (mutationController.current === controller) mutationController.current = null;
    }
  };

  const revoke = async (agent: AgentView) => {
    if (
      !canManage ||
      team === null ||
      agent.state === "revoked" ||
      !window.confirm(`确定撤销 ${agent.name} 吗？该 Agent 将立即停止接收任务。`)
    ) {
      return;
    }
    const controller = new AbortController();
    mutationController.current?.abort();
    mutationController.current = controller;
    setMutationError(null);
    try {
      const updated = await client.revokeAgent(
        team.id,
        agent.agent_id,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setAgents((current) =>
        current.map((item) => (item.agent_id === updated.agent_id ? updated : item)),
      );
      refreshDevices();
    } catch {
      if (!controller.signal.aborted) setMutationError("Agent 撤销失败，请稍后重试。");
    } finally {
      if (mutationController.current === controller) mutationController.current = null;
    }
  };

  return (
    <div className="agent-management">
      <header className="page-header agent-page-header">
        <div className="page-header-copy">
          <p className="page-eyebrow">设备控制面</p>
          <h1>设备 Agent</h1>
          <p className="page-subtitle">
            管理运行在 macOS、Windows 和 Linux 上的采集端。
          </p>
        </div>
      </header>

      <section className="agent-registration-panel" aria-labelledby="agent-register-title">
        <div>
          <h2 id="agent-register-title">自动接入已开启</h2>
          <p>
            在任意 macOS、Windows 或 Linux 电脑安装并启动 Agent 后，
            服务器会自动注册、保存凭证并建立连接。
          </p>
        </div>
      </section>

      {mutationError ? <p className="agent-management-error" role="alert">{mutationError}</p> : null}

      <section className="agent-list-section" aria-labelledby="agent-list-title">
        <header className="agent-list-heading">
          <div>
            <h2 id="agent-list-title">已连接的 Agent</h2>
            <p>{team ? `${team.name} 团队` : "正在读取团队"}</p>
          </div>
          <button
            type="button"
            className="icon-text-action"
            onClick={() => {
              const controller = new AbortController();
              setLoading(true);
              setLoadError(null);
              void loadAgents(controller.signal);
            }}
            disabled={team === null || loading}
          >
            <RefreshCw aria-hidden="true" />
            刷新
          </button>
        </header>

        {loading ? <p className="agent-list-state" role="status">正在读取 Agent…</p> : null}
        {loadError ? <p className="agent-list-state is-error" role="alert">{loadError}</p> : null}
        {!loading && !loadError && agents.length === 0 ? (
          <div className="agent-list-state">
            <p>尚未发现 Agent。安装并启动后，这里会自动出现，无需注册码。</p>
          </div>
        ) : null}

        {agents.length > 0 ? (
          <ul className="agent-list">
            {agents.map((agent) => (
              <li key={agent.agent_id} className={`agent-row is-${agent.state}`}>
                <div className="agent-row-primary">
                  {editingId === agent.agent_id ? (
                    <div className="agent-rename-form">
                      <label className="sr-only" htmlFor={`agent-name-${agent.agent_id}`}>新 Agent 名称</label>
                      <input
                        id={`agent-name-${agent.agent_id}`}
                        value={editingName}
                        maxLength={200}
                        onChange={(event) => setEditingName(event.target.value)}
                      />
                      <button type="button" onClick={() => void rename(agent)}>保存</button>
                      <button type="button" onClick={() => setEditingId(null)}>取消</button>
                    </div>
                  ) : (
                    <>
                      <strong>{agent.name}</strong>
                      <span>{agent.hostname ?? "尚未注册设备信息"}</span>
                    </>
                  )}
                </div>
                <div className="agent-row-meta">
                  <span className={`agent-state is-${agent.state}`}>{stateLabel[agent.state]}</span>
                  <span>{agent.platform ? platformLabel[agent.platform] : "平台待确认"}</span>
                  <span>{agent.agent_version ? `Agent ${agent.agent_version}` : "版本待确认"}</span>
                  <span>最近心跳 {timeLabel(agent.last_heartbeat_at)}</span>
                </div>
                {canManage ? (
                  <div className="agent-row-actions">
                    <button
                      type="button"
                      aria-label={`重命名 ${agent.name}`}
                      disabled={agent.state === "revoked"}
                      onClick={() => {
                        setEditingId(agent.agent_id);
                        setEditingName(agent.name);
                      }}
                    >
                      <Pencil aria-hidden="true" />
                      重命名
                    </button>
                    <button
                      type="button"
                      aria-label={`撤销 ${agent.name}`}
                      disabled={agent.state === "revoked"}
                      onClick={() => void revoke(agent)}
                    >
                      <ShieldOff aria-hidden="true" />
                      撤销
                    </button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </section>
    </div>
  );
}
