"use client";

import { type FormEvent, useState, type ReactNode } from "react";

import { usePerfPilotSession } from "./perfpilot-session-provider";

interface LocalLoginProps {
  readonly children: ReactNode;
}

interface LocalAuthShellProps {
  readonly children: ReactNode;
  readonly busy?: boolean;
}

function LocalAuthShell({ children, busy = false }: LocalAuthShellProps) {
  return (
    <main className="local-auth-shell" aria-busy={busy || undefined}>
      <section className="local-auth-story" aria-labelledby="local-auth-value">
        <div className="local-auth-brand" aria-label="PerfPilot">
          <span className="local-auth-brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span>PerfPilot</span>
        </div>

        <div className="local-auth-story-content">
          <p className="local-auth-story-label">Android 性能诊断平台</p>
          <h2 id="local-auth-value">从 Trace 直达源码优化</h2>
          <p className="local-auth-story-description">
            性能证据、问题定位与复测建议统一呈现。
          </p>

          <div className="local-auth-trace" aria-hidden="true">
            <span /><span /><span /><span /><span /><span /><span /><span />
          </div>

          <ul className="local-auth-capabilities">
            <li><strong>真实设备</strong><span>远程采集启动与滑动 Trace</span></li>
            <li><strong>证据闭环</strong><span>SmartPerfetto 原始报告独立保留</span></li>
            <li><strong>源码关联</strong><span>直接输出可执行的优化与复测方案</span></li>
          </ul>
        </div>

        <p className="local-auth-environment">私有测试环境</p>
      </section>

      <section className="local-auth-panel">
        <div className="local-auth-card">{children}</div>
      </section>
    </main>
  );
}

export function LocalLogin({ children }: LocalLoginProps) {
  const { status, error, login, changePassword } = usePerfPilotSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  async function submitLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    try {
      await login(username, password);
      setPassword("");
    } catch {
      setFormError("账号或密码错误，请重试。");
    }
  }

  async function submitPasswordChange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    if (newPassword !== confirmPassword) {
      setFormError("两次输入的新密码不一致。");
      return;
    }
    try {
      await changePassword(currentPassword, newPassword);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch {
      setFormError("初始密码修改失败，请检查后重试。");
    }
  }

  if (status === "ready") return <>{children}</>;
  if (status === "password_change_required") {
    return (
      <LocalAuthShell>
        <div className="local-auth-card-heading">
          <p className="local-auth-card-label">首次登录安全设置</p>
          <h1>修改初始密码</h1>
          <p>设置仅由你掌握的新密码，完成后进入工作台。</p>
        </div>
        <form className="local-auth-form" onSubmit={submitPasswordChange}>
          <label htmlFor="local-current-password">当前密码</label>
          <input id="local-current-password" aria-label="当前密码" type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" />
          <label htmlFor="local-new-password">新密码</label>
          <input id="local-new-password" aria-label="新密码" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" />
          <label htmlFor="local-confirm-password">确认新密码</label>
          <input id="local-confirm-password" aria-label="确认新密码" type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" />
          <button type="submit">修改密码</button>
        </form>
        {formError || error ? <p className="local-auth-error" role="alert">{formError ?? error}</p> : null}
      </LocalAuthShell>
    );
  }
  if (status === "signed_out") {
    return (
      <LocalAuthShell>
        <div className="local-auth-card-heading">
          <p className="local-auth-card-label">团队工作台</p>
          <h1>欢迎回来</h1>
          <p>使用管理员分配的账号登录。</p>
        </div>
        <form className="local-auth-form" onSubmit={submitLogin}>
          <label htmlFor="local-username">账号</label>
          <input id="local-username" aria-label="账号" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="请输入账号" />
          <label htmlFor="local-password">密码</label>
          <input id="local-password" aria-label="密码" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" placeholder="请输入密码" />
          <button type="submit">登录</button>
        </form>
        {formError || error ? <p className="local-auth-error" role="alert">{formError ?? error}</p> : null}
        <p className="local-auth-help">首次登录后需要修改初始密码。</p>
      </LocalAuthShell>
    );
  }
  return (
    <LocalAuthShell busy>
      <div className="local-auth-loading" role="status">
        <span className="local-auth-loading-mark" aria-hidden="true" />
        <h1>正在验证本地会话</h1>
        <p>请稍候，正在确认你的账号与团队权限。</p>
      </div>
    </LocalAuthShell>
  );
}
