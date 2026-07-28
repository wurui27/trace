import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError

from perfpilot_api.db.control.models import TenantResource
from perfpilot_api.db.tenant.router import (
    SqlAlchemyTenantRouteRepository,
    TenantClusterEndpoint,
    TenantRoute,
    TenantRouteError,
    TenantRouter,
)
from perfpilot_api.secrets.base import SecretContext, SecretStoreError

TEAM_A = UUID("10000000-0000-4000-8000-000000000001")
TEAM_B = UUID("10000000-0000-4000-8000-000000000002")
RESOURCE_A_V1 = UUID("20000000-0000-4000-8000-000000000001")
RESOURCE_A_V2 = UUID("20000000-0000-4000-8000-000000000002")
RESOURCE_B_V1 = UUID("20000000-0000-4000-8000-000000000003")


def _route(
    *,
    team_id: UUID = TEAM_A,
    resource_id: UUID = RESOURCE_A_V1,
    resource_version: int = 1,
    credential_version: int = 1,
    database_name: str = "pp_team_a_v1",
    database_role_name: str = "pp_team_a_v1_app",
    database_secret_ref: str = "secret://route-a-v1",
    write_paused: bool = False,
) -> TenantRoute:
    return TenantRoute(
        team_id=team_id,
        resource_id=resource_id,
        resource_version=resource_version,
        credential_version=credential_version,
        database_name=database_name,
        database_role_name=database_role_name,
        database_secret_ref=database_secret_ref,
        write_paused=write_paused,
    )


class FakeRouteRepository:
    def __init__(self, routes: dict[UUID, TenantRoute | None]) -> None:
        self.routes = routes
        self.calls: list[UUID] = []
        self.error: Exception | None = None

    async def active_for_team(self, team_id: UUID) -> TenantRoute | None:
        self.calls.append(team_id)
        if self.error is not None:
            raise self.error
        return self.routes.get(team_id)


class FakeControlSession:
    def __init__(self, resource: TenantResource) -> None:
        self.resource = resource
        self.statements: list[object] = []

    async def __aenter__(self) -> "FakeControlSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def scalar(self, statement: object) -> TenantResource:
        self.statements.append(statement)
        return self.resource


class OutOfOrderRouteRepository:
    def __init__(self, stale_route: TenantRoute, current_route: TenantRoute) -> None:
        self.stale_route = stale_route
        self.current_route = current_route
        self.calls = 0
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def active_for_team(self, team_id: UUID) -> TenantRoute | None:
        assert team_id == TEAM_A
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
            return self.stale_route
        return self.current_route


class FakeSecretStore:
    def __init__(self, password: bytes = b"server-side-password") -> None:
        self.password = password
        self.get_calls: list[tuple[str, SecretContext]] = []
        self.error: Exception | None = None
        self.get_started = asyncio.Event()
        self.get_release: asyncio.Event | None = None

    async def get(self, reference: str, *, context: SecretContext) -> bytes:
        self.get_calls.append((reference, context))
        self.get_started.set()
        if self.get_release is not None:
            await self.get_release.wait()
        if self.error is not None:
            raise self.error
        return self.password

    async def put(self, secret: bytes, *, context: SecretContext) -> str:
        raise AssertionError("router must never write secrets")

    async def delete(self, reference: str) -> None:
        raise AssertionError("router must never delete secrets")


class FakeSession:
    def __init__(self) -> None:
        self.info: dict[str, object] = {}


class FakeSessionContext(AbstractAsyncContextManager[FakeSession]):
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine
        self.session = FakeSession()

    async def __aenter__(self) -> FakeSession:
        if self.engine.begin_error is not None:
            raise self.engine.begin_error
        self.engine.open_sessions += 1
        self.engine.max_open_sessions = max(
            self.engine.max_open_sessions,
            self.engine.open_sessions,
        )
        return self.session

    async def __aexit__(self, *_: object) -> None:
        self.engine.open_sessions -= 1


class FakeSessionMaker:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    def begin(self) -> FakeSessionContext:
        return FakeSessionContext(self.engine)


