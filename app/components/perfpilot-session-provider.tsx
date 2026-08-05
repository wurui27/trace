"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  createPerfPilotClient,
  type PerfPilotClient,
  type RemoteDeviceView,
} from "../lib/perfpilot-api";

type SessionStatus = "loading" | "ready" | "error";
type DeviceDirectoryStatus = "loading" | "ready" | "error";

export interface PerfPilotTeamSession {
  readonly id: string;
  readonly name: string;
  readonly role: string;
}

export interface PerfPilotSessionValue {
  readonly client: PerfPilotClient;
  readonly status: SessionStatus;
  readonly deviceStatus: DeviceDirectoryStatus;
  readonly team: PerfPilotTeamSession | null;
  readonly devices: readonly RemoteDeviceView[];
  readonly selectedDeviceId: string | null;
  readonly selectedDevice: RemoteDeviceView | null;
  readonly error: string | null;
  readonly selectDevice: (deviceId: string | null) => void;
  readonly refreshDevices: () => void;
}

interface PerfPilotSessionProviderProps {
  readonly children: ReactNode;
  readonly client?: PerfPilotClient;
  readonly pollDelay?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
}

const PerfPilotSessionContext = createContext<PerfPilotSessionValue | null>(null);

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const cancel = () => {
      window.clearTimeout(timer);
      reject(signal.reason);
    };
    const finish = () => {
      signal.removeEventListener("abort", cancel);
      resolve();
    };
    const timer = window.setTimeout(finish, milliseconds);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

export function PerfPilotSessionProvider({
  children,
  client: providedClient,
  pollDelay = wait,
}: PerfPilotSessionProviderProps) {
  const [client] = useState(() => providedClient ?? createPerfPilotClient());
  const [status, setStatus] = useState<SessionStatus>("loading");
  const [deviceStatus, setDeviceStatus] = useState<DeviceDirectoryStatus>("loading");
  const [team, setTeam] = useState<PerfPilotTeamSession | null>(null);
  const [devices, setDevices] = useState<readonly RemoteDeviceView[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const selectionsRef = useRef(new Map<string, string | null>());
  const selectionInitializedRef = useRef(new Set<string>());

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      await client.csrf(controller.signal);
      const me = await client.me(controller.signal);
      const membership = me.memberships[0];
      if (!membership) {
        throw new Error("team_required");
      }
      if (controller.signal.aborted) return;
      setTeam({
        id: membership.team.id,
        name: membership.team.name,
        role: membership.role,
      });
      setStatus("ready");
    })().catch(() => {
      if (controller.signal.aborted) return;
      setTeam(null);
      setStatus("error");
      setError("账号或团队信息读取失败，请刷新页面重试。");
    });
    return () => controller.abort();
  }, [client]);

  useEffect(() => {
    if (team === null) return;
    const controller = new AbortController();
    const teamId = team.id;
    void (async () => {
      while (!controller.signal.aborted) {
        try {
          const response = await client.devices(teamId, controller.signal);
          if (controller.signal.aborted) return;
          const nextDevices = response.devices;
          const previousSelection = selectionsRef.current.get(teamId) ?? null;
          let nextSelection = previousSelection;
          if (previousSelection !== null) {
            const selected = nextDevices.find(
              (device) =>
                device.device_id === previousSelection && device.state === "ready",
            );
            if (!selected) {
              nextSelection = null;
              selectionsRef.current.set(teamId, null);
              selectionInitializedRef.current.add(teamId);
            }
          } else if (!selectionInitializedRef.current.has(teamId)) {
            const firstReady = nextDevices.find((device) => device.state === "ready");
            if (firstReady) {
              nextSelection = firstReady.device_id;
              selectionsRef.current.set(teamId, nextSelection);
              selectionInitializedRef.current.add(teamId);
            }
          }
          setDevices(nextDevices);
          setSelectedDeviceId(nextSelection);
          setDeviceStatus("ready");
        } catch {
          if (controller.signal.aborted) return;
          setDeviceStatus("error");
        }
        await pollDelay(10_000, controller.signal);
      }
    })().catch(() => {
      if (!controller.signal.aborted) setDeviceStatus("error");
    });
    return () => controller.abort();
  }, [client, pollDelay, refreshVersion, team]);

  const selectDevice = useCallback(
    (deviceId: string | null) => {
      if (team === null) return;
      const selectable =
        deviceId === null
          ? null
          : devices.find(
              (device) => device.device_id === deviceId && device.state === "ready",
            )?.device_id ?? null;
      selectionsRef.current.set(team.id, selectable);
      selectionInitializedRef.current.add(team.id);
      setSelectedDeviceId(selectable);
    },
    [devices, team],
  );

  const refreshDevices = useCallback(() => {
    setRefreshVersion((value) => value + 1);
  }, []);

  const selectedDevice = useMemo(
    () =>
      devices.find(
        (device) =>
          device.device_id === selectedDeviceId && device.state === "ready",
      ) ?? null,
    [devices, selectedDeviceId],
  );

  const value = useMemo<PerfPilotSessionValue>(
    () => ({
      client,
      status,
      deviceStatus,
      team,
      devices,
      selectedDeviceId,
      selectedDevice,
      error,
      selectDevice,
      refreshDevices,
    }),
    [
      client,
      status,
      deviceStatus,
      team,
      devices,
      selectedDeviceId,
      selectedDevice,
      error,
      selectDevice,
      refreshDevices,
    ],
  );

  return (
    <PerfPilotSessionContext.Provider value={value}>
      {children}
    </PerfPilotSessionContext.Provider>
  );
}

export function usePerfPilotSession(): PerfPilotSessionValue {
  const value = useContext(PerfPilotSessionContext);
  if (value === null) {
    throw new Error("PerfPilotSessionProvider is required");
  }
  return value;
}

export function useOptionalPerfPilotSession(): PerfPilotSessionValue | null {
  return useContext(PerfPilotSessionContext);
}
