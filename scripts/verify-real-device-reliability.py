#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx


_TERMINAL_STATES = frozenset(
    {"completed", "partially_completed", "failed", "canceled", "deleted"}
)
_STAGE_LABELS = {
    "input_validation": "正在校验输入",
    "device_claim": "正在等待远端设备",
    "device_capture": "正在采集真机 Trace",
    "smartperfetto": "SmartPerfetto 正在分析",
    "source_code": "正在读取并匹配源码",
    "perfpilot_ai": "正在生成中文总结",
    "report": "正在发布最终报告",
}


class VerificationFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VerificationConfig:
    __slots__ = (
        "activity",
        "duration_seconds",
        "package",
        "poll_interval_seconds",
        "server_url",
        "source_workspace_id",
        "test_type",
    )

    def __init__(
        self,
        *,
        server_url: str,
        package: str,
        activity: str,
        test_type: str,
        duration_seconds: int,
        source_workspace_id: str,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        parsed = urlsplit(server_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("server URL rejected")
        if not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", package
        ):
            raise ValueError("package rejected")
        if (
            not activity
            or len(activity) > 512
            or not re.fullmatch(r"[A-Za-z0-9_.$/]+", activity)
        ):
            raise ValueError("activity rejected")
        if test_type not in {"cold_start", "hot_start", "scroll"}:
            raise ValueError("test type rejected")
        if type(duration_seconds) is not int or not 1 <= duration_seconds <= 300:
            raise ValueError("duration rejected")
        try:
            workspace_id = UUID(source_workspace_id)
        except ValueError:
            raise ValueError("workspace rejected") from None
        if str(workspace_id) != source_workspace_id:
            raise ValueError("workspace rejected")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll interval rejected")
        self.server_url = server_url.rstrip("/")
        self.package = package
        self.activity = activity
        self.test_type = test_type
        self.duration_seconds = duration_seconds
        self.source_workspace_id = source_workspace_id
        self.poll_interval_seconds = float(poll_interval_seconds)


class ReliabilityClient(Protocol):
    def me(self) -> dict[str, object]: ...

    def agents(self, team_id: str) -> dict[str, object]: ...

    def devices(self, team_id: str) -> dict[str, object]: ...

    def workspaces(self, team_id: str) -> dict[str, object]: ...

    def create_analysis(
        self, team_id: str, payload: dict[str, object]
    ) -> dict[str, object]: ...

    def analysis(self, team_id: str, analysis_id: str) -> dict[str, object]: ...

    def report(self, team_id: str, analysis_id: str) -> dict[str, object]: ...

    def original_html(self, team_id: str, analysis_id: str) -> bytes: ...


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise VerificationFailure(code, "服务器返回内容无效")
    return value


def _items(document: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    raw = document.get(key)
    if not isinstance(raw, list):
        raise VerificationFailure("invalid_server_response", "服务器返回内容无效")
    return tuple(_mapping(item, "invalid_server_response") for item in raw)


def _component(package: str, activity: str) -> str:
    if "/" in activity:
        component = activity
    else:
        component = f"{package}/{activity}"
    if component.split("/", 1)[0] != package:
        raise VerificationFailure("launch_target_invalid", "启动类不属于目标包名")
    return component


def _first_team(me: Mapping[str, object]) -> str:
    memberships = _items(me, "memberships")
    if not memberships:
        raise VerificationFailure("team_membership_unavailable", "当前账号没有可用团队")
    team = _mapping(memberships[0].get("team"), "invalid_server_response")
    team_id = team.get("id")
    if not isinstance(team_id, str):
        raise VerificationFailure("invalid_server_response", "服务器返回内容无效")
    return team_id


def _select_ready_device(
    devices: tuple[Mapping[str, object], ...],
    *,
    package: str,
    component: str,
) -> Mapping[str, object]:
    for device in devices:
        if device.get("state") != "ready":
            continue
        targets = device.get("launch_targets")
        if not isinstance(targets, list):
            continue
        if any(
            isinstance(target, Mapping)
            and target.get("package_name") == package
            and target.get("launch_activity") == component
            for target in targets
        ):
            return device
    raise VerificationFailure(
        "ready_device_unavailable", "没有已连接且包含目标包名的可用设备"
    )


def _select_workspace(
    workspaces: tuple[Mapping[str, object], ...], workspace_id: str
) -> Mapping[str, object]:
    for workspace in workspaces:
        if workspace.get("workspace_id") == workspace_id and workspace.get("state") == "ready":
            return workspace
    raise VerificationFailure("source_workspace_unavailable", "源码工作区尚未就绪")


def _has_chinese(value: object) -> bool:
    return isinstance(value, str) and re.search(r"[\u4e00-\u9fff]", value) is not None


def verify_reliability(
    client: ReliabilityClient,
    config: VerificationConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> dict[str, object]:
    team_id = _first_team(_mapping(client.me(), "authentication_required"))
    agents = _items(_mapping(client.agents(team_id), "invalid_server_response"), "agents")
    online_agents = {
        str(agent.get("agent_id"))
        for agent in agents
        if agent.get("state") == "online" and isinstance(agent.get("agent_id"), str)
    }
    if not online_agents:
        raise VerificationFailure("approved_agent_unavailable", "没有在线的已授权 Agent")

    component = _component(config.package, config.activity)
    device = _select_ready_device(
        _items(_mapping(client.devices(team_id), "invalid_server_response"), "devices"),
        package=config.package,
        component=component,
    )
    agent_id = device.get("agent_id")
    device_id = device.get("device_id")
    if not isinstance(agent_id, str) or agent_id not in online_agents or not isinstance(
        device_id, str
    ):
        raise VerificationFailure("ready_device_unavailable", "远端设备没有在线 Agent")
    workspace = _select_workspace(
        _items(
            _mapping(client.workspaces(team_id), "invalid_server_response"),
            "workspaces",
        ),
        config.source_workspace_id,
    )
    if workspace.get("agent_id") != agent_id:
        raise VerificationFailure(
            "source_workspace_agent_mismatch", "源码工作区与设备不属于同一 Agent"
        )
    profiles = workspace.get("validation_profiles")
    profile_id: str | None = None
    if isinstance(profiles, list) and profiles:
        first_profile = _mapping(profiles[0], "invalid_server_response")
        raw_profile_id = first_profile.get("profile_id")
        if isinstance(raw_profile_id, str):
            profile_id = raw_profile_id

    payload: dict[str, object] = {
        "schema_version": "1.3",
        "analysis_mode": "device",
        "device_id": device_id,
        "test_type": config.test_type,
        "launch_mode": "manual" if config.test_type == "scroll" else "automatic",
        "duration_seconds": config.duration_seconds,
        "target": {
            "package_name": config.package,
            "launch_activity": component,
        },
        "source_binding": {
            "provider_kind": "agent_workspace",
            "agent_id": agent_id,
            "workspace_id": config.source_workspace_id,
            "snapshot_policy": "tracked_worktree",
            "validation_profile_id": profile_id,
        },
    }
    created = _mapping(
        client.create_analysis(team_id, payload), "analysis_create_failed"
    )
    analysis_id = created.get("analysis_id")
    if created.get("schema_version") != "1.3" or not isinstance(analysis_id, str):
        raise VerificationFailure("invalid_server_response", "分析创建响应无效")

    observed: list[str] = []
    last_line: tuple[str, str, str] | None = None
    while True:
        current = _mapping(
            client.analysis(team_id, analysis_id), "analysis_read_failed"
        )
        if current.get("schema_version") != "1.3":
            raise VerificationFailure("invalid_server_response", "分析状态不是 1.3")
        runtime = _mapping(current.get("runtime_status"), "invalid_server_response")
        stage = runtime.get("current_stage")
        stage_state = runtime.get("stage_state")
        summary = runtime.get("progress_summary")
        if not all(isinstance(value, str) for value in (stage, stage_state, summary)):
            raise VerificationFailure("invalid_server_response", "分析进度响应无效")
        line = (stage, stage_state, summary)
        if line != last_line:
            emit(f"{_STAGE_LABELS.get(stage, stage)}：{summary}")
            last_line = line
        if stage not in observed:
            observed.append(stage)
        state = current.get("state")
        if state in _TERMINAL_STATES:
            if state != "completed":
                raise VerificationFailure(
                    "analysis_not_completed", "真实设备分析没有完整完成"
                )
            break
        sleep(config.poll_interval_seconds)

    report = _mapping(client.report(team_id, analysis_id), "report_unavailable")
    repeated_report = _mapping(
        client.report(team_id, analysis_id), "report_unavailable"
    )
    synthesis = _mapping(report.get("synthesis"), "invalid_server_response")
    provenance = _mapping(
        synthesis.get("provenance"), "invalid_server_response"
    )
    if report != repeated_report or provenance.get("generation") != 1:
        raise VerificationFailure("report_generation_conflict", "报告 generation 不唯一")
    if report.get("schema_version") != "1.2":
        raise VerificationFailure("invalid_server_response", "报告版本无效")
    output = _mapping(synthesis.get("output"), "invalid_server_response")
    if synthesis.get("state") != "completed" or not (
        _has_chinese(output.get("verdict"))
        and _has_chinese(output.get("executive_summary"))
    ):
        raise VerificationFailure("chinese_report_invalid", "中文总结未完整生成")
    source = _mapping(report.get("source_code"), "invalid_server_response")
    if (
        source.get("context_state") != "available"
        or source.get("match_summary") != "strong"
        or not _items(source, "source_refs")
    ):
        raise VerificationFailure("source_match_not_strong", "源码没有形成 strong 匹配")
    original = client.original_html(team_id, analysis_id)
    if len(original) > 2 * 1024 * 1024 or not re.match(
        rb"\s*(?:<!doctype\s+html|<html)", original, re.IGNORECASE
    ):
        raise VerificationFailure("smartperfetto_html_invalid", "原始 HTML 无效")

    return {
        "schema_version": "1.0",
        "result": "passed",
        "analysis_id": analysis_id,
        "state": "completed",
        "generation": 1,
        "observed_stages": observed,
        "report_schema_version": "1.2",
        "source_match": "strong",
        "original_html_verified": True,
    }


class HttpReliabilityClient:
    def __init__(
        self, *, server_url: str, username: str, password: str
    ) -> None:
        parsed = urlsplit(server_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._client = httpx.Client(
            base_url=server_url,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, read=120.0),
        )
        csrf = self._request("GET", "/v1/auth/csrf")
        token = csrf.get("csrf_token")
        if not isinstance(token, str):
            raise VerificationFailure("authentication_failed", "无法初始化登录会话")
        login = self._request(
            "POST",
            "/v1/auth/login",
            headers={"origin": self._origin, "x-csrf-token": token},
            json={"username": username, "password": password},
        )
        csrf_token = login.get("csrf_token")
        if not isinstance(csrf_token, str):
            raise VerificationFailure("authentication_failed", "账号登录失败")
        self._csrf_token = csrf_token

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
        response = self._client.request(method, path, **kwargs)
        try:
            document = response.json()
        except ValueError:
            document = None
        if not response.is_success or not isinstance(document, dict):
            raise VerificationFailure("server_request_failed", "服务器请求失败")
        return document

    def me(self) -> dict[str, object]:
        return self._request("GET", "/v1/me")

    def agents(self, team_id: str) -> dict[str, object]:
        return self._request("GET", f"/v1/teams/{team_id}/agents")

    def devices(self, team_id: str) -> dict[str, object]:
        return self._request("GET", f"/v1/teams/{team_id}/devices")

    def workspaces(self, team_id: str) -> dict[str, object]:
        return self._request("GET", f"/v1/teams/{team_id}/source-workspaces")

    def create_analysis(
        self, team_id: str, payload: dict[str, object]
    ) -> dict[str, object]:
        return self._request(
            "POST",
            f"/v1/teams/{team_id}/analyses",
            headers={
                "origin": self._origin,
                "x-csrf-token": self._csrf_token,
            },
            json=payload,
        )

    def analysis(self, team_id: str, analysis_id: str) -> dict[str, object]:
        return self._request("GET", f"/v1/teams/{team_id}/analyses/{analysis_id}")

    def report(self, team_id: str, analysis_id: str) -> dict[str, object]:
        return self._request(
            "GET", f"/v1/teams/{team_id}/analyses/{analysis_id}/report"
        )

    def original_html(self, team_id: str, analysis_id: str) -> bytes:
        response = self._client.get(
            f"/v1/teams/{team_id}/analyses/{analysis_id}/smartperfetto-original",
            params={"download": "true"},
        )
        if not response.is_success or response.headers.get("content-type", "").split(
            ";", 1
        )[0] != "text/html":
            raise VerificationFailure(
                "smartperfetto_html_unavailable", "SmartPerfetto 原始 HTML 不可用"
            )
        return response.content


def parse_args(argv: Sequence[str] | None = None) -> VerificationConfig:
    parser = argparse.ArgumentParser(description="验证 PerfPilot 真实设备可靠性")
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--activity", required=True)
    parser.add_argument(
        "--test-type", required=True, choices=("cold_start", "hot_start", "scroll")
    )
    parser.add_argument("--duration-seconds", required=True, type=int)
    parser.add_argument("--source-workspace-id", required=True)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    arguments = parser.parse_args(argv)
    return VerificationConfig(
        server_url=arguments.server_url,
        package=arguments.package,
        activity=arguments.activity,
        test_type=arguments.test_type,
        duration_seconds=arguments.duration_seconds,
        source_workspace_id=arguments.source_workspace_id,
        poll_interval_seconds=arguments.poll_interval_seconds,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = parse_args(argv)
        username = input("PerfPilot 账号：").strip()
        password = getpass.getpass("PerfPilot 密码：")
        if not username or not password:
            raise VerificationFailure("authentication_required", "账号和密码不能为空")
        client = HttpReliabilityClient(
            server_url=config.server_url, username=username, password=password
        )
        try:
            summary = verify_reliability(client, config)
        finally:
            client.close()
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except VerificationFailure as error:
        print(
            json.dumps(
                {"schema_version": "1.0", "result": "failed", "code": error.code},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (KeyboardInterrupt, EOFError):
        print(
            json.dumps(
                {"schema_version": "1.0", "result": "canceled"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