class FakeConnection:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    async def __aenter__(self) -> "FakeConnection":
        if self.engine.connect_error is not None:
            raise self.engine.connect_error
        self.engine.connect_count += 1
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, statement: object) -> None:
        self.engine.executed_statements.append(str(statement))


class FakeEngine:
    def __init__(self, url: URL, kwargs: dict[str, object]) -> None:
        self.url = url
        self.kwargs = kwargs
        self.dispose_count = 0
        self.open_sessions = 0
        self.max_open_sessions = 0
        self.begin_error: Exception | None = None
        self.connect_error: Exception | None = None
        self.connect_count = 0
        self.executed_statements: list[str] = []

    async def dispose(self) -> None:
        self.dispose_count += 1

    def connect(self) -> FakeConnection:
        return FakeConnection(self)


class FakeEngineFactory:
    def __init__(self) -> None:
        self.engines: list[FakeEngine] = []
        self.error: Exception | None = None

    def __call__(self, url: URL, **kwargs: object) -> FakeEngine:
        if self.error is not None:
            raise self.error
        engine = FakeEngine(url, kwargs)
        self.engines.append(engine)
        return engine


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _router(
    repository: FakeRouteRepository,
    secret_store: FakeSecretStore,
    engine_factory: FakeEngineFactory,
    *,
    clock: Callable[[], float] | None = None,
    pool_size: int = 2,
    max_overflow: int = 1,
    max_cached_pools: int = 4,
    max_global_checkouts: int = 8,
    idle_timeout_seconds: float = 60.0,
) -> TenantRouter:
    return TenantRouter(
        control_resources=repository,
        secret_store=secret_store,
        cluster=TenantClusterEndpoint(
            host="tenant-postgres.internal",
            port=5432,
            sslmode="verify-full",
        ),
        engine_factory=cast(Any, engine_factory),
        sessionmaker_factory=cast(Any, FakeSessionMaker),
        clock=clock,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout_seconds=0.25,
        max_cached_pools=max_cached_pools,
        max_global_checkouts=max_global_checkouts,
        idle_timeout_seconds=idle_timeout_seconds,
    )


def _assert_unavailable(error: TenantRouteError) -> None:
    assert error.code == "tenant_store_unavailable"
    assert error.status_code == 503
    assert error.retryable is True


@pytest.mark.asyncio
async def test_session_uses_only_authoritative_mapping_and_fixed_cluster_endpoint() -> None:
    route = _route()
    repository = FakeRouteRepository({TEAM_A: route})
    secret_store = FakeSecretStore(b"p@ss:/?#[]")
    engines = FakeEngineFactory()
    router = _router(repository, secret_store, engines)

    async with router.session(TEAM_A) as session:
        assert session.info == {
            "team_id": TEAM_A,
            "tenant_resource_id": RESOURCE_A_V1,
            "tenant_resource_version": 1,
        }

    assert repository.calls == [TEAM_A]
    assert secret_store.get_calls == [
        (
            "secret://route-a-v1",
            SecretContext(
                team_id=TEAM_A,
                resource_id=RESOURCE_A_V1,
                credential_version=1,
                purpose="tenant_database_password",
            ),
        )
    ]
    assert len(engines.engines) == 1
    url = engines.engines[0].url
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "tenant-postgres.internal"
    assert url.port == 5432
    assert url.database == "pp_team_a_v1"
    assert url.username == "pp_team_a_v1_app"
    assert url.password == "p@ss:/?#[]"
    assert dict(url.query) == {"sslmode": "verify-full"}
    assert "p@ss" not in repr(url)
    assert engines.engines[0].kwargs == {
        "pool_pre_ping": True,
        "pool_size": 2,
        "max_overflow": 1,
        "pool_timeout": 0.25,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "migrating"])
