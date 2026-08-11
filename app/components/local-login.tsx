"use client";

import { type FormEvent, useState, type ReactNode } from "react";

import { usePerfPilotSession } from "./perfpilot-session-provider";

interface LocalLoginProps {
  readonly children: ReactNode;
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
      <main>
        <h1>修改初始密码</h1>
        <form onSubmit={submitPasswordChange}>
          <label>当前密码<input aria-label="当前密码" type="password" value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} autoComplete="current-password" /></label>
          <label>新密码<input aria-label="新密码" type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} autoComplete="new-password" /></label>
          <label>确认新密码<input aria-label="确认新密码" type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} autoComplete="new-password" /></label>
          <button type="submit">修改密码</button>
        </form>
        {formError || error ? <p role="alert">{formError ?? error}</p> : null}
      </main>
    );
  }
  if (status === "signed_out") {
    return (
      <main>
        <h1>登录</h1>
        <form onSubmit={submitLogin}>
          <label>账号<input aria-label="账号" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></label>
          <label>密码<input aria-label="密码" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></label>
          <button type="submit">登录</button>
        </form>
        {formError || error ? <p role="alert">{formError ?? error}</p> : null}
      </main>
    );
  }
  return <main aria-busy="true">正在验证本地会话…</main>;
}
