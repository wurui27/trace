import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from perfpilot_api.db.control.models import TenantResource
from perfpilot_api.errors import ApiError
from perfpilot_api.secrets.base import SecretContext, SecretStore

_DATABASE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_TENANT_STORE_MESSAGE = "团队数据存储暂时不可用"


class TenantRouteError(ApiError):
    """A stable, redacted tenant-store failure suitable for the API boundary."""

    def __init__(self) -> None:
        super().__init__(
            code="tenant_store_unavailable",
            message=_TENANT_STORE_MESSAGE,
            status_code=503,
            retryable=True,
        )


@dataclass(frozen=True, slots=True)
class TenantClusterEndpoint:
    """The server-owned PostgreSQL endpoint shared by tenant databases."""

    host: str
    port: int = 5432
    sslmode: str = "verify-full"

    def __post_init__(self) -> None:
        if (
            not self.host
            or self.host != self.host.strip()
            or any(character in self.host for character in "/?#@")
        ):
            raise ValueError("invalid tenant cluster host")
        if not 1 <= self.port <= 65535:
            raise ValueError("invalid tenant cluster port")
        if self.sslmode not in {
            "disable",
            "allow",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        }:
            raise ValueError("invalid tenant cluster sslmode")


@dataclass(frozen=True, slots=True)
class TenantRoute:
    """An authoritative control-plane mapping for one tenant resource version."""

    team_id: UUID
    resource_id: UUID
    resource_version: int
    credential_version: int
    database_name: str
    database_role_name: str
    database_secret_ref: str
    write_paused: bool


class TenantRouteRepository(Protocol):
    async def active_for_team(self, team_id: UUID) -> TenantRoute | None: ...