async def test_write_paused_serving_route_is_rejected_before_pool_access(
    state: str,
) -> None:
    resource = TenantResource(
        id=RESOURCE_A_V1,
        team_id=TEAM_A,
        resource_version=1,
        credential_version=1,
        state=state,
        database_name="pp_team_a_v1",
        database_role_name="pp_team_a_v1_app",
        database_secret_ref="secret://route-a-v1",
        write_paused=True,
    )
    control_session = FakeControlSession(resource)
    repository = SqlAlchemyTenantRouteRepository(
        session_factory=cast(Any, lambda: control_session),
    )
    secret_store = FakeSecretStore()
    engines = FakeEngineFactory()
    router = _router(cast(Any, repository), secret_store, engines)

    with pytest.raises(TenantRouteError) as paused_error:
        async with router.session(TEAM_A):
            pass

    _assert_unavailable(paused_error.value)
    assert len(control_session.statements) == 1
    assert secret_store.get_calls == []
    assert engines.engines == []


@pytest.mark.asyncio
async def test_pausing_a_cached_route_blocks_checkout_without_rebuilding_pool() -> None:
    route = _route()
    repository = FakeRouteRepository({TEAM_A: route})
    secret_store = FakeSecretStore()
    engines = FakeEngineFactory()
    router = _router(repository, secret_store, engines)

    async with router.session(TEAM_A):
        pass

    repository.routes[TEAM_A] = replace(route, write_paused=True)
    with pytest.raises(TenantRouteError) as paused_error:
        async with router.session(TEAM_A):
            pass

    _assert_unavailable(paused_error.value)
    assert len(secret_store.get_calls) == 1
    assert len(engines.engines) == 1
    assert engines.engines[0].open_sessions == 0
    assert engines.engines[0].dispose_count == 0

    repository.routes[TEAM_A] = route
    async with router.session(TEAM_A):
        pass

    assert len(secret_store.get_calls) == 1
    assert len(engines.engines) == 1
    assert engines.engines[0].max_open_sessions == 1


@pytest.mark.asyncio
async def test_hostile_caller_values_cannot_select_a_database() -> None:
    repository = FakeRouteRepository({TEAM_A: _route()})
    secret_store = FakeSecretStore()
    engines = FakeEngineFactory()
    router = _router(repository, secret_store, engines)

    with pytest.raises(TenantRouteError) as invalid_team:
        async with router.session(cast(Any, "postgresql+psycopg://attacker@evil/other_tenant")):
            pass
    _assert_unavailable(invalid_team.value)
    assert repository.calls == []
    assert engines.engines == []

    with pytest.raises(TypeError):
        async with router.session(
            TEAM_A,
            database_name="other_tenant",  # type: ignore[call-arg]
        ):
            pass


@pytest.mark.asyncio
async def test_concurrent_first_use_creates_one_engine() -> None:
    repository = FakeRouteRepository({TEAM_A: _route()})
    secret_store = FakeSecretStore()
    secret_store.get_release = asyncio.Event()
    engines = FakeEngineFactory()
    router = _router(repository, secret_store, engines)

    async def use_session() -> None:
        async with router.session(TEAM_A):
            await asyncio.sleep(0)

    first = asyncio.create_task(use_session())
    await secret_store.get_started.wait()
    second = asyncio.create_task(use_session())
    await asyncio.sleep(0)
    secret_store.get_release.set()
    await asyncio.gather(first, second)

    assert len(secret_store.get_calls) == 1
    assert len(engines.engines) == 1
    assert engines.engines[0].max_open_sessions == 2


@pytest.mark.asyncio
async def test_global_cached_pool_limit_never_evicts_a_checked_out_pool() -> None:
    route_b = _route(
        team_id=TEAM_B,
        resource_id=RESOURCE_B_V1,
        database_name="pp_team_b_v1",
        database_role_name="pp_team_b_v1_app",
        database_secret_ref="secret://route-b-v1",
    )
    repository = FakeRouteRepository({TEAM_A: _route(), TEAM_B: route_b})
    secret_store = FakeSecretStore()
    engines = FakeEngineFactory()
    router = _router(
        repository,
        secret_store,
        engines,
        max_cached_pools=1,
    )

    checked_out = router.session(TEAM_A)
    await checked_out.__aenter__()
    with pytest.raises(TenantRouteError) as capacity_error:
        async with router.session(TEAM_B):
            pass
    _assert_unavailable(capacity_error.value)
    assert len(engines.engines) == 1
    assert engines.engines[0].dispose_count == 0

    await checked_out.__aexit__(None, None, None)
    async with router.session(TEAM_B):
        pass
    assert len(engines.engines) == 2
    assert engines.engines[0].dispose_count == 1


