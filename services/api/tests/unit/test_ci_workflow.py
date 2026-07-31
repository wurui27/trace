from __future__ import annotations

import os
import subprocess
from itertools import product
from pathlib import Path

import pytest
import yaml

WORKFLOW_PATH = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "ci.yml"

CHECKOUT_ACTION = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
SETUP_PYTHON_ACTION = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
SETUP_UV_ACTION = "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e"
SETUP_NODE_ACTION = "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020"


@pytest.fixture(scope="module")
def workflow() -> dict[str, object]:
    assert WORKFLOW_PATH.is_file(), f"required workflow is missing: {WORKFLOW_PATH}"
    loaded = yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _run_steps(job: dict[str, object]) -> list[str]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return [step["run"] for step in steps if "run" in step]


def _action_step(job: dict[str, object], action: str) -> dict[str, object]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return next(step for step in steps if step.get("uses") == action)


def test_workflow_has_required_triggers_permissions_and_concurrency(
    workflow: dict[str, object],
) -> None:
    assert workflow["name"] == "CI"
    assert workflow["on"] == {
        "pull_request": {},
        "push": {"branches": ["main"]},
        "workflow_dispatch": {},
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "true",
    }


def test_workflow_has_only_required_jobs_and_pinned_actions(
    workflow: dict[str, object],
) -> None:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"python-quality", "python-tests", "web", "ci-gate"}

    action_references = {
        step["uses"]
        for job in jobs.values()
        for step in job.get("steps", [])
        if "uses" in step
    }
    assert action_references == {
        CHECKOUT_ACTION,
        SETUP_PYTHON_ACTION,
        SETUP_UV_ACTION,
        SETUP_NODE_ACTION,
    }


def test_python_quality_runs_locked_ruff_checks(workflow: dict[str, object]) -> None:
    quality = workflow["jobs"]["python-quality"]
    assert quality["runs-on"] == "ubuntu-latest"
    assert _action_step(quality, SETUP_PYTHON_ACTION)["with"] == {"python-version": "3.12"}
    assert _action_step(quality, SETUP_UV_ACTION)["with"] == {"version": "0.11.32"}
    assert _run_steps(quality) == [
        "uv sync --locked",
        "uv run --package perfpilot-api ruff check services/api/src services/api/tests",
    ]


def test_python_tests_checkout_upstream_and_require_all_services(
    workflow: dict[str, object],
) -> None:
    tests = workflow["jobs"]["python-tests"]
    assert tests["runs-on"] == "ubuntu-latest"
    assert tests["services"] == {
        "postgres": {
            "image": "postgres:17",
            "env": {
                "POSTGRES_DB": "postgres",
                "POSTGRES_PASSWORD": "postgres",
                "POSTGRES_USER": "postgres",
            },
            "ports": ["5432:5432"],
            "options": (
                "--health-cmd pg_isready --health-interval 10s "
                "--health-timeout 5s --health-retries 5"
            ),
        },
        "redis": {
            "image": "redis:8-alpine",
            "ports": ["6379:6379"],
            "options": (
                '--health-cmd "redis-cli ping" --health-interval 10s '
                "--health-timeout 5s --health-retries 5"
            ),
        },
    }
    assert tests["env"] == {
        "PERFPILOT_TEST_POSTGRES_URL": (
            "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres"
        ),
        "PERFPILOT_TEST_TENANT_ADMIN_URL": (
            "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
        ),
        "PERFPILOT_TEST_REDIS_URL": "redis://127.0.0.1:6379/15",
        "PERFPILOT_REQUIRE_POSTGRES_TESTS": "1",
        "PERFPILOT_REQUIRE_REDIS_TESTS": "1",
        "PERFPILOT_REQUIRE_ANDROID_MEMORY_TESTS": "1",
        "PERFPILOT_ANDROID_MEMORY_ROOT": "${{ github.workspace }}/.ci/android-memory",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    steps = tests["steps"]
    assert steps[0]["uses"] == CHECKOUT_ACTION
    assert steps[1] == {
        "name": "Checkout Android memory analyzer",
        "uses": CHECKOUT_ACTION,
        "with": {
            "repository": "Gracker/Android-App-Memory-Analysis",
            "ref": "d5514972ced78c3faa7fc17589c1ea9231645056",
            "path": ".ci/android-memory",
            "persist-credentials": "false",
        },
    }
    assert _action_step(tests, SETUP_PYTHON_ACTION)["with"] == {"python-version": "3.12"}
    assert _action_step(tests, SETUP_UV_ACTION)["with"] == {"version": "0.11.32"}
    assert _run_steps(tests) == [
        "uv sync --locked",
        (
            "uv run --package perfpilot-api pytest -p no:cacheprovider "
            "services/api/tests -q"
        ),
    ]


def test_web_runs_locked_install_lint_and_tests_from_app(workflow: dict[str, object]) -> None:
    web = workflow["jobs"]["web"]
    assert web["runs-on"] == "ubuntu-latest"
    assert web["defaults"] == {"run": {"working-directory": "app"}}
    assert _action_step(web, SETUP_NODE_ACTION)["with"] == {
        "node-version": "22.13.0",
        "cache": "npm",
        "cache-dependency-path": "app/package-lock.json",
    }
    assert _run_steps(web) == ["npm ci", "npm run lint", "npm test"]


def test_ci_gate_passes_only_when_every_required_job_succeeds(
    workflow: dict[str, object],
) -> None:
    gate = workflow["jobs"]["ci-gate"]
    assert gate["runs-on"] == "ubuntu-latest"
    assert gate["needs"] == ["python-quality", "python-tests", "web"]
    assert gate["if"] == "${{ always() }}"

    steps = gate["steps"]
    assert len(steps) == 1
    gate_step = steps[0]
    result_variables = (
        "PYTHON_QUALITY_RESULT",
        "PYTHON_TESTS_RESULT",
        "WEB_RESULT",
    )
    assert gate_step["env"] == {
        "PYTHON_QUALITY_RESULT": "${{ needs.python-quality.result }}",
        "PYTHON_TESTS_RESULT": "${{ needs.python-tests.result }}",
        "WEB_RESULT": "${{ needs.web.result }}",
    }

    for results in product(("success", "failure", "cancelled", "skipped"), repeat=3):
        environment = os.environ.copy()
        environment.update(dict(zip(result_variables, results, strict=True)))
        completed = subprocess.run(
            ["bash", "-e", "-u", "-o", "pipefail", "-c", gate_step["run"]],
            check=False,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert (completed.returncode == 0) is all(result == "success" for result in results)
