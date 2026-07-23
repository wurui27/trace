import type { ReactNode } from "react";
import Link from "next/link";
import {
  CheckCircle2,
  CircleAlert,
  FlaskConical,
  GitCompare,
  Layers3,
  LayoutDashboard,
  Smartphone,
  UserRound,
} from "lucide-react";

import type { DashboardData } from "../lib/performance-data";

type ActiveItem =
  | "overview"
  | "tests"
  | "scenarios"
  | "problems"
  | "comparisons";

interface AppShellProps {
  readonly activeItem: ActiveItem;
  readonly app: DashboardData["app"];
  readonly device: DashboardData["device"];
  readonly children: ReactNode;
}

const navigationItems = [
  {
    id: "overview",
    label: "总览",
    href: "/",
    icon: LayoutDashboard,
  },
  {
    id: "tests",
    label: "测试",
    href: "/tests",
    icon: FlaskConical,
  },
  {
    id: "scenarios",
    label: "场景",
    href: "/scenarios",
    icon: Layers3,
  },
  {
    id: "problems",
    label: "问题",
    href: "/problems",
    icon: CircleAlert,
  },
  {
    id: "comparisons",
    label: "对比",
    href: "/comparisons",
    icon: GitCompare,
  },
] as const;

export function AppShell({
  activeItem,
  app,
  device,
  children,
}: AppShellProps) {
  const verificationLabel = device.verified ? "已验证" : "未验证";

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <Link className="sidebar-brand" href="/" aria-label="PerfPilot 首页">
          <span className="brand-mark" aria-hidden="true">
            <span className="brand-mark-bar brand-mark-bar-short" />
            <span className="brand-mark-bar brand-mark-bar-medium" />
            <span className="brand-mark-bar brand-mark-bar-tall" />
          </span>
          <span className="brand-name">PerfPilot</span>
        </Link>

        <nav className="sidebar-navigation" aria-label="主导航">
          <ul className="navigation-list">
            {navigationItems.map((item) => {
              const Icon = item.icon;
              const isActive = activeItem === item.id;

              return (
                <li key={item.id}>
                  <Link
                    className={`navigation-link${isActive ? " is-active" : ""}`}
                    href={item.href}
                    aria-current={isActive ? "page" : undefined}
                  >
                    <Icon className="navigation-icon" aria-hidden="true" />
                    <span>{item.label}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="sidebar-footer">
          <div
            className="connected-device"
            aria-label={`${device.name}，${verificationLabel}，已连接，${device.os}`}
          >
            <span className="device-icon">
              <Smartphone aria-hidden="true" />
            </span>
            <span className="device-details">
              <strong>{device.name}</strong>
              <span className="device-connection">
                <CheckCircle2 aria-hidden="true" />
                {verificationLabel} · 已连接
              </span>
              <span className="device-os">{device.os}</span>
            </span>
          </div>
        </div>
      </aside>

      <div className="app-workspace">
        <header className="top-bar">
          <div className="current-app">
            <span className="app-icon" aria-hidden="true">
              <span className="app-icon-frame">
                <span className="app-icon-sun" />
                <span className="app-icon-landscape" />
              </span>
            </span>
            <span className="current-app-details">
              <strong>{app.name}</strong>
              <code>{app.packageName}</code>
            </span>
          </div>

          <div className="current-user" aria-label="当前用户：林墨，Android 团队">
            <span className="user-avatar" aria-hidden="true">
              <UserRound />
            </span>
            <span className="user-details">
              <strong>林墨</strong>
              <span>Android 团队</span>
            </span>
          </div>
        </header>

        <main id="main-content" className="main-content">
          {children}
        </main>
      </div>
    </div>
  );
}