@pytest.mark.asyncio
async def test_global_checkout_limit_is_hard_and_recovers_after_release() -> None:
    route_b = _route(
        team_id=TEAM_B,
        resource_id=RESOURCE_B_V1,
        database_name="pp_team_b_v1",
        database_role_name="pp_team_b_v1_app",
        database_secret_ref="secret://route-b-v1",
    )
    repository = FakeRouteRepository({TEAM_A: _route(), TEAM_B: route_b})
    engines = FakeEngineFactory()
    router = _router(
        repository,
        FakeSecretStore(),
        engines,
        max_global_checkouts=1,
    )

    checked_out = router.session(TEAM_A)
    await checked_out.__aenter__()
    with pytest.raises(TenantRouteError) as capacity_error:
        async with router.session(TEAM_B):
            pass
    _assert_unavailable(capacity_error.value)
    assert sum(engine.open_sessions for engine in engines.engines) == 1

    await checked_out.__aexit__(None, None, None)
    async with router.session(TEAM_B):
        pass
    assert sum(engine.open_sessions for engine in engines.engines) == 0


@pytest.mark.asyncio
async def test_per_engine_checkout_limit_is_pool_size_plus_overflow() -> None:
    repository = FakeRouteRepository({TEAM_A: _route()})
    engines = FakeEngineFactory()
    router = _router(
        repository,
        FakeSecretStore(),
        engines,
        pool_size=1,
        max_overflow=0,
        max_global_checkouts=2,
    )

    checked_out = router.session(TEAM_A)
    await checked_out.__aenter__()
    with pytest.raises(TenantRouteError) as capacity_error:
        async with router.session(TEAM_A):
            pass
    _assert_unavailable(capacity_error.value)
    assert engines.engines[0].max_open_sessions == 1
    await checked_out.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_idle_pool_is_disposed_and_recreated() -> None:
    clock = FakeClock()
    repository = FakeRouteRepository({TEAM_A: _route()})
    engines = FakeEngineFactory()
    router = _router(
        repository,
        FakeSecretStore(),
        engines,
        clock=clock,
        idle_timeout_seconds=30,
    )

    async with router.session(TEAM_A):
        pass
    clock.value = 29.999
    assert await router.dispose_idle() == 0
    clock.value = 30.0
    assert await router.dispose_idle() == 1
    assert engines.engines[0].dispose_count == 1

    async with router.session(TEAM_A):
        pass
    assert len(engines.engines) == 2


@pytest.mark.asyncio
async def test_next_route_lookup_sweeps_expired_idle_pools() -> None:
    clock = FakeClock()
    route_b = _route(
        team_id=TEAM_B,
        resource_id=RESOURCE_B_V1,
        database_name="pp_team_b_v1",
        database_role_name="pp_team_b_v1_app",
        database_secret_ref="secret://route-b-v1",
    )
    repository = FakeRouteRepository({TEAM_A: _route(), TEAM_B: route_b})
    engines = FakeEngineFactory()
    router = _router(
        repository,
        FakeSecretStore(),
        engines,
        clock=clock,
        idle_timeout_seconds=30,
    )

    async with router.session(TEAM_A):
        pass
    clock.value = 30
    async with router.session(TEAM_B):
        pass

    assert len(engines.engines) == 2
    assert engines.engines[0].dispose_count == 1


@pytest.mark.asyncio
async def test_resource_version_switch_disposes_old_engine_before_yielding_new_session() -> None:
    route_v1 = _route()
    route_v2 = replace(
        route_v1,
        resource_id=RESOURCE_A_V2,
        resource_version=2,
        credential_version=2,
        database_name="pp_team_a_v2",
        database_role_name="pp_team_a_v2_app",
        database_secret_ref="secret://route-a-v2",
    )
    repository = FakeRouteRepository({TEAM_A: route_v1})
    engines = FakeEngineFactory()
    router = _router(repository, FakeSecretStore(), engines)

    checked_out_v1 = router.session(TEAM_A)
    await checked_out_v1.__aenter__()
    repository.routes[TEAM_A] = route_v2

    async with router.session(TEAM_A) as session_v2:
        assert engines.engines[0].dispose_count == 1
        assert session_v2.info["tenant_resource_version"] == 2
        assert engines.engines[1].url.database == "pp_team_a_v2"

    await checked_out_v1.__aexit__(None, None, None)
    assert engines.engines[0].dispose_count == 1