class SqlAlchemyTenantRouteRepository:
    """Read the one authoritative serving mapping from the control database."""

    def __init__(self, *, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def active_for_team(self, team_id: UUID) -> TenantRoute | None:
        if not isinstance(team_id, UUID):
            return None
        async with self._session_factory() as session:
            resource = await session.scalar(
                select(TenantResource).where(
                    TenantResource.team_id == team_id,
                    TenantResource.state.in_(("active", "migrating")),
                )
            )
        if (
            resource is None
            or resource.database_name is None
            or resource.database_role_name is None
            or resource.database_secret_ref is None
        ):
            return None
        return TenantRoute(
            team_id=resource.team_id,
            resource_id=resource.id,
            resource_version=resource.resource_version,
            credential_version=resource.credential_version,
            database_name=resource.database_name,
            database_role_name=resource.database_role_name,
            database_secret_ref=resource.database_secret_ref,
            write_paused=resource.write_paused,
        )


class _SessionMaker(Protocol):
    def begin(self) -> Any: ...


class _Engine(Protocol):
    def connect(self) -> Any: ...

    async def dispose(self) -> None: ...


EngineFactory = Callable[..., _Engine]
SessionMakerFactory = Callable[[_Engine], _SessionMaker]


def _default_sessionmaker_factory(engine: _Engine) -> _SessionMaker:
    return async_sessionmaker(
        engine,  # type: ignore[arg-type]
        expire_on_commit=False,
    )


@dataclass(slots=True)
class _PoolEntry:
    route: TenantRoute
    engine: _Engine
    sessionmaker: _SessionMaker
    last_used: float
    checked_out: int = 0
    disposed: bool = False


@dataclass(frozen=True, slots=True)
class _BuildFlight:
    route: TenantRoute
    task: asyncio.Task[_PoolEntry]


class TenantRouter:
    """Resolve tenant databases from control state and manage bounded async pools."""

    def __init__(
        self,
        *,
        control_resources: TenantRouteRepository,
        secret_store: SecretStore,
        cluster: TenantClusterEndpoint,
        engine_factory: EngineFactory = create_async_engine,  # type: ignore[assignment]
        sessionmaker_factory: SessionMakerFactory = _default_sessionmaker_factory,
        clock: Callable[[], float] | None = None,
        pool_size: int = 4,
        max_overflow: int = 0,
        pool_timeout_seconds: float = 5.0,
        max_cached_pools: int = 32,
        max_global_checkouts: int = 64,
        idle_timeout_seconds: float = 300.0,
    ) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be positive")
        if max_overflow < 0:
            raise ValueError("max_overflow must be nonnegative")
        if pool_timeout_seconds <= 0:
            raise ValueError("pool_timeout_seconds must be positive")
        if max_cached_pools < 1:
            raise ValueError("max_cached_pools must be positive")
        if max_global_checkouts < 1:
            raise ValueError("max_global_checkouts must be positive")
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")

        self._control_resources = control_resources
        self._secret_store = secret_store
        self._cluster = cluster
        self._engine_factory = engine_factory
        self._sessionmaker_factory = sessionmaker_factory
        self._clock = clock or monotonic
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._per_engine_checkout_limit = pool_size + max_overflow
        self._pool_timeout_seconds = pool_timeout_seconds
        self._max_cached_pools = max_cached_pools
        self._max_global_checkouts = max_global_checkouts
        self._idle_timeout_seconds = idle_timeout_seconds

        self._entries: dict[tuple[UUID, int], _PoolEntry] = {}
        self._inflight: dict[tuple[UUID, int], _BuildFlight] = {}
        self._desired_routes: dict[UUID, TenantRoute] = {}
        self._resource_version_floors: dict[UUID, int] = {}
        self._global_checkouts = 0
        self._closed = False
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self, team_id: UUID) -> AsyncIterator[AsyncSession]:
        """Yield a transaction-scoped session selected only by an authoritative team ID."""

        if not isinstance(team_id, UUID):
            raise TenantRouteError()

        route = await self._resolve_route(team_id)
        entry = await self._entry_for(route)
        await self._reserve_checkout(entry)
        try:
            try:
                async with entry.sessionmaker.begin() as tenant_session:
                    tenant_session.info["team_id"] = team_id
                    tenant_session.info["tenant_resource_id"] = route.resource_id
                    tenant_session.info["tenant_resource_version"] = route.resource_version
                    yield tenant_session
            except SQLAlchemyError:
                raise TenantRouteError() from None
        finally:
            await self._release_checkout(entry)

    async def validate_route(self, route: TenantRoute) -> None:
        """Ping a prospective route without putting its engine into the shared cache."""

        try:
            self._validate_route(route, expected_team_id=route.team_id)
        except Exception:
            raise TenantRouteError() from None

        entry: _PoolEntry | None = None
        try:
            entry = await self._build_entry(route)
            async with entry.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except TenantRouteError:
            raise
        except Exception:
            raise TenantRouteError() from None
        finally:
            if entry is not None:
                await self._dispose_entries((entry,), strict=False)

    async def dispose_idle(self) -> int:
        """Dispose cache entries that have had no checkout for the configured interval."""

        now = self._clock()
        async with self._lock:
            expired = [
                entry
                for entry in self._entries.values()
                if entry.checked_out == 0 and now - entry.last_used >= self._idle_timeout_seconds
            ]
            for entry in expired:
                self._remove_entry_locked(entry)
        await self._dispose_entries(expired)
        return len(expired)

    async def dispose_route(self, team_id: UUID, resource_version: int) -> bool:
        """Stop reuse of one route, for provisioning and credential rotation."""

        if (
            not isinstance(team_id, UUID)
            or type(resource_version) is not int
            or resource_version < 1
        ):
            raise TenantRouteError()
        key = (team_id, resource_version)
        async with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                entry.disposed = True
            flight = self._inflight.pop(key, None)
            if flight is not None:
                flight.task.cancel()
            desired = self._desired_routes.get(team_id)
            if desired is not None and desired.resource_version == resource_version:
                self._desired_routes.pop(team_id, None)
        try:
            if entry is not None:
                await self._dispose_entries((entry,))
        finally:
            await self._drain_flights((flight,) if flight is not None else ())
        return entry is not None

    async def dispose_team(
        self,
        team_id: UUID,
        *,
        keep_resource_version: int | None = None,
    ) -> int:
        """Dispose a team's obsolete pools after an atomic control mapping switch."""

        if not isinstance(team_id, UUID) or (
            keep_resource_version is not None
            and (type(keep_resource_version) is not int or keep_resource_version < 1)
        ):
            raise TenantRouteError()
        async with self._lock:
            current_floor = self._resource_version_floors.get(team_id)
            if (
                keep_resource_version is not None
                and current_floor is not None
                and keep_resource_version < current_floor
            ):
                raise TenantRouteError()
            if keep_resource_version is not None:
                self._resource_version_floors[team_id] = keep_resource_version
            entries = [
                entry
                for key, entry in self._entries.items()
                if key[0] == team_id and key[1] != keep_resource_version
            ]
            for entry in entries:
                self._remove_entry_locked(entry)
            flights = [
                flight
                for key, flight in self._inflight.items()
                if key[0] == team_id and key[1] != keep_resource_version
            ]
            for flight in flights:
                self._inflight.pop(
                    (flight.route.team_id, flight.route.resource_version),
                    None,
                )
                flight.task.cancel()
            desired = self._desired_routes.get(team_id)
            if desired is not None and desired.resource_version != keep_resource_version:
                self._desired_routes.pop(team_id, None)
        try:
            await self._dispose_entries(entries)
        finally:
            await self._drain_flights(flights)
        return len(entries)

    async def dispose(self) -> None:
        """Dispose every cached engine and reject future checkouts."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = list(self._entries.values())
            flights = list(self._inflight.values())
            self._entries.clear()
            self._inflight.clear()
            self._desired_routes.clear()
            self._resource_version_floors.clear()
            for entry in entries:
                entry.disposed = True
            for flight in flights:
                flight.task.cancel()
        try:
            await self._dispose_entries(entries)
        finally:
            await self._drain_flights(flights)

    async def _resolve_route(self, team_id: UUID) -> TenantRoute:
        try:
            route = await self._control_resources.active_for_team(team_id)
            if route is None:
                raise TenantRouteError()
            self._validate_route(route, expected_team_id=team_id)
            if route.write_paused:
                raise TenantRouteError()
            return route
        except TenantRouteError:
            raise
        except Exception:
            raise TenantRouteError() from None

    def _validate_route(self, route: TenantRoute, *, expected_team_id: UUID) -> None:
        if not isinstance(route, TenantRoute) or route.team_id != expected_team_id:
            raise ValueError("invalid tenant route")
        if not isinstance(route.resource_id, UUID):
            raise ValueError("invalid tenant resource")
        if type(route.resource_version) is not int or route.resource_version < 1:
            raise ValueError("invalid tenant resource version")
        if type(route.credential_version) is not int or route.credential_version < 1:
            raise ValueError("invalid tenant credential version")
        if type(route.write_paused) is not bool:
            raise ValueError("invalid tenant write pause state")
        if _DATABASE_IDENTIFIER.fullmatch(route.database_name) is None:
            raise ValueError("invalid tenant database name")
        if _DATABASE_IDENTIFIER.fullmatch(route.database_role_name) is None:
            raise ValueError("invalid tenant database role")
        if not route.database_secret_ref.startswith("secret://"):
            raise ValueError("invalid tenant database secret reference")

    async def _entry_for(self, route: TenantRoute) -> _PoolEntry:
        key = (route.team_id, route.resource_version)
        entries_to_dispose: list[_PoolEntry] = []
        flights_to_cancel: list[_BuildFlight] = []
        cached: _PoolEntry | None = None
        flight: _BuildFlight | None = None
        capacity_unavailable = False
        async with self._lock:
            if self._closed:
                raise TenantRouteError()

            desired = self._desired_routes.get(route.team_id)
            resource_version_floor = self._resource_version_floors.get(route.team_id, 0)
            if route.resource_version < resource_version_floor or (
                desired is not None
                and (
                    route.resource_version < desired.resource_version
                    or route.credential_version < desired.credential_version
                    or (route.resource_version == desired.resource_version and route != desired)
                )
            ):
                raise TenantRouteError()

            now = self._clock()
            for candidate in tuple(self._entries.values()):
                if (
                    candidate.checked_out == 0
                    and now - candidate.last_used >= self._idle_timeout_seconds
                ):
                    self._remove_entry_locked(candidate)
                    entries_to_dispose.append(candidate)

            self._desired_routes[route.team_id] = route
            self._resource_version_floors[route.team_id] = route.resource_version
            for candidate_key, candidate in tuple(self._entries.items()):
                if candidate_key[0] == route.team_id and (
                    candidate_key != key or candidate.route != route
                ):
                    self._remove_entry_locked(candidate)
                    entries_to_dispose.append(candidate)

            for candidate_key, candidate_flight in tuple(self._inflight.items()):
                if candidate_key[0] == route.team_id and candidate_key != key:
                    self._inflight.pop(candidate_key)
                    candidate_flight.task.cancel()
                    flights_to_cancel.append(candidate_flight)

            cached = self._entries.get(key)
            if cached is None:
                flight = self._inflight.get(key)
            if cached is None and flight is not None and flight.route != route:
                flight.task.cancel()
                self._inflight.pop(key, None)
                flights_to_cancel.append(flight)
                flight = None

            if cached is None and flight is None:
                while len(self._entries) + len(self._inflight) >= self._max_cached_pools:
                    victim = self._least_recently_used_available_entry_locked()
                    if victim is None:
                        capacity_unavailable = True
                        break
                    self._remove_entry_locked(victim)
                    entries_to_dispose.append(victim)
                if not capacity_unavailable:
                    task = asyncio.create_task(self._build_entry(route))
                    flight = _BuildFlight(route=route, task=task)
                    self._inflight[key] = flight

        await self._drain_flights(flights_to_cancel)
        await self._dispose_entries(entries_to_dispose)
        if capacity_unavailable:
            raise TenantRouteError()
        if cached is not None:
            return cached
        if flight is None:
            raise TenantRouteError()

        try:
            built = await asyncio.shield(flight.task)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            raise TenantRouteError() from None
        except TenantRouteError:
            async with self._lock:
                if self._inflight.get(key) is flight:
                    self._inflight.pop(key, None)
            raise
        except Exception:
            async with self._lock:
                if self._inflight.get(key) is flight:
                    self._inflight.pop(key, None)
            raise TenantRouteError() from None

        dispose_built = False
        async with self._lock:
            registered_flight = self._inflight.get(key)
            cached = self._entries.get(key)
            if cached is built:
                return cached
            if (
                registered_flight is flight
                and not self._closed
                and self._desired_routes.get(route.team_id) == route
            ):
                self._inflight.pop(key, None)
                self._entries[key] = built
                return built
            if registered_flight is flight:
                self._inflight.pop(key, None)
            dispose_built = True

        if dispose_built:
            await self._dispose_entries((built,), strict=False)
        raise TenantRouteError()

    async def _build_entry(self, route: TenantRoute) -> _PoolEntry:
        engine: _Engine | None = None
        try:
            context = SecretContext(
                team_id=route.team_id,
                resource_id=route.resource_id,
                credential_version=route.credential_version,
                purpose="tenant_database_password",
            )
            encoded_password = await self._secret_store.get(
                route.database_secret_ref,
                context=context,
            )
            if not isinstance(encoded_password, bytes):
                raise ValueError("invalid tenant credential")
            password = encoded_password.decode("utf-8")
            if not password or "\x00" in password:
                raise ValueError("invalid tenant credential")
            url = URL.create(
                drivername="postgresql+psycopg",
                username=route.database_role_name,
                password=password,
                host=self._cluster.host,
                port=self._cluster.port,
                database=route.database_name,
                query={"sslmode": self._cluster.sslmode},
            )
            engine = self._engine_factory(
                url,
                pool_pre_ping=True,
                pool_size=self._pool_size,
                max_overflow=self._max_overflow,
                pool_timeout=self._pool_timeout_seconds,
            )
            return _PoolEntry(
                route=route,
                engine=engine,
                sessionmaker=self._sessionmaker_factory(engine),
                last_used=self._clock(),
            )
        except asyncio.CancelledError:
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception:
                    pass
            raise
        except TenantRouteError:
            raise
        except Exception:
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception:
                    pass
            raise TenantRouteError() from None

    async def _reserve_checkout(self, entry: _PoolEntry) -> None:
        key = (entry.route.team_id, entry.route.resource_version)
        async with self._lock:
            if (
                self._closed
                or entry.disposed
                or self._entries.get(key) is not entry
                or entry.checked_out >= self._per_engine_checkout_limit
                or self._global_checkouts >= self._max_global_checkouts
            ):
                raise TenantRouteError()
            entry.checked_out += 1
            self._global_checkouts += 1

    async def _release_checkout(self, entry: _PoolEntry) -> None:
        async with self._lock:
            entry.checked_out -= 1
            self._global_checkouts -= 1
            entry.last_used = self._clock()

    def _least_recently_used_available_entry_locked(self) -> _PoolEntry | None:
        available = [entry for entry in self._entries.values() if entry.checked_out == 0]
        if not available:
            return None
        return min(available, key=lambda entry: entry.last_used)

    def _remove_entry_locked(self, entry: _PoolEntry) -> None:
        key = (entry.route.team_id, entry.route.resource_version)
        if self._entries.get(key) is entry:
            self._entries.pop(key)
        entry.disposed = True

    async def _dispose_entries(
        self,
        entries: list[_PoolEntry] | tuple[_PoolEntry, ...],
        *,
        strict: bool = True,
    ) -> None:
        failed = False
        for entry in entries:
            entry.disposed = True
            try:
                await entry.engine.dispose()
            except Exception:
                failed = True
        if failed and strict:
            raise TenantRouteError()

    async def _drain_flights(
        self,
        flights: list[_BuildFlight] | tuple[_BuildFlight, ...],
    ) -> None:
        if flights:
            await asyncio.gather(
                *(flight.task for flight in flights),
                return_exceptions=True,
            )


__all__ = [
    "SqlAlchemyTenantRouteRepository",
    "TenantClusterEndpoint",
    "TenantRoute",
    "TenantRouteError",
    "TenantRouteRepository",
    "TenantRouter",
]
