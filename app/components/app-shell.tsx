import type { ReactNode } from "react";
import Link from "next/link";
import {
  CircleAlert,
  FlaskConical,
  GitCompare,
  Layers3,
  LayoutDashboard,
  PackageOpen,
  ServerCog,
  UserRound,
} from "lucide-react";

import { ConnectedDevice } from "./connected-device";

type ActiveItem =
  | "overview"
  | "tests"
  | "scenarios"
  | "problems"
  | "comparisons"
  | "agents";

interface AppShellProps {
  readonly activeItem: ActiveItem;
  readonly app?: {
    readonly name: string;
    readonly packageName: string;
  };
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
  {
    id: "agents",
    label: "设备 Agent",
    href: "/agents",
    icon: ServerCog,
  },
] as const;

export function AppShell({
  activeItem,
  app,
  children,
}: AppShellProps) {
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
          <ConnectedDevice />
        </div>
      </aside>

      <div className="app-workspace">
        <header className="top-bar">
          <div className="current-app">
            <span className={`app-icon${app ? "" : " is-empty"}`} aria-hidden="true">
              {app ? (
                <span className="app-icon-frame">
                  <span className="app-icon-sun" />
                  <span className="app-icon-landscape" />
                </span>
              ) : (
                <PackageOpen />
              )}
            </span>
            <span className="current-app-details">
              <strong>{app?.name ?? "尚未选择应用"}</strong>
              <code>{app?.packageName ?? "新建分析后自动识别"}</code>
            </span>
          </div>

          <div className="current-user" aria-label="当前用户：ray_wu，本地管理员">
            <span className="user-avatar" aria-hidden="true">
              <UserRound />
            </span>
            <span className="user-details">
              <strong>ray_wu</strong>
              <span>本地管理员</span>
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