@pytest.mark.asyncio
async def test_late_stale_lookup_cannot_downgrade_a_newer_resource_version() -> None:
    route_v1 = _route()
    route_v2 = replace(
        route_v1,
        resource_id=RESOURCE_A_V2,
        resource_version=2,
        credential_version=2,
        database_name="pp_team_a_v2",
        database_role_name="pp_team_a_v2_app",
        database_secret_ref="secret://route-a-v2",
    )
    repository = OutOfOrderRouteRepository(route_v1, route_v2)
    engines = FakeEngineFactory()
    router = _router(cast(Any, repository), FakeSecretStore(), engines)

    async def use_stale_lookup() -> None:
        async with router.session(TEAM_A):
            pass

    stale_request = asyncio.create_task(use_stale_lookup())
    await repository.first_started.wait()
    async with router.session(TEAM_A):
        pass
    repository.release_first.set()

    with pytest.raises(TenantRouteError):
        await stale_request
    assert len(engines.engines) == 1
    assert engines.engines[0].url.database == "pp_team_a_v2"
    assert engines.engines[0].dispose_count == 0


@pytest.mark.asyncio
async def test_dispose_team_during_build_does_not_strand_an_inflight_reservation() -> None:
    repository = FakeRouteRepository({TEAM_A: _route()})
    secret_store = FakeSecretStore()
    secret_store.get_release = asyncio.Event()
    engines = FakeEngineFactory()
    router = _router(repository, secret_store, engines, max_cached_pools=1)

    async def use_session() -> None:
        async with router.session(TEAM_A):
            pass

    building_session = asyncio.create_task(use_session())
    await secret_store.get_started.wait()
    assert await router.dispose_team(TEAM_A) == 0
    secret_store.get_release.set()
    with pytest.raises(TenantRouteError):
        await building_session

    secret_store.get_release = None
    async with router.session(TEAM_A):
        pass
    assert len(secret_store.get_calls) == 2
    assert len(engines.engines) == 1


@pytest.mark.asyncio
async def test_validate_route_pings_transient_engine_without_caching_it() -> None:
    route_v2 = _route(
        resource_id=RESOURCE_A_V2,
        resource_version=2,
        credential_version=2,
        database_name="pp_team_a_v2",
        database_role_name="pp_team_a_v2_app",
        database_secret_ref="secret://route-a-v2",
    )
    repository = FakeRouteRepository({})
    engines = FakeEngineFactory()
    router = _router(repository, FakeSecretStore(), engines)

    await router.validate_route(route_v2)

    assert repository.calls == []
    assert len(engines.engines) == 1
    assert engines.engines[0].connect_count == 1
    assert engines.engines[0].executed_statements == ["SELECT 1"]
    assert engines.engines[0].dispose_count == 1
    assert await router.dispose_route(TEAM_A, 2) is False


@pytest.mark.asyncio
async def test_dispose_team_keeps_only_requested_resource_version() -> None:
    route_v1 = _route()
    repository = FakeRouteRepository({TEAM_A: route_v1})
    engines = FakeEngineFactory()
    router = _router(repository, FakeSecretStore(), engines)
    async with router.session(TEAM_A):
        pass

    assert await router.dispose_team(TEAM_A, keep_resource_version=2) == 1
    assert engines.engines[0].dispose_count == 1
    assert await router.dispose_team(TEAM_A, keep_resource_version=2) == 0


