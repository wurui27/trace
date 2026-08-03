from __future__ import annotations

import subprocess
from copy import deepcopy
from itertools import product
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"

CHECKOUT_ACTION = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
SETUP_PYTHON_ACTION = "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
SETUP_UV_ACTION = "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e"
SETUP_NODE_ACTION = "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020"
PREREQUISITE_JOBS = ("python-quality", "python-tests", "web")


def _assert_workflow_policy(workflow: dict[str, object]) -> None:
    assert "defaults" not in workflow

    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    for job_name, job in jobs.items():
        assert isinstance(job, dict)
        assert "continue-on-error" not in job, job_name
        assert "defaults" not in job, job_name
        assert "permissions" not in job, job_name

        steps = job["steps"]
        assert isinstance(steps, list)
        for step_index, step in enumerate(steps):
            assert isinstance(step, dict)
            assert "continue-on-error" not in step, (job_name, step_index)
            assert "shell" not in step, (job_name, step_index)

    for job_name in PREREQUISITE_JOBS:
        job = jobs[job_name]
        assert "needs" not in job, job_name
        assert "if" not in job, job_name
        for step_index, step in enumerate(job["steps"]):
            assert "if" not in step, (job_name, step_index)

    gate = jobs["ci-gate"]
    assert gate.get("if") == "${{ always() }}"
    for step_index, step in enumerate(gate["steps"]):
        assert "if" not in step, ("ci-gate", step_index)

    web = jobs["web"]
    assert "defaults" not in web
    for step_index, step in enumerate(web["steps"]):
        assert "working-directory" not in step, ("web", step_index)


def _assert_checkout_policy(workflow: dict[str, object]) -> None:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    for job_name in PREREQUISITE_JOBS:
        job = jobs[job_name]
        assert isinstance(job, dict)
        steps = job["steps"]
        assert isinstance(steps, list)

        platform_checkout = steps[0]
        assert platform_checkout.get("uses") == CHECKOUT_ACTION, job_name
        assert platform_checkout.get("with") == {
            "persist-credentials": "false"
        }, job_name

        checkout_steps = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/checkout@")
        ]
        expected_checkout_count = 2 if job_name == "python-tests" else 1
        assert len(checkout_steps) == expected_checkout_count, job_name

    tests_steps = jobs["python-tests"]["steps"]
    assert tests_steps[1] == {
        "name": "Checkout Android memory analyzer",
        "uses": CHECKOUT_ACTION,
        "with": {
            "repository": "Gracker/Android-App-Memory-Analysis",
            "ref": "d5514972ced78c3faa7fc17589c1ea9231645056",
            "path": ".ci/android-memory",
            "persist-credentials": "false",
        },
    }


def _assert_workflow_paths(workflow_paths: list[Path]) -> None:
    assert workflow_paths == [WORKFLOW_PATH]


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


def _set_nested(
    root: dict[str, object],
    path: tuple[str | int, ...],
    value: object,
) -> None:
    target: object = root
    for segment in path[:-1]:
        if isinstance(segment, int):
            assert isinstance(target, list)
            target = target[segment]
        else:
            assert isinstance(target, dict)
            target = target[segment]

    leaf = path[-1]
    if isinstance(leaf, int):
        assert isinstance(target, list)
        target[leaf] = value
    else:
        assert isinstance(target, dict)
        target[leaf] = value


def test_actual_workflow_satisfies_strict_policy(workflow: dict[str, object]) -> None:
    _assert_workflow_policy(workflow)


def test_actual_workflow_satisfies_checkout_policy(workflow: dict[str, object]) -> None:
    _assert_checkout_policy(workflow)


def test_workflow_directory_contains_only_ci_workflow() -> None:
    workflow_paths = sorted(
        path
        for path in WORKFLOW_PATH.parent.iterdir()
        if path.suffix in {".yml", ".yaml"}
    )
    _assert_workflow_paths(workflow_paths)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(
            ("jobs", "web", "continue-on-error"),
            "true",
            id="job-continue-on-error",
        ),
        pytest.param(
            ("jobs", "ci-gate", "steps", 0, "continue-on-error"),
            "true",
            id="gate-step-continue-on-error",
        ),
        pytest.param(
            ("jobs", "python-quality", "permissions"),
            {"contents": "write"},
            id="job-permissions",
        ),
        pytest.param(
            ("jobs", "python-tests", "needs"),
            ["python-quality"],
            id="prerequisite-needs",
        ),
        pytest.param(
            ("defaults",),
            {"run": {"working-directory": "app"}},
            id="workflow-working-directory",
        ),
        pytest.param(
            ("jobs", "web", "defaults"),
            {"run": {"working-directory": "app"}},
            id="web-job-working-directory",
        ),
        pytest.param(
            ("jobs", "web", "steps", 2, "working-directory"),
            "app",
            id="web-step-working-directory",
        ),
        pytest.param(
            ("jobs", "ci-gate", "steps", 0, "shell"),
            "true {0}",
            id="gate-step-shell",
        ),
        pytest.param(
            ("jobs", "ci-gate", "defaults"),
            {"run": {"shell": "true {0}"}},
            id="gate-job-shell-default",
        ),
        pytest.param(
            ("jobs", "python-quality", "steps", 3, "shell"),
            "true {0}",
            id="prerequisite-run-step-shell",
        ),
        pytest.param(
            ("jobs", "python-tests", "if"),
            "${{ false }}",
            id="prerequisite-job-if",
        ),
        pytest.param(
            ("jobs", "python-tests", "steps", 5, "if"),
            "${{ false }}",
            id="prerequisite-test-step-if",
        ),
        pytest.param(
            ("jobs", "ci-gate", "steps", 0, "if"),
            "${{ false }}",
            id="gate-step-if",
        ),
    ],
)
def test_policy_rejects_execution_weakening(
    workflow: dict[str, object],
    path: tuple[str | int, ...],
    value: object,
) -> None:
    weakened = deepcopy(workflow)
    _set_nested(weakened, path, value)

    with pytest.raises(AssertionError):
        _assert_workflow_policy(weakened)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(
            ("jobs", "python-quality", "steps", 0, "with", "ref"),
            "main",
            id="quality-checkout-ref",
        ),
        pytest.param(
            ("jobs", "web", "steps", 0, "with", "repository"),
            "attacker/example",
            id="web-checkout-repository",
        ),
    ],
)
def test_checkout_policy_rejects_platform_override(
    workflow: dict[str, object],
    path: tuple[str | int, ...],
    value: object,
) -> None:
    weakened = deepcopy(workflow)
    _set_nested(weakened, path, value)

    with pytest.raises(AssertionError):
        _assert_checkout_policy(weakened)


def test_policy_rejects_a_second_workflow_file() -> None:
    workflow_paths = [WORKFLOW_PATH, WORKFLOW_PATH.with_name("release.yml")]

    with pytest.raises(AssertionError):
        _assert_workflow_paths(workflow_paths)


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


def test_every_checkout_disables_persisted_credentials(
    workflow: dict[str, object],
) -> None:
    checkout_steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses") == CHECKOUT_ACTION
    ]
    assert checkout_steps
    for step in checkout_steps:
        assert step.get("with", {}).get("persist-credentials") == "false"


def test_python_quality_runs_locked_ruff_checks(workflow: dict[str, object]) -> None:
    quality = workflow["jobs"]["python-quality"]
    assert quality["runs-on"] == "ubuntu-latest"
    assert _action_step(quality, SETUP_PYTHON_ACTION)["with"] == {"python-version": "3.12"}
    assert _action_step(quality, SETUP_UV_ACTION)["with"] == {"version": "0.11.32"}
    assert _run_steps(quality) == [
        "uv sync --locked --all-packages",
        "uv run --locked --package perfpilot-api ruff check services/api/src services/api/tests",
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
        "PERFPILOT_ANDROID_MEMORY_ROOT": "${{ github.workspace }}/.ci/android-memory",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    steps = tests["steps"]
    assert steps[0] == {
        "name": "Checkout platform",
        "uses": CHECKOUT_ACTION,
        "with": {"persist-credentials": "false"},
    }
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
        "uv sync --locked --all-packages",
        (
            "uv run --locked --package perfpilot-api pytest -p no:cacheprovider "
            "services/api/tests -q"
        ),
    ]


def test_web_runs_locked_install_lint_and_tests_from_repository_root(
    workflow: dict[str, object],
) -> None:
    web = workflow["jobs"]["web"]
    assert web["runs-on"] == "ubuntu-latest"
    assert "defaults" not in web
    assert _action_step(web, SETUP_NODE_ACTION)["with"] == {
        "node-version": "22.13.0",
        "cache": "npm",
        "cache-dependency-path": "package-lock.json",
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
        environment = dict(zip(result_variables, results, strict=True))
        completed = subprocess.run(
            ["/bin/bash", "-e", "-u", "-o", "pipefail", "-c", gate_step["run"]],
            check=False,
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert (completed.returncode == 0) is all(
            result == "success" for result in results
        ), (
            f"results={results!r}, returncode={completed.returncode}, "
            f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
        )