@pytest.mark.asyncio
async def test_resource_switch_hook_rejects_a_late_old_route_before_new_first_use() -> None:
    route_v1 = _route()
    route_v2 = replace(
        route_v1,
        resource_id=RESOURCE_A_V2,
        resource_version=2,
        credential_version=2,
        database_name="pp_team_a_v2",
        database_role_name="pp_team_a_v2_app",
        database_secret_ref="secret://route-a-v2",
    )
    repository = FakeRouteRepository({TEAM_A: route_v1})
    engines = FakeEngineFactory()
    router = _router(repository, FakeSecretStore(), engines)

    assert await router.dispose_team(TEAM_A, keep_resource_version=2) == 0
    with pytest.raises(TenantRouteError):
        async with router.session(TEAM_A):
            pass
    assert engines.engines == []

    repository.routes[TEAM_A] = route_v2
    async with router.session(TEAM_A):
        pass
    assert len(engines.engines) == 1
    assert engines.engines[0].url.database == "pp_team_a_v2"


@pytest.mark.asyncio
async def test_stale_resource_switch_hook_cannot_dispose_a_newer_pool() -> None:
    route_v2 = _route(
        resource_id=RESOURCE_A_V2,
        resource_version=2,
        credential_version=2,
        database_name="pp_team_a_v2",
        database_role_name="pp_team_a_v2_app",
        database_secret_ref="secret://route-a-v2",
    )
    repository = FakeRouteRepository({TEAM_A: route_v2})
    engines = FakeEngineFactory()
    router = _router(repository, FakeSecretStore(), engines)
    async with router.session(TEAM_A):
        pass

    with pytest.raises(TenantRouteError):
        await router.dispose_team(TEAM_A, keep_resource_version=1)
    assert engines.engines[0].dispose_count == 0

    async with router.session(TEAM_A):
        pass
    assert len(engines.engines) == 1


@pytest.mark.asyncio
async def test_missing_mapping_is_a_typed_redacted_503() -> None:
    repository = FakeRouteRepository({TEAM_A: None})
    engines = FakeEngineFactory()
    router = _router(repository, FakeSecretStore(), engines)

    with pytest.raises(TenantRouteError) as exc_info:
        async with router.session(TEAM_A):
            pass

    _assert_unavailable(exc_info.value)
    assert str(TEAM_A) not in str(exc_info.value)
    assert engines.engines == []


@pytest.mark.asyncio
async def test_secret_failure_is_a_typed_redacted_503() -> None:
    secret_marker = "database-password-marker"
    repository = FakeRouteRepository({TEAM_A: _route()})
    secret_store = FakeSecretStore()
    secret_store.error = SecretStoreError(secret_marker)
    router = _router(repository, secret_store, FakeEngineFactory())

    with pytest.raises(TenantRouteError) as exc_info:
        async with router.session(TEAM_A):
            pass

    _assert_unavailable(exc_info.value)
    assert secret_marker not in str(exc_info.value)
    assert secret_marker not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_database_checkout_failure_is_a_typed_redacted_503() -> None:
    repository = FakeRouteRepository({TEAM_A: _route()})
    engines = FakeEngineFactory()
    router = _router(repository, FakeSecretStore(), engines)

    first = router.session(TEAM_A)
    await first.__aenter__()
    await first.__aexit__(None, None, None)
    engines.engines[0].begin_error = OperationalError(
        "contains-sensitive-dsn",
        {},
        RuntimeError("database-password-marker"),
    )

    with pytest.raises(TenantRouteError) as exc_info:
        async with router.session(TEAM_A):
            pass

    _assert_unavailable(exc_info.value)
    assert "sensitive" not in str(exc_info.value)
    assert "password-marker" not in repr(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile_route",
    [
        replace(_route(), team_id=TEAM_B),
        replace(_route(), database_name="other_tenant; DROP DATABASE control"),
        replace(_route(), database_role_name='app" SUPERUSER'),
    ],
    ids=["wrong-team", "unsafe-database", "unsafe-role"],
)
async def test_route_must_match_requested_team_and_have_safe_identifiers(
    hostile_route: TenantRoute,
) -> None:
    repository = FakeRouteRepository({TEAM_A: hostile_route})
    engines = FakeEngineFactory()
    router = _router(repository, FakeSecretStore(), engines)

    with pytest.raises(TenantRouteError) as exc_info:
        async with router.session(TEAM_A):
            pass

    _assert_unavailable(exc_info.value)
    assert engines.engines == []
