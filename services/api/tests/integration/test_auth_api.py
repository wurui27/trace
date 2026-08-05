from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
import jsonschema
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from redis import asyncio as redis_async
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import perfpilot_api.main as main_module
from perfpilot_api.config import Settings
from perfpilot_api.db.control.models import AuthSession, Membership, Team, User
from perfpilot_api.db.control.session import create_control_session_factory
from perfpilot_api.main import create_app
from perfpilot_api.security.csrf import digest_csrf_token
from perfpilot_api.security.sessions import digest_session_token
from perfpilot_api.security.passwords import (
    hash_password,
    normalize_username,
    verify_password,
)
from perfpilot_api.security.proxy_signature import (
    InMemoryReplayStore,
    RedisReplayStore,
    sign_proxy_client_identity,
    sign_proxy_request,
)

from perfpilot_api.services.auth import (
    AuthService,
    InMemoryLoginRateLimiter,
    InvalidCsrfError,
    InvalidCredentialsError,
    InvalidSessionError,
    LastOwnerError,
    LoginRateLimitedError,
    RedisLoginRateLimiter,
    RoleForbiddenError,
    TeamAccessNotFoundError,
)

_API_ROOT = Path(__file__).resolve().parents[2]
_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_POSTGRES_URL_ENV = "PERFPILOT_TEST_POSTGRES_URL"
_REDIS_URL_ENV = "PERFPILOT_TEST_REDIS_URL"
_REQUIRE_REDIS_ENV = "PERFPILOT_REQUIRE_REDIS_TESTS"


def _psycopg_conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture(scope="module")
def auth_control_database_url() -> Iterator[URL]:
    raw_url = os.getenv(_POSTGRES_URL_ENV)
    if raw_url is None:
        pytest.skip(f"set {_POSTGRES_URL_ENV} to run PostgreSQL auth tests")
    admin_url = make_url(raw_url)
    database_name = f"perfpilot_test_auth_{uuid4().hex}"
    database_url = admin_url.set(database=database_name)

    with psycopg.connect(_psycopg_conninfo(admin_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(database_name)
            )
        )

    try:
        migration_root = (_API_ROOT / "migrations" / "control").resolve()
        config = Config(str(migration_root / "alembic.ini"))
        config.set_main_option("script_location", str(migration_root))
        config.set_main_option(
            "sqlalchemy.url",
            database_url.render_as_string(hide_password=False).replace("%", "%%"),
        )
        command.upgrade(config, "head")
        yield database_url
    finally:
        with psycopg.connect(_psycopg_conninfo(admin_url), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


@pytest.fixture
async def auth_session_factory(
    auth_control_database_url: URL,
) -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        auth_control_database_url.render_as_string(hide_password=False),
        poolclass=NullPool,
    )
    factory = create_control_session_factory(engine)
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE TABLE sessions, memberships, teams, users CASCADE")
        )
    try:
        yield factory
    finally:
        await engine.dispose()


class StubTokenSource:
    def __init__(self, session_tokens: list[str], csrf_tokens: list[str]) -> None:
        self._session_tokens = iter(session_tokens)
        self._csrf_tokens = iter(csrf_tokens)

    def new_session_token(self) -> str:
        return next(self._session_tokens)

    def new_csrf_token(self) -> str:
        return next(self._csrf_tokens)


async def _seed_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str = "admin",
    password: str = "correct horse battery staple",
    state: str = "active",
    is_platform_admin: bool = True,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        state=state,
        is_platform_admin=is_platform_admin,
    )
    async with session_factory() as session, session.begin():
        session.add(user)
    return user


async def _seed_team_membership(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: object,
    team_name: str,
    role: str,
) -> tuple[Team, Membership]:
    team = Team(name=team_name, state="active")
    async with session_factory() as session, session.begin():
        session.add(team)
        await session.flush()
        membership = Membership(team_id=team.id, user_id=user_id, role=role)
        session.add(membership)
    return team, membership


async def _seed_membership(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    team_id: object,
    user_id: object,
    role: str,
) -> Membership:
    membership = Membership(team_id=team_id, user_id=user_id, role=role)
    async with session_factory() as session, session.begin():
        session.add(membership)
    return membership


async def _seed_authenticated_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    token: str,
    csrf_token: str,
    now: datetime,
) -> AuthSession:
    auth_session = AuthSession(
        user_id=user_id,
        token_digest=digest_session_token(token),
        kind="authenticated",
        csrf_secret_hash=digest_csrf_token(csrf_token),
        last_seen_at=now,
        absolute_expires_at=now + timedelta(days=7),
        revoked_at=None,
    )
    async with session_factory() as session, session.begin():
        session.add(auth_session)
    return auth_session


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["team_owner", "team_member"])
async def test_team_write_authorization_accepts_owner_and_member(
    auth_session_factory: async_sessionmaker[AsyncSession],
    role: str,
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    actor = await _seed_user(auth_session_factory, username=f"writer-{role}")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=actor.id,
        team_name=f"write-{role}",
        role=role,
    )
    await _seed_authenticated_session(
        auth_session_factory,
        user_id=actor.id,
        token="team-write-session",
        csrf_token="team-write-csrf",
        now=now,
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
    )

    context = await service.authorize_team_request(
        session_token="team-write-session",
        csrf_token="team-write-csrf",
        team_id=team.id,
        access="write",
    )

    assert context.user_id == actor.id
    assert context.team_id == team.id
    assert context.role == role


@pytest.mark.asyncio
async def test_team_viewer_can_read_but_cannot_write(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    actor = await _seed_user(auth_session_factory, username="viewer")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=actor.id,
        team_name="viewer-team",
        role="team_viewer",
    )
    await _seed_authenticated_session(
        auth_session_factory,
        user_id=actor.id,
        token="team-view-session",
        csrf_token="team-view-csrf",
        now=now,
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
    )

    context = await service.authorize_team_request(
        session_token="team-view-session",
        csrf_token="team-view-csrf",
        team_id=team.id,
        access="read",
    )
    assert context.role == "team_viewer"
    with pytest.raises(RoleForbiddenError):
        await service.authorize_team_request(
            session_token="team-view-session",
            csrf_token="team-view-csrf",
            team_id=team.id,
            access="write",
        )


@pytest.mark.asyncio
async def test_team_authorization_hides_missing_membership(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    actor = await _seed_user(auth_session_factory, username="non-member")
    team = Team(name="unrelated-team", state="active")
    async with auth_session_factory() as session, session.begin():
        session.add(team)
    await _seed_authenticated_session(
        auth_session_factory,
        user_id=actor.id,
        token="non-member-session",
        csrf_token="non-member-csrf",
        now=now,
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
    )

    with pytest.raises(TeamAccessNotFoundError):
        await service.authorize_team_request(
            session_token="non-member-session",
            csrf_token="non-member-csrf",
            team_id=team.id,
            access="read",
        )


@pytest.mark.asyncio
async def test_login_rate_limiter_blocks_the_fifth_failure_and_recovers_after_window() -> None:
    current_time = [1_700_000_000.0]
    limiter = InMemoryLoginRateLimiter(
        clock=lambda: current_time[0],
        window_seconds=900,
        failure_limit=5,
    )

    for _ in range(4):
        await limiter.precheck("admin", "198.51.100.10")
        assert await limiter.record_failure("admin", "198.51.100.10") is False

    await limiter.precheck("admin", "198.51.100.10")
    assert await limiter.record_failure("admin", "198.51.100.10") is True
    with pytest.raises(LoginRateLimitedError):
        await limiter.precheck("admin", "198.51.100.10")

    current_time[0] += 901
    await limiter.precheck("admin", "198.51.100.10")


@pytest.mark.asyncio
async def test_concurrent_login_reservations_only_rate_limit_the_fifth_actual_failure() -> None:
    limiter = InMemoryLoginRateLimiter(
        clock=lambda: 1_700_000_000.0,
        failure_limit=5,
    )
    reservations = [
        await limiter.reserve_attempt("admin", "198.51.100.10")
        for _ in range(5)
    ]

    limited_results = [
        await limiter.finish_failure(reservation)
        for reservation in reservations
    ]

    assert limited_results == [False, False, False, False, True]


@pytest.mark.asyncio
async def test_auth_service_creates_digest_only_ten_minute_pre_auth_session(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
    )

    issued = await service.create_pre_auth_session()

    assert issued.token == "pre-session-token"
    assert issued.csrf_token == "pre-csrf-token"
    assert issued.kind == "pre_auth"
    assert issued.user_id is None
    assert issued.max_age == 600
    async with auth_session_factory() as session:
        stored = (await session.scalars(select(AuthSession))).one()
    assert stored.token_digest == digest_session_token("pre-session-token")
    assert stored.token_digest != "pre-session-token"
    assert stored.csrf_secret_hash == digest_csrf_token("pre-csrf-token")
    assert stored.csrf_secret_hash != "pre-csrf-token"
    assert stored.kind == "pre_auth"
    assert stored.user_id is None
    assert stored.last_seen_at == now
    assert stored.absolute_expires_at == now + timedelta(minutes=10)
    assert stored.revoked_at is None


@pytest.mark.asyncio
async def test_auth_service_validates_pre_auth_kind_expiry_and_updates_last_seen(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    current_time = [datetime(2026, 7, 27, 8, 0, tzinfo=UTC)]
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: current_time[0],
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
    )
    issued = await service.create_pre_auth_session()
    current_time[0] += timedelta(minutes=5)

    session_context = await service.authenticate_session(
        issued.token,
        required_kind="pre_auth",
    )

    assert session_context.kind == "pre_auth"
    assert session_context.user_id is None
    async with auth_session_factory() as session:
        stored = (await session.scalars(select(AuthSession))).one()
    assert stored.last_seen_at == current_time[0]

    current_time[0] += timedelta(minutes=6)
    with pytest.raises(InvalidSessionError):
        await service.authenticate_session(
            issued.token,
            required_kind="pre_auth",
        )


@pytest.mark.asyncio
async def test_login_atomically_revokes_pre_auth_and_issues_authenticated_session(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _seed_user(auth_session_factory)
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    pre_auth = await service.create_pre_auth_session()

    authenticated = await service.login(
        pre_auth_token=pre_auth.token,
        csrf_token=pre_auth.csrf_token,
        username="  ADMIN  ",
        password="correct horse battery staple",
        client_address="198.51.100.10",
    )

    assert authenticated.token == "authenticated-session-token"
    assert authenticated.csrf_token == "authenticated-csrf-token"
    assert authenticated.kind == "authenticated"
    assert authenticated.user_id == user.id
    assert authenticated.max_age == 7 * 24 * 60 * 60
    async with auth_session_factory() as session:
        sessions = (
            await session.scalars(select(AuthSession).order_by(AuthSession.created_at))
        ).all()
    assert len(sessions) == 2
    old_session = next(row for row in sessions if row.kind == "pre_auth")
    new_session = next(row for row in sessions if row.kind == "authenticated")
    assert old_session.revoked_at == now
    assert new_session.user_id == user.id
    assert new_session.token_digest == digest_session_token(
        "authenticated-session-token"
    )
    assert new_session.csrf_secret_hash == digest_csrf_token(
        "authenticated-csrf-token"
    )
    assert new_session.absolute_expires_at == now + timedelta(days=7)


@pytest.mark.parametrize(
    ("username", "password", "user_state"),
    [
        ("missing", "secret-marker", None),
        ("admin", "wrong-secret-marker", "active"),
        ("admin", "correct horse battery staple", "disabled"),
    ],
    ids=["unknown", "wrong-password", "disabled"],
)
@pytest.mark.asyncio
async def test_login_failures_are_identical_and_run_exactly_one_argon2_verify(
    auth_session_factory: async_sessionmaker[AsyncSession],
    username: str,
    password: str,
    user_state: str | None,
) -> None:
    if user_state is not None:
        await _seed_user(auth_session_factory, state=user_state)
    verifier_calls: list[tuple[str, str]] = []

    def counting_verifier(password_hash: str, candidate: str) -> bool:
        verifier_calls.append((password_hash, candidate))
        return verify_password(password_hash, candidate)

    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
        password_verifier=counting_verifier,
    )
    pre_auth = await service.create_pre_auth_session()

    with pytest.raises(InvalidCredentialsError) as exc_info:
        await service.login(
            pre_auth_token=pre_auth.token,
            csrf_token=pre_auth.csrf_token,
            username=username,
            password=password,
            client_address="198.51.100.10",
        )

    assert str(exc_info.value) == "invalid login credentials"
    assert len(verifier_calls) == 1
    assert "secret-marker" not in repr(exc_info.value)
    async with auth_session_factory() as session:
        pre_auth_row = await session.scalar(
            select(AuthSession).where(AuthSession.kind == "pre_auth")
        )
    assert pre_auth_row is not None
    assert pre_auth_row.revoked_at is None


@pytest.mark.asyncio
async def test_concurrent_login_with_one_pre_auth_session_has_one_winner(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(auth_session_factory)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    pre_auth = await service.create_pre_auth_session()

    results = await asyncio.gather(
        *(
            service.login(
                pre_auth_token=pre_auth.token,
                csrf_token=pre_auth.csrf_token,
                username="admin",
                password="correct horse battery staple",
                client_address=f"198.51.100.{index}",
            )
            for index in (10, 11)
        ),
        return_exceptions=True,
    )

    assert sum(result.kind == "authenticated" for result in results if not isinstance(result, Exception)) == 1
    assert sum(isinstance(result, InvalidSessionError) for result in results) == 1
    async with auth_session_factory() as session:
        authenticated_count = len(
            (
                await session.scalars(
                    select(AuthSession).where(AuthSession.kind == "authenticated")
                )
            ).all()
        )
    assert authenticated_count == 1


@pytest.mark.asyncio
async def test_authenticated_session_enforces_twelve_hour_idle_and_active_user(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _seed_user(auth_session_factory)
    current_time = [datetime(2026, 7, 27, 8, 0, tzinfo=UTC)]
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: current_time[0],
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    pre_auth = await service.create_pre_auth_session()
    authenticated = await service.login(
        pre_auth_token=pre_auth.token,
        csrf_token=pre_auth.csrf_token,
        username="admin",
        password="correct horse battery staple",
        client_address="198.51.100.10",
    )

    current_time[0] += timedelta(hours=12)
    context = await service.authenticate_session(
        authenticated.token,
        required_kind="authenticated",
    )
    assert context.user_id == user.id

    current_time[0] += timedelta(hours=12, seconds=1)
    with pytest.raises(InvalidSessionError):
        await service.authenticate_session(
            authenticated.token,
            required_kind="authenticated",
        )

    current_time[0] -= timedelta(hours=12)
    async with auth_session_factory() as session, session.begin():
        await session.execute(
            update(User).where(User.id == user.id).values(state="disabled")
        )
    with pytest.raises(InvalidSessionError):
        await service.authenticate_session(
            authenticated.token,
            required_kind="authenticated",
        )


@pytest.mark.asyncio
async def test_success_clears_only_username_rate_limit_dimension() -> None:
    limiter = InMemoryLoginRateLimiter()

    for _ in range(4):
        await limiter.record_failure("admin", "198.51.100.10")
    await limiter.clear_username("admin")
    await limiter.precheck("admin", "198.51.100.11")
    assert await limiter.record_failure("someone-else", "198.51.100.10") is True
    with pytest.raises(LoginRateLimitedError):
        await limiter.precheck("another-user", "198.51.100.10")


@pytest.mark.asyncio
async def test_rate_limiter_aggregates_username_failures_across_addresses() -> None:
    limiter = InMemoryLoginRateLimiter()

    results = await asyncio.gather(
        *(
            limiter.record_failure("admin", f"198.51.100.{index}")
            for index in range(1, 6)
        )
    )

    assert results.count(True) == 1
    with pytest.raises(LoginRateLimitedError):
        await limiter.precheck("admin", "203.0.113.1")


@pytest.mark.asyncio
async def test_redis_rate_limiter_uses_hashed_keys_and_atomic_two_key_eval() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.results = iter([0, 1, 1])
            self.eval_calls: list[tuple[object, ...]] = []
            self.delete_calls: list[str] = []

        async def eval(
            self,
            script: str,
            numkeys: int,
            *keys_and_args: object,
        ) -> int:
            self.eval_calls.append((script, numkeys, *keys_and_args))
            return next(self.results)

        async def delete(self, key: str) -> int:
            self.delete_calls.append(key)
            return 1

    redis = FakeRedis()
    limiter = RedisLoginRateLimiter(
        redis,
        key_secret=b"rate-limit-key-secret",
        clock=lambda: 1_700_000_000.0,
        nonce_source=lambda: "nonce",
        key_prefix="test:login:",
    )

    await limiter.precheck("admin", "198.51.100.10")
    assert await limiter.record_failure("admin", "198.51.100.10") is True
    with pytest.raises(LoginRateLimitedError):
        await limiter.precheck("admin", "198.51.100.10")
    await limiter.clear_username("admin")

    assert len(redis.eval_calls) == 3
    assert all(call[1] == 2 for call in redis.eval_calls)
    rendered_calls = repr(redis.eval_calls + [tuple(redis.delete_calls)])
    assert "admin" not in rendered_calls
    assert "198.51.100.10" not in rendered_calls
    assert all(
        str(call[2]).startswith("test:login:username:")
        and str(call[3]).startswith("test:login:address:")
        for call in redis.eval_calls
    )
    assert redis.delete_calls == [redis.eval_calls[0][2]]


@pytest.mark.asyncio
async def test_redis_attempt_and_preauth_limiters_use_atomic_hashed_transitions() -> None:
    from perfpilot_api.services.auth import (
        PreAuthSessionRateLimitedError,
        RedisPreAuthSessionLimiter,
    )

    class FakeRedis:
        def __init__(self) -> None:
            self.results = iter([1, 1, 1, 1, 1, 1, 1, 0])
            self.eval_calls: list[tuple[object, ...]] = []
            self.delete_calls: list[str] = []

        async def eval(
            self,
            script: str,
            numkeys: int,
            *keys_and_args: object,
        ) -> int:
            self.eval_calls.append((script, numkeys, *keys_and_args))
            return next(self.results)

        async def delete(self, key: str) -> int:
            self.delete_calls.append(key)
            return 1

    nonces = iter(
        [
            "login-reservation-1",
            "login-reservation-2",
            "login-reservation-3",
            "preauth-issuance-1",
            "preauth-issuance-2",
        ]
    )
    redis = FakeRedis()
    login_limiter = RedisLoginRateLimiter(
        redis,
        key_secret=b"rate-limit-key-secret",
        clock=lambda: 1_700_000_000.0,
        nonce_source=lambda: next(nonces),
        key_prefix="test:login:",
    )
    first = await login_limiter.reserve_attempt(
        "admin",
        "verified-client-id",
    )
    assert await login_limiter.finish_failure(first) is True
    second = await login_limiter.reserve_attempt(
        "admin",
        "verified-client-id",
    )
    await login_limiter.finish_success(second)
    third = await login_limiter.reserve_attempt(
        "admin",
        "verified-client-id",
    )
    await login_limiter.release_attempt(third)

    preauth_limiter = RedisPreAuthSessionLimiter(
        redis,
        key_secret=b"preauth-key-secret",
        nonce_source=lambda: next(nonces),
        clock=lambda: 1_700_000_000.0,
        key_prefix="test:preauth:",
        issuance_limit=1,
    )
    await preauth_limiter.check_and_record("verified-client-id")
    with pytest.raises(PreAuthSessionRateLimitedError):
        await preauth_limiter.check_and_record("verified-client-id")

    login_calls = redis.eval_calls[:6]
    preauth_calls = redis.eval_calls[6:]
    assert len(login_calls) == 6
    assert all(call[1] == 2 for call in login_calls)
    assert all(
        str(call[2]).startswith("test:login:username:")
        and str(call[3]).startswith("test:login:address:")
        for call in login_calls
    )
    assert len({call[0] for call in login_calls}) == 4
    assert len(preauth_calls) == 2
    assert all(call[1] == 1 for call in preauth_calls)
    assert all(
        str(call[2]).startswith("test:preauth:address:")
        for call in preauth_calls
    )
    rendered = repr(redis.eval_calls + [tuple(redis.delete_calls)])
    assert "admin" not in rendered
    assert "verified-client-id" not in rendered
    assert redis.delete_calls == []


@pytest.mark.asyncio
async def test_real_redis_login_reservations_rate_limit_the_fifth_actual_failure() -> None:
    redis_url = os.getenv(_REDIS_URL_ENV)
    if redis_url is None:
        if os.getenv(_REQUIRE_REDIS_ENV) == "1":
            pytest.fail(f"{_REDIS_URL_ENV} is required")
        pytest.skip(f"set {_REDIS_URL_ENV} to run Redis auth tests")

    client = redis_async.from_url(redis_url)
    try:
        await client.flushdb()
        nonces = iter(f"real-redis-reservation-{index}" for index in range(6))
        limiter = RedisLoginRateLimiter(
            client,
            key_secret=b"real-redis-rate-limit-key",
            clock=lambda: 1_700_000_000.0,
            nonce_source=lambda: next(nonces),
            key_prefix="test:real-redis-login:",
            failure_limit=5,
        )
        reservations = [
            await limiter.reserve_attempt("admin", "verified-client-id")
            for _ in range(5)
        ]

        limited_results = [
            await limiter.finish_failure(reservation)
            for reservation in reservations
        ]

        assert limited_results == [False, False, False, False, True]
        with pytest.raises(LoginRateLimitedError):
            await limiter.reserve_attempt("admin", "verified-client-id")
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.mark.asyncio
async def test_login_validates_pre_auth_and_csrf_before_rate_limit_and_argon2(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class TrackingRateLimiter(InMemoryLoginRateLimiter):
        def __init__(self) -> None:
            super().__init__()
            self.prechecks = 0

        async def precheck(
            self,
            normalized_username: str,
            client_address: str,
        ) -> None:
            self.prechecks += 1
            await super().precheck(normalized_username, client_address)

    await _seed_user(auth_session_factory)
    limiter = TrackingRateLimiter()
    verifier_calls: list[tuple[str, str]] = []
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=limiter,
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
        password_verifier=lambda password_hash, password: (
            verifier_calls.append((password_hash, password)) or True
        ),
    )
    pre_auth = await service.create_pre_auth_session()

    with pytest.raises(InvalidCsrfError):
        await service.login(
            pre_auth_token=pre_auth.token,
            csrf_token="wrong-csrf-token",
            username="admin",
            password="secret-marker",
            client_address="198.51.100.10",
        )

    assert limiter.prechecks == 0
    assert verifier_calls == []


def _proxy_headers(
    *,
    method: str,
    target: str,
    body: bytes = b"",
    timestamp: int = 1_700_000_000,
    request_id: str = "req-auth",
) -> dict[str, str]:
    raw_target = target.encode("ascii")
    raw_path, separator, raw_query = raw_target.partition(b"?")
    signature = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=timestamp,
        request_id=request_id,
        method=method,
        raw_path=raw_path,
        raw_query=raw_query if separator else b"",
        body=body,
    )
    return {
        "x-perfpilot-proxy-timestamp": str(timestamp),
        "x-perfpilot-proxy-signature": signature,
        "x-request-id": request_id,
    }


def _login_api_client(
    client: TestClient,
    *,
    username: str,
    request_prefix: str,
) -> str:
    csrf_response = client.get(
        "/v1/auth/csrf",
        headers=_proxy_headers(
            method="GET",
            target="/v1/auth/csrf",
            request_id=f"{request_prefix}-csrf",
        ),
    )
    body = (
        f'{{"username":"{username}",'
        '"password":"correct horse battery staple"}'
    ).encode()
    headers = _proxy_headers(
        method="POST",
        target="/v1/auth/login",
        body=body,
        request_id=f"{request_prefix}-login",
    )
    headers.update(
        {
            "content-type": "application/json",
            "origin": "https://app.example",
            "x-csrf-token": csrf_response.json()["csrf_token"],
        }
    )
    response = client.post("/v1/auth/login", content=body, headers=headers)
    assert response.status_code == 200
    return response.json()["csrf_token"]


@pytest.mark.asyncio
async def test_signed_csrf_endpoint_creates_secure_preauth_cookie_and_no_store_response(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
    )
    settings = Settings(
        app_env="test",
        proxy_secret="test-proxy-secret",
        allowed_origins=["https://app.example"],
        _env_file=None,
    )
    app = create_app(
        testing=True,
        settings_override=settings,
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(method="GET", target="/v1/auth/csrf"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "csrf_token": "pre-csrf-token",
    }
    assert response.headers["cache-control"] == "no-store"
    set_cookie = response.headers["set-cookie"]
    assert "perfpilot_session=pre-session-token" in set_cookie
    assert "Max-Age=600" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Domain=" not in set_cookie


def _auth_test_settings() -> Settings:
    return Settings(
        app_env="test",
        proxy_secret="test-proxy-secret",
        allowed_origins=["https://app.example"],
        _env_file=None,
    )


def test_production_app_wires_and_closes_database_and_redis_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    upload_service = object()

    class FakeEngine:
        async def dispose(self) -> None:
            closed.append("engine")

    class FakeRedis:
        async def aclose(self) -> None:
            closed.append("redis")

        async def set(self, *args: object, **kwargs: object) -> bool:
            return True

        async def eval(self, *args: object, **kwargs: object) -> int:
            return 0

        async def delete(self, *args: object, **kwargs: object) -> int:
            return 1

    class FakeArtifactRuntime:
        def __init__(self) -> None:
            self.upload_service = upload_service
            self.apk_inspector = object()
            self.tenant_router = object()
            self.s3_client = object()
            self.artifact_store = object()
            self.bucket_resolver = object()

        async def close(self) -> None:
            closed.append("artifacts")

    fake_engine = FakeEngine()
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        main_module,
        "create_control_engine",
        lambda database_url: fake_engine,
        raising=False,
    )
    monkeypatch.setattr(
        main_module,
        "create_control_session_factory",
        lambda engine: lambda: None,
        raising=False,
    )

    class FakeRedisModule:
        @staticmethod
        def from_url(redis_url: str) -> FakeRedis:
            return fake_redis

    monkeypatch.setattr(main_module, "redis", FakeRedisModule, raising=False)

    async def build_artifact_runtime(**kwargs: object) -> FakeArtifactRuntime:
        assert kwargs["settings"] is settings
        assert callable(kwargs["control_session_factory"])
        return FakeArtifactRuntime()

    monkeypatch.setattr(
        main_module,
        "build_artifact_runtime",
        build_artifact_runtime,
        raising=False,
    )
    settings = Settings(
        app_env="production",
        control_database_url=(
            "postgresql+psycopg://perfpilot:test-password@db.example.com:5432/"
            "control?sslmode=verify-full"
        ),
        redis_url="rediss://cache.example.com:6380/0",
        s3_endpoint_url="https://objects.example.com",
        s3_region="cn-north-1",
        tenant_cluster_host="tenant-db.example.com",
        tenant_cluster_port=6432,
        tenant_cluster_sslmode="verify-full",
        secret_keyring_config="/run/secrets/perfpilot/keyring.json",
        secret_store_root="/var/lib/perfpilot/secrets",
        apkanalyzer_binary="/bin/echo",
        proxy_secret="production-proxy-secret",
        session_secret="production-session-secret",
        jws_signing_key_reference="kms://keys/perfpilot-signing",
        agent_registration_secret_reference="vault://agent-registration",
        allowed_origins=["https://app.example"],
        _env_file=None,
    )

    app = create_app(
        testing=False,
        settings_override=settings,
        apk_inspector=object(),  # type: ignore[arg-type]
    )

    assert isinstance(app.state.auth_service, AuthService)
    assert isinstance(app.state.proxy_replay_store, RedisReplayStore)
    with TestClient(app, base_url="https://app.example") as client:
        assert app.state.upload_service is upload_service
        assert client.get("/v1/health").status_code == 200
    assert closed == ["artifacts", "redis", "engine"]


@pytest.mark.asyncio
async def test_invalid_proxy_signature_does_not_reserve_request_id_and_is_no_store(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    valid_headers = _proxy_headers(
        method="GET",
        target="/v1/auth/csrf",
        request_id="req-not-reserved",
    )
    invalid_headers = dict(valid_headers)
    invalid_headers["x-perfpilot-proxy-signature"] = "a" * 43

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        rejected = client.get("/v1/auth/csrf", headers=invalid_headers)
        accepted = client.get("/v1/auth/csrf", headers=valid_headers)

    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "proxy_authentication_failed"
    assert rejected.headers["cache-control"] == "no-store"
    assert accepted.status_code == 200


@pytest.mark.asyncio
async def test_proxy_dependency_rejects_repeated_security_headers(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    headers = list(
        _proxy_headers(
            method="GET",
            target="/v1/auth/csrf",
            request_id="req-duplicate-header",
        ).items()
    )
    headers.append(("x-perfpilot-proxy-timestamp", "1700000000"))

    with TestClient(app, base_url="https://app.example") as client:
        response = client.get("/v1/auth/csrf", headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "proxy_authentication_failed"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_proxy_dependency_rejects_body_larger_than_one_mebibyte(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    body = b"x" * (1024 * 1024 + 1)

    with TestClient(app, base_url="https://app.example") as client:
        response = client.request(
            "GET",
            "/v1/auth/csrf",
            content=body,
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                body=body,
                request_id="req-body-too-large",
            ),
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_body_too_large"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_signed_login_rotates_cookie_and_csrf_after_database_commit(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _seed_user(auth_session_factory)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
        client_address_resolver=lambda _: "198.51.100.10",
    )
    login_body = (
        b'{"username":"admin","password":"correct horse battery staple"}'
    )

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-login-csrf",
            ),
        )
        pre_auth_cookie = client.cookies["perfpilot_session"]
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-login",
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        login_response = client.post(
            "/v1/auth/login",
            content=login_body,
            headers=login_headers,
        )
        authenticated_cookie = client.cookies["perfpilot_session"]

    assert login_response.status_code == 200
    assert login_response.json() == {
        "schema_version": "1.0",
        "csrf_token": "authenticated-csrf-token",
    }
    assert login_response.headers["cache-control"] == "no-store"
    assert pre_auth_cookie == "pre-session-token"
    assert authenticated_cookie == "authenticated-session-token"
    assert authenticated_cookie != pre_auth_cookie
    assert "Max-Age=604800" in login_response.headers["set-cookie"]
    async with auth_session_factory() as session:
        authenticated_session = await session.scalar(
            select(AuthSession).where(AuthSession.kind == "authenticated")
        )
    assert authenticated_session is not None
    assert authenticated_session.user_id == user.id


@pytest.mark.parametrize(
    ("username", "password", "user_state"),
    [
        ("missing", "not-the-password", None),
        ("admin", "not-the-password", "active"),
        ("admin", "correct horse battery staple", "disabled"),
    ],
    ids=["unknown", "wrong-password", "disabled"],
)
@pytest.mark.asyncio
async def test_login_api_hides_unknown_wrong_and_disabled_user_difference(
    auth_session_factory: async_sessionmaker[AsyncSession],
    username: str,
    password: str,
    user_state: str | None,
) -> None:
    if user_state is not None:
        await _seed_user(auth_session_factory, state=user_state)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
        client_address_resolver=lambda _: "198.51.100.10",
    )
    login_body = (
        '{"username":"%s","password":"%s"}' % (username, password)
    ).encode()

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-failed-login-csrf",
            ),
        )
        headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-failed-login",
        )
        headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        response = client.post("/v1/auth/login", content=login_body, headers=headers)

    assert response.status_code == 401
    assert response.json() == {
        "schema_version": "1.0",
        "error": {
            "code": "invalid_credentials",
            "message": "用户名或密码错误",
            "retryable": False,
            "request_id": "req-failed-login",
        },
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_login_api_returns_429_on_the_fifth_failed_attempt(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(auth_session_factory)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
        client_address_resolver=lambda _: "198.51.100.10",
    )
    login_body = b'{"username":"admin","password":"wrong-password"}'

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-rate-csrf",
            ),
        )
        statuses: list[int] = []
        for attempt in range(1, 6):
            headers = _proxy_headers(
                method="POST",
                target="/v1/auth/login",
                body=login_body,
                request_id=f"req-rate-{attempt}",
            )
            headers.update(
                {
                    "content-type": "application/json",
                    "origin": "https://app.example",
                    "x-csrf-token": csrf_response.json()["csrf_token"],
                }
            )
            statuses.append(
                client.post(
                    "/v1/auth/login",
                    content=login_body,
                    headers=headers,
                ).status_code
            )

    assert statuses == [401, 401, 401, 401, 429]


@pytest.mark.parametrize(
    "origin",
    [None, "null", "http://app.example", "https://app.example.evil"],
)
@pytest.mark.asyncio
async def test_login_api_rejects_non_exact_origin_before_argon2(
    auth_session_factory: async_sessionmaker[AsyncSession],
    origin: str | None,
) -> None:
    await _seed_user(auth_session_factory)
    verifier_calls: list[tuple[str, str]] = []
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
        password_verifier=lambda password_hash, password: (
            verifier_calls.append((password_hash, password)) or True
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"admin","password":"correct horse battery staple"}'
    )

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-origin-csrf",
            ),
        )
        headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-origin-login",
        )
        headers.update(
            {
                "content-type": "application/json",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        if origin is not None:
            headers["origin"] = origin
        response = client.post("/v1/auth/login", content=login_body, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert verifier_calls == []


@pytest.mark.asyncio
async def test_login_api_rejects_wrong_csrf_before_argon2(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(auth_session_factory)
    verifier_calls: list[tuple[str, str]] = []
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(["pre-session-token"], ["pre-csrf-token"]),
        password_verifier=lambda password_hash, password: (
            verifier_calls.append((password_hash, password)) or True
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"admin","password":"correct horse battery staple"}'
    )

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-wrong-csrf-get",
            ),
        )
        headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-wrong-csrf-login",
        )
        headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": "wrong-csrf-token",
            }
        )
        response = client.post("/v1/auth/login", content=login_body, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_validation_failed"
    assert verifier_calls == []


@pytest.mark.asyncio
async def test_login_request_rejects_extra_properties_without_rotating_session(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(auth_session_factory)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"admin","password":"correct horse battery staple",'
        b'"role":"platform_admin"}'
    )

    with TestClient(app, base_url="https://app.example") as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-extra-csrf",
            ),
        )
        headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-extra-login",
        )
        headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        response = client.post("/v1/auth/login", content=login_body, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
    async with auth_session_factory() as session:
        sessions = (await session.scalars(select(AuthSession))).all()
    assert len(sessions) == 1
    assert sessions[0].kind == "pre_auth"
    assert sessions[0].revoked_at is None


@pytest.mark.asyncio
async def test_csrf_endpoint_rotates_authenticated_csrf_without_downgrading_session(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _seed_user(auth_session_factory)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            [
                "pre-csrf-token",
                "authenticated-csrf-token",
                "rotated-authenticated-csrf-token",
            ],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"admin","password":"correct horse battery staple"}'
    )

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-rotate-csrf-pre",
            ),
        )
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-rotate-csrf-login",
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        login_response = client.post(
            "/v1/auth/login",
            content=login_body,
            headers=login_headers,
        )
        authenticated_cookie = client.cookies["perfpilot_session"]
        rotated_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-rotate-csrf-authenticated",
            ),
        )

    assert login_response.status_code == 200
    assert rotated_response.status_code == 200
    assert rotated_response.json()["csrf_token"] == (
        "rotated-authenticated-csrf-token"
    )
    assert authenticated_cookie == "authenticated-session-token"
    assert "perfpilot_session=authenticated-session-token" in (
        rotated_response.headers["set-cookie"]
    )
    async with auth_session_factory() as session:
        authenticated = await session.scalar(
            select(AuthSession).where(AuthSession.kind == "authenticated")
        )
        session_count = len((await session.scalars(select(AuthSession))).all())
    assert authenticated is not None
    assert authenticated.user_id == user.id
    assert authenticated.csrf_secret_hash == digest_csrf_token(
        "rotated-authenticated-csrf-token"
    )
    assert session_count == 2


@pytest.mark.asyncio
async def test_logout_revokes_session_rotates_csrf_and_clears_cookie(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_user(auth_session_factory)
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            [
                "pre-csrf-token",
                "authenticated-csrf-token",
                "logout-rotated-csrf-token",
            ],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"admin","password":"correct horse battery staple"}'
    )

    with TestClient(app, base_url="https://app.example") as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-logout-csrf",
            ),
        )
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-logout-login",
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        login_response = client.post(
            "/v1/auth/login",
            content=login_body,
            headers=login_headers,
        )
        auth_csrf = login_response.json()["csrf_token"]
        logout_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/logout",
            request_id="req-logout",
        )
        logout_headers.update(
            {
                "origin": "https://app.example",
                "x-csrf-token": auth_csrf,
            }
        )
        logout_response = client.post("/v1/auth/logout", headers=logout_headers)
        cookie_after_logout = client.cookies.get("perfpilot_session")

    assert logout_response.status_code == 204
    assert logout_response.content == b""
    assert logout_response.headers["cache-control"] == "no-store"
    cleared_cookie = logout_response.headers["set-cookie"]
    assert "perfpilot_session=" in cleared_cookie
    assert "Max-Age=0" in cleared_cookie
    assert "Domain=" not in cleared_cookie
    assert cookie_after_logout is None
    async with auth_session_factory() as session:
        authenticated = await session.scalar(
            select(AuthSession).where(AuthSession.kind == "authenticated")
        )
    assert authenticated is not None
    assert authenticated.revoked_at == datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    assert authenticated.csrf_secret_hash == digest_csrf_token(
        "logout-rotated-csrf-token"
    )
    with pytest.raises(InvalidSessionError):
        await service.authenticate_session(
            "authenticated-session-token",
            required_kind="authenticated",
        )


@pytest.mark.asyncio
async def test_me_returns_active_user_and_only_actual_memberships(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user = await _seed_user(auth_session_factory, is_platform_admin=True)
    team, membership = await _seed_team_membership(
        auth_session_factory,
        user_id=user.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"admin","password":"correct horse battery staple"}'
    )

    with TestClient(app, base_url="https://app.example") as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-me-csrf",
            ),
        )
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-me-login",
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        client.post("/v1/auth/login", content=login_body, headers=login_headers)
        me_response = client.get(
            "/v1/me",
            headers=_proxy_headers(
                method="GET",
                target="/v1/me",
                request_id="req-me",
            ),
        )

    assert me_response.status_code == 200
    assert me_response.headers["cache-control"] == "no-store"
    assert me_response.json() == {
        "schema_version": "1.0",
        "user": {
            "id": str(user.id),
            "username": "admin",
            "is_platform_admin": True,
        },
        "memberships": [
            {
                "id": str(membership.id),
                "team": {"id": str(team.id), "name": "Team Alpha"},
                "role": "team_owner",
            }
        ],
    }


@pytest.mark.asyncio
async def test_team_viewer_can_list_members_of_their_team(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed_user(
        auth_session_factory,
        username="owner",
        is_platform_admin=False,
    )
    viewer = await _seed_user(
        auth_session_factory,
        username="viewer",
        is_platform_admin=False,
    )
    team, owner_membership = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    viewer_membership = await _seed_membership(
        auth_session_factory,
        team_id=team.id,
        user_id=viewer.id,
        role="team_viewer",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"viewer","password":"correct horse battery staple"}'
    )
    members_path = f"/v1/teams/{team.id}/members"

    with TestClient(app, base_url="https://app.example") as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-members-csrf",
            ),
        )
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-members-login",
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        client.post("/v1/auth/login", content=login_body, headers=login_headers)
        members_response = client.get(
            members_path,
            headers=_proxy_headers(
                method="GET",
                target=members_path,
                request_id="req-members-list",
            ),
        )

    assert members_response.status_code == 200
    assert members_response.json() == {
        "schema_version": "1.0",
        "members": [
            {
                "id": str(owner_membership.id),
                "user": {"id": str(owner.id), "username": "owner"},
                "role": "team_owner",
            },
            {
                "id": str(viewer_membership.id),
                "user": {"id": str(viewer.id), "username": "viewer"},
                "role": "team_viewer",
            },
        ],
    }


@pytest.mark.parametrize("actor_role", ["team_member", "team_viewer"])
@pytest.mark.asyncio
async def test_non_owner_cannot_add_team_member(
    auth_session_factory: async_sessionmaker[AsyncSession],
    actor_role: str,
) -> None:
    owner = await _seed_user(auth_session_factory, username="owner")
    actor = await _seed_user(auth_session_factory, username="actor")
    target = await _seed_user(auth_session_factory, username="target")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    await _seed_membership(
        auth_session_factory,
        team_id=team.id,
        user_id=actor.id,
        role=actor_role,
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"actor","password":"correct horse battery staple"}'
    )
    members_path = f"/v1/teams/{team.id}/members"
    add_body = (
        f'{{"user_id":"{target.id}","role":"team_viewer"}}'.encode()
    )

    with TestClient(app, base_url="https://app.example") as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id=f"req-non-owner-{actor_role}-csrf",
            ),
        )
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id=f"req-non-owner-{actor_role}-login",
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        login_response = client.post(
            "/v1/auth/login",
            content=login_body,
            headers=login_headers,
        )
        add_headers = _proxy_headers(
            method="POST",
            target=members_path,
            body=add_body,
            request_id=f"req-non-owner-{actor_role}-add",
        )
        add_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": login_response.json()["csrf_token"],
            }
        )
        response = client.post(members_path, content=add_body, headers=add_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_forbidden"
    async with auth_session_factory() as session:
        target_membership = await session.scalar(
            select(Membership).where(
                Membership.team_id == team.id,
                Membership.user_id == target.id,
            )
        )
    assert target_membership is None


@pytest.mark.asyncio
async def test_team_owner_can_add_active_user_as_member(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed_user(auth_session_factory, username="owner")
    target = await _seed_user(auth_session_factory, username="target")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"owner","password":"correct horse battery staple"}'
    )
    members_path = f"/v1/teams/{team.id}/members"
    add_body = (
        f'{{"user_id":"{target.id}","role":"team_viewer"}}'.encode()
    )

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-owner-add-csrf",
            ),
        )
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-owner-add-login",
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        login_response = client.post(
            "/v1/auth/login",
            content=login_body,
            headers=login_headers,
        )
        add_headers = _proxy_headers(
            method="POST",
            target=members_path,
            body=add_body,
            request_id="req-owner-add",
        )
        add_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": login_response.json()["csrf_token"],
            }
        )
        response = client.post(members_path, content=add_body, headers=add_headers)

    assert response.status_code == 201
    assert response.json() == {
        "schema_version": "1.0",
        "member": {
            "id": response.json()["member"]["id"],
            "user": {"id": str(target.id), "username": "target"},
            "role": "team_viewer",
        },
    }
    async with auth_session_factory() as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.team_id == team.id,
                Membership.user_id == target.id,
            )
        )
    assert membership is not None
    assert membership.role == "team_viewer"


@pytest.mark.asyncio
async def test_owner_role_change_revokes_target_authenticated_sessions(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    owner = await _seed_user(auth_session_factory, username="owner")
    target = await _seed_user(auth_session_factory, username="target")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    target_membership = await _seed_membership(
        auth_session_factory,
        team_id=team.id,
        user_id=target.id,
        role="team_member",
    )
    await _seed_authenticated_session(
        auth_session_factory,
        user_id=owner.id,
        token="owner-session-token",
        csrf_token="owner-csrf-token",
        now=now,
    )
    target_session = await _seed_authenticated_session(
        auth_session_factory,
        user_id=target.id,
        token="target-session-token",
        csrf_token="target-csrf-token",
        now=now,
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
    )

    updated = await service.update_team_member(
        session_token="owner-session-token",
        csrf_token="owner-csrf-token",
        team_id=team.id,
        member_id=target_membership.id,
        role="team_viewer",
    )

    assert updated.id == target_membership.id
    assert updated.role == "team_viewer"
    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, target_membership.id)
        stored_target_session = await session.get(AuthSession, target_session.id)
    assert stored_membership is not None
    assert stored_membership.role == "team_viewer"
    assert stored_target_session is not None
    assert stored_target_session.revoked_at == now


@pytest.mark.asyncio
async def test_cannot_demote_the_last_team_owner(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    owner = await _seed_user(auth_session_factory, username="owner")
    team, owner_membership = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    owner_session = await _seed_authenticated_session(
        auth_session_factory,
        user_id=owner.id,
        token="owner-session-token",
        csrf_token="owner-csrf-token",
        now=now,
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
    )

    with pytest.raises(LastOwnerError):
        await service.update_team_member(
            session_token="owner-session-token",
            csrf_token="owner-csrf-token",
            team_id=team.id,
            member_id=owner_membership.id,
            role="team_member",
        )

    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, owner_membership.id)
        stored_owner_session = await session.get(AuthSession, owner_session.id)
    assert stored_membership is not None
    assert stored_membership.role == "team_owner"
    assert stored_owner_session is not None
    assert stored_owner_session.revoked_at is None


@pytest.mark.asyncio
async def test_owner_delete_member_revokes_target_authenticated_sessions(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    owner = await _seed_user(auth_session_factory, username="owner")
    target = await _seed_user(auth_session_factory, username="target")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    target_membership = await _seed_membership(
        auth_session_factory,
        team_id=team.id,
        user_id=target.id,
        role="team_member",
    )
    await _seed_authenticated_session(
        auth_session_factory,
        user_id=owner.id,
        token="owner-session-token",
        csrf_token="owner-csrf-token",
        now=now,
    )
    target_session = await _seed_authenticated_session(
        auth_session_factory,
        user_id=target.id,
        token="target-session-token",
        csrf_token="target-csrf-token",
        now=now,
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
    )

    await service.delete_team_member(
        session_token="owner-session-token",
        csrf_token="owner-csrf-token",
        team_id=team.id,
        member_id=target_membership.id,
    )

    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, target_membership.id)
        stored_target_session = await session.get(AuthSession, target_session.id)
    assert stored_membership is None
    assert stored_target_session is not None
    assert stored_target_session.revoked_at == now


@pytest.mark.asyncio
async def test_cannot_delete_the_last_team_owner(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    owner = await _seed_user(auth_session_factory, username="owner")
    team, owner_membership = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    owner_session = await _seed_authenticated_session(
        auth_session_factory,
        user_id=owner.id,
        token="owner-session-token",
        csrf_token="owner-csrf-token",
        now=now,
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: now,
    )

    with pytest.raises(LastOwnerError):
        await service.delete_team_member(
            session_token="owner-session-token",
            csrf_token="owner-csrf-token",
            team_id=team.id,
            member_id=owner_membership.id,
        )

    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, owner_membership.id)
        stored_owner_session = await session.get(AuthSession, owner_session.id)
    assert stored_membership is not None
    assert stored_membership.role == "team_owner"
    assert stored_owner_session is not None
    assert stored_owner_session.revoked_at is None


@pytest.mark.asyncio
async def test_owner_can_patch_and_delete_team_member_through_signed_api(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed_user(auth_session_factory, username="owner")
    target = await _seed_user(auth_session_factory, username="target")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    target_membership = await _seed_membership(
        auth_session_factory,
        team_id=team.id,
        user_id=target.id,
        role="team_member",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    login_body = (
        b'{"username":"owner","password":"correct horse battery staple"}'
    )
    member_path = f"/v1/teams/{team.id}/members/{target_membership.id}"
    patch_body = b'{"role":"team_viewer"}'

    with TestClient(app, base_url="https://app.example") as client:
        csrf_response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-patch-delete-csrf",
            ),
        )
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=login_body,
            request_id="req-patch-delete-login",
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
            }
        )
        login_response = client.post(
            "/v1/auth/login",
            content=login_body,
            headers=login_headers,
        )
        csrf_token = login_response.json()["csrf_token"]
        patch_headers = _proxy_headers(
            method="PATCH",
            target=member_path,
            body=patch_body,
            request_id="req-member-patch",
        )
        patch_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_token,
            }
        )
        patch_response = client.patch(
            member_path,
            content=patch_body,
            headers=patch_headers,
        )
        delete_headers = _proxy_headers(
            method="DELETE",
            target=member_path,
            request_id="req-member-delete",
        )
        delete_headers.update(
            {
                "origin": "https://app.example",
                "x-csrf-token": csrf_token,
            }
        )
        delete_response = client.delete(member_path, headers=delete_headers)

    assert patch_response.status_code == 200
    assert patch_response.json()["member"] == {
        "id": str(target_membership.id),
        "user": {"id": str(target.id), "username": "target"},
        "role": "team_viewer",
    }
    assert delete_response.status_code == 204
    assert delete_response.content == b""
    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, target_membership.id)
    assert stored_membership is None


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.asyncio
async def test_non_owner_cannot_patch_or_delete_team_member(
    auth_session_factory: async_sessionmaker[AsyncSession],
    method: str,
) -> None:
    owner = await _seed_user(auth_session_factory, username="owner")
    actor = await _seed_user(auth_session_factory, username="actor")
    target = await _seed_user(auth_session_factory, username="target")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    await _seed_membership(
        auth_session_factory,
        team_id=team.id,
        user_id=actor.id,
        role="team_member",
    )
    target_membership = await _seed_membership(
        auth_session_factory,
        team_id=team.id,
        user_id=target.id,
        role="team_viewer",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    member_path = f"/v1/teams/{team.id}/members/{target_membership.id}"
    body = b'{"role":"team_member"}' if method == "PATCH" else b""

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_token = _login_api_client(
            client,
            username="actor",
            request_prefix=f"req-non-owner-{method.casefold()}",
        )
        headers = _proxy_headers(
            method=method,
            target=member_path,
            body=body,
            request_id=f"req-non-owner-{method.casefold()}-write",
        )
        headers.update(
            {
                "origin": "https://app.example",
                "x-csrf-token": csrf_token,
            }
        )
        if body:
            headers["content-type"] = "application/json"
        response = client.request(method, member_path, content=body, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_forbidden"
    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, target_membership.id)
    assert stored_membership is not None
    assert stored_membership.role == "team_viewer"


@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
@pytest.mark.asyncio
async def test_last_owner_change_returns_stable_conflict(
    auth_session_factory: async_sessionmaker[AsyncSession],
    method: str,
) -> None:
    owner = await _seed_user(auth_session_factory, username="owner")
    team, owner_membership = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    member_path = f"/v1/teams/{team.id}/members/{owner_membership.id}"
    body = b'{"role":"team_member"}' if method == "PATCH" else b""

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_token = _login_api_client(
            client,
            username="owner",
            request_prefix=f"req-last-owner-{method.casefold()}",
        )
        headers = _proxy_headers(
            method=method,
            target=member_path,
            body=body,
            request_id=f"req-last-owner-{method.casefold()}-write",
        )
        headers.update(
            {
                "origin": "https://app.example",
                "x-csrf-token": csrf_token,
            }
        )
        if body:
            headers["content-type"] = "application/json"
        response = client.request(method, member_path, content=body, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_owner_required"
    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, owner_membership.id)
    assert stored_membership is not None
    assert stored_membership.role == "team_owner"


@pytest.mark.asyncio
async def test_cross_team_member_id_is_not_disclosed(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner_a = await _seed_user(auth_session_factory, username="owner-a")
    owner_b = await _seed_user(auth_session_factory, username="owner-b")
    target = await _seed_user(auth_session_factory, username="target")
    team_a, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner_a.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    team_b, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner_b.id,
        team_name="Team Beta",
        role="team_owner",
    )
    target_membership = await _seed_membership(
        auth_session_factory,
        team_id=team_b.id,
        user_id=target.id,
        role="team_viewer",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    member_path = f"/v1/teams/{team_a.id}/members/{target_membership.id}"
    body = b'{"role":"team_member"}'

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_token = _login_api_client(
            client,
            username="owner-a",
            request_prefix="req-cross-team",
        )
        headers = _proxy_headers(
            method="PATCH",
            target=member_path,
            body=body,
            request_id="req-cross-team-patch",
        )
        headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_token,
            }
        )
        response = client.patch(member_path, content=body, headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "member_not_found"
    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, target_membership.id)
    assert stored_membership is not None
    assert stored_membership.team_id == team_b.id
    assert stored_membership.role == "team_viewer"


@pytest.mark.asyncio
async def test_owner_cannot_add_disabled_target_user(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = await _seed_user(auth_session_factory, username="owner")
    target = await _seed_user(
        auth_session_factory,
        username="target",
        state="disabled",
    )
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    members_path = f"/v1/teams/{team.id}/members"
    body = f'{{"user_id":"{target.id}","role":"team_member"}}'.encode()

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_token = _login_api_client(
            client,
            username="owner",
            request_prefix="req-disabled-target",
        )
        headers = _proxy_headers(
            method="POST",
            target=members_path,
            body=body,
            request_id="req-disabled-target-add",
        )
        headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_token,
            }
        )
        response = client.post(members_path, content=body, headers=headers)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"
    async with auth_session_factory() as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.team_id == team.id,
                Membership.user_id == target.id,
            )
        )
    assert membership is None


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("origin", 403, "origin_not_allowed"),
        ("csrf", 403, "csrf_validation_failed"),
        ("nonmember", 404, "team_not_found"),
    ],
)
@pytest.mark.asyncio
async def test_member_patch_maps_security_and_tenant_boundary_errors(
    auth_session_factory: async_sessionmaker[AsyncSession],
    case: str,
    expected_status: int,
    expected_code: str,
) -> None:
    actor = await _seed_user(
        auth_session_factory,
        username="actor",
        is_platform_admin=case == "nonmember",
    )
    target = await _seed_user(auth_session_factory, username="target")
    team_owner = actor
    if case == "nonmember":
        team_owner = await _seed_user(auth_session_factory, username="owner")
    team, _ = await _seed_team_membership(
        auth_session_factory,
        user_id=team_owner.id,
        team_name="Team Alpha",
        role="team_owner",
    )
    target_membership = await _seed_membership(
        auth_session_factory,
        team_id=team.id,
        user_id=target.id,
        role="team_viewer",
    )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["pre-session-token", "authenticated-session-token"],
            ["pre-csrf-token", "authenticated-csrf-token"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    member_path = f"/v1/teams/{team.id}/members/{target_membership.id}"
    body = b'{"role":"team_member"}'

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        csrf_token = _login_api_client(
            client,
            username="actor",
            request_prefix=f"req-boundary-{case}",
        )
        headers = _proxy_headers(
            method="PATCH",
            target=member_path,
            body=body,
            request_id=f"req-boundary-{case}-patch",
        )
        headers.update(
            {
                "content-type": "application/json",
                "origin": (
                    "https://evil.example"
                    if case == "origin"
                    else "https://app.example"
                ),
                "x-csrf-token": (
                    "wrong-csrf-token" if case == "csrf" else csrf_token
                ),
            }
        )
        response = client.patch(member_path, content=body, headers=headers)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    async with auth_session_factory() as session:
        stored_membership = await session.get(Membership, target_membership.id)
    assert stored_membership is not None
    assert stored_membership.role == "team_viewer"


@pytest.mark.asyncio
async def test_login_without_valid_preauth_cookie_is_stable_unauthenticated(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    verifier_calls: list[tuple[str, str]] = []
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        password_verifier=lambda password_hash, password: (
            verifier_calls.append((password_hash, password)) or False
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )
    body = b'{"username":"missing","password":"secret-marker"}'
    headers = _proxy_headers(
        method="POST",
        target="/v1/auth/login",
        body=body,
        request_id="req-no-preauth",
    )
    headers.update(
        {
            "content-type": "application/json",
            "origin": "https://app.example",
            "x-csrf-token": "missing-preauth-csrf",
        }
    )

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        response = client.post("/v1/auth/login", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["cache-control"] == "no-store"
    assert verifier_calls == []


def test_cli_create_user_reads_and_pops_password_env_and_prints_only_uuid(
    auth_control_database_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from perfpilot_api import cli

    password = "cli-password-secret-marker"
    monkeypatch.setenv("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD", password)
    settings = SimpleNamespace(
        app_env="production",
        control_database_url=SimpleNamespace(
            get_secret_value=lambda: auth_control_database_url.render_as_string(
                hide_password=False
            )
        ),
    )

    exit_code = cli.main(
        [
            "create-user",
            "--username",
            "  CLIＡdmin  ",
            "--role",
            "platform_admin",
        ],
        settings_override=settings,
    )

    captured = capsys.readouterr()
    created_user_id = UUID(captured.out.strip())
    assert exit_code == 0
    assert captured.out == f"{created_user_id}\n"
    assert captured.err == ""
    assert "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD" not in os.environ
    assert "secret-marker" not in captured.out + captured.err
    with psycopg.connect(_psycopg_conninfo(auth_control_database_url)) as connection:
        row = connection.execute(
            "SELECT username, password_hash, is_platform_admin FROM users WHERE id = %s",
            (created_user_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "cliadmin"
    assert row[1] != password
    assert verify_password(row[1], password)
    assert row[2] is True


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("weak-password-user", "too-short"),
        ("LongAdminName", " ＬＯＮＧＡＤＭＩＮＮＡＭＥ "),
    ],
)
def test_cli_create_user_rejects_invalid_production_passwords_without_leaking(
    auth_control_database_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    username: str,
    password: str,
) -> None:
    from perfpilot_api import cli

    monkeypatch.setenv("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD", password)
    settings = SimpleNamespace(
        app_env="production",
        control_database_url=SimpleNamespace(
            get_secret_value=lambda: auth_control_database_url.render_as_string(
                hide_password=False
            )
        ),
    )

    exit_code = cli.main(
        ["create-user", "--username", username, "--role", "platform_admin"],
        settings_override=settings,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "invalid password" in captured.err
    assert password not in captured.err
    assert "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD" not in os.environ
    with psycopg.connect(_psycopg_conninfo(auth_control_database_url)) as connection:
        user_count = connection.execute(
            "SELECT count(*) FROM users WHERE username = %s",
            (normalize_username(username),),
        ).fetchone()
    assert user_count == (0,)


def test_cli_create_user_duplicate_and_idempotent_preserve_existing_user(
    auth_control_database_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from perfpilot_api import cli

    username = "cli-idempotent-user"
    original_hash = hash_password("original password value")
    with psycopg.connect(_psycopg_conninfo(auth_control_database_url)) as connection:
        existing = connection.execute(
            """
            INSERT INTO users (username, password_hash, is_platform_admin, state)
            VALUES (%s, %s, false, 'disabled')
            RETURNING id
            """,
            (username, original_hash),
        ).fetchone()
    assert existing is not None
    existing_id = existing[0]
    settings = SimpleNamespace(
        app_env="production",
        control_database_url=SimpleNamespace(
            get_secret_value=lambda: auth_control_database_url.render_as_string(
                hide_password=False
            )
        ),
    )

    duplicate_password = "duplicate password value"
    monkeypatch.setenv(
        "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD", duplicate_password
    )
    duplicate_exit = cli.main(
        ["create-user", "--username", username, "--role", "platform_admin"],
        settings_override=settings,
    )
    duplicate_output = capsys.readouterr()

    assert duplicate_exit == 1
    assert duplicate_output.out == ""
    assert "user already exists" in duplicate_output.err
    assert duplicate_password not in duplicate_output.err
    assert "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD" not in os.environ

    replacement_password = "replacement password value"
    monkeypatch.setenv(
        "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD", replacement_password
    )
    idempotent_exit = cli.main(
        [
            "create-user",
            "--username",
            " CLI-IDEMPOTENT-USER ",
            "--role",
            "platform_admin",
            "--idempotent",
        ],
        settings_override=settings,
    )
    idempotent_output = capsys.readouterr()

    assert idempotent_exit == 0
    assert idempotent_output.out == f"{existing_id}\n"
    assert idempotent_output.err == ""
    assert replacement_password not in idempotent_output.out
    assert "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD" not in os.environ
    with psycopg.connect(_psycopg_conninfo(auth_control_database_url)) as connection:
        row = connection.execute(
            """
            SELECT password_hash, is_platform_admin, state
            FROM users
            WHERE id = %s
            """,
            (existing_id,),
        ).fetchone()
    assert row == (original_hash, False, "disabled")


def test_cli_create_user_rejects_password_option_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from perfpilot_api import cli

    forbidden_password = "forbidden-cli-secret-value"
    monkeypatch.setenv(
        "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD", "environment password value"
    )

    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "create-user",
                "--username",
                "cli-option-user",
                "--role",
                "platform_admin",
                "--password",
                forbidden_password,
            ]
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert forbidden_password not in captured.out + captured.err


@pytest.mark.parametrize("username", ["   ", "x" * 129])
def test_cli_create_user_rejects_invalid_normalized_username(
    auth_control_database_url: URL,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    username: str,
) -> None:
    from perfpilot_api import cli

    password = "valid production password"
    monkeypatch.setenv("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD", password)
    settings = SimpleNamespace(
        app_env="production",
        control_database_url=SimpleNamespace(
            get_secret_value=lambda: auth_control_database_url.render_as_string(
                hide_password=False
            )
        ),
    )

    exit_code = cli.main(
        ["create-user", "--username", username, "--role", "platform_admin"],
        settings_override=settings,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "invalid username" in captured.err
    assert password not in captured.err
    assert "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD" not in os.environ
    with psycopg.connect(_psycopg_conninfo(auth_control_database_url)) as connection:
        user_count = connection.execute(
            "SELECT count(*) FROM users WHERE username = %s",
            (normalize_username(username),),
        ).fetchone()
    assert user_count == (0,)


def test_cli_create_user_requires_password_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from perfpilot_api import cli

    monkeypatch.delenv("PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD", raising=False)

    exit_code = cli.main(
        [
            "create-user",
            "--username",
            "missing-env-user",
            "--role",
            "platform_admin",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "bootstrap password is required" in captured.err


@pytest.mark.asyncio
async def test_cli_create_user_enforces_concurrent_uniqueness(
    auth_control_database_url: URL,
) -> None:
    from perfpilot_api import cli

    settings = SimpleNamespace(
        app_env="production",
        control_database_url=SimpleNamespace(
            get_secret_value=lambda: auth_control_database_url.render_as_string(
                hide_password=False
            )
        ),
    )
    non_idempotent_results = await asyncio.gather(
        cli._create_user(
            settings,
            "cli-concurrent-user",
            "first concurrent password",
            idempotent=False,
        ),
        cli._create_user(
            settings,
            "cli-concurrent-user",
            "second concurrent password",
            idempotent=False,
        ),
        return_exceptions=True,
    )

    created_ids = [
        result for result in non_idempotent_results if isinstance(result, UUID)
    ]
    duplicate_errors = [
        result
        for result in non_idempotent_results
        if isinstance(result, cli.UserAlreadyExistsError)
    ]
    assert len(created_ids) == 1
    assert len(duplicate_errors) == 1

    idempotent_results = await asyncio.gather(
        cli._create_user(
            settings,
            "cli-concurrent-idempotent-user",
            "first idempotent password",
            idempotent=True,
        ),
        cli._create_user(
            settings,
            "cli-concurrent-idempotent-user",
            "second idempotent password",
            idempotent=True,
        ),
    )
    assert isinstance(idempotent_results[0], UUID)
    assert idempotent_results[0] == idempotent_results[1]

    with psycopg.connect(_psycopg_conninfo(auth_control_database_url)) as connection:
        rows = connection.execute(
            """
            SELECT username, count(*)
            FROM users
            WHERE username IN (%s, %s)
            GROUP BY username
            ORDER BY username
            """,
            ("cli-concurrent-idempotent-user", "cli-concurrent-user"),
        ).fetchall()
    assert rows == [
        ("cli-concurrent-idempotent-user", 1),
        ("cli-concurrent-user", 1),
    ]


def _load_auth_contract(filename: str) -> dict[str, object]:
    path = _REPOSITORY_ROOT / "contracts" / "v1" / "auth" / filename
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        (
            "login-request.schema.json",
            {"username": "admin", "password": "correct horse battery staple"},
        ),
        (
            "session-response.schema.json",
            {"schema_version": "1.0", "csrf_token": "a" * 43},
        ),
        (
            "me-response.schema.json",
            {
                "schema_version": "1.0",
                "user": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "username": "admin",
                    "is_platform_admin": True,
                },
                "memberships": [
                    {
                        "id": "22222222-2222-4222-8222-222222222222",
                        "team": {
                            "id": "33333333-3333-4333-8333-333333333333",
                            "name": "Example Team",
                        },
                        "role": "team_owner",
                    }
                ],
            },
        ),
    ],
)
def test_auth_contract_accepts_exact_valid_shape(
    filename: str,
    payload: dict[str, object],
) -> None:
    schema = _load_auth_contract(filename)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )

    validator.validate(payload)


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        (
            "login-request.schema.json",
            {"username": "admin", "password": "password", "extra": True},
        ),
        (
            "session-response.schema.json",
            {"schema_version": "1.0", "csrf_token": "not-base64url"},
        ),
        (
            "me-response.schema.json",
            {
                "schema_version": "1.0",
                "user": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "username": "admin",
                    "is_platform_admin": True,
                    "unexpected": "field",
                },
                "memberships": [],
            },
        ),
        (
            "me-response.schema.json",
            {
                "schema_version": "1.0",
                "user": {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "username": "admin",
                    "is_platform_admin": False,
                },
                "memberships": [
                    {
                        "id": "22222222-2222-4222-8222-222222222222",
                        "team": {
                            "id": "33333333-3333-4333-8333-333333333333",
                            "name": "Example Team",
                        },
                        "role": "platform_admin",
                    }
                ],
            },
        ),
    ],
)
def test_auth_contract_rejects_invalid_or_extra_fields(
    filename: str,
    payload: dict[str, object],
) -> None:
    schema = _load_auth_contract(filename)
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )

    with pytest.raises(jsonschema.ValidationError):
        validator.validate(payload)


def test_proxy_authentication_precedes_malformed_login_body_validation() -> None:
    app = create_app(testing=True)

    with TestClient(
        app,
        base_url="https://app.example",
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/v1/auth/login",
            content=b"{",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "proxy_authentication_failed"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_proxy_body_limit_stops_chunked_body_before_route_parsing() -> None:
    app = create_app(testing=True)
    chunks = iter(
        [
            {"type": "http.request", "body": b"x" * 700_000, "more_body": True},
            {"type": "http.request", "body": b"x" * 400_000, "more_body": True},
            {"type": "http.request", "body": b"tail", "more_body": False},
        ]
    )
    receive_calls = 0
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal receive_calls
        receive_calls += 1
        return next(chunks)

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/auth/login",
            "raw_path": b"/v1/auth/login",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"app.example"),
                (b"content-type", b"application/json"),
                (b"x-request-id", b"req-chunked-too-large"),
                (b"x-perfpilot-proxy-timestamp", b"1700000000"),
                (b"x-perfpilot-proxy-signature", b"invalid"),
            ],
            "client": ("203.0.113.10", 12345),
            "server": ("app.example", 443),
        },
        receive,
        send,
    )

    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert response_start["status"] == 413
    assert json.loads(response_body)["error"]["code"] == "request_body_too_large"
    assert receive_calls == 2


@pytest.mark.asyncio
async def test_verified_proxy_client_identity_is_required_and_used_for_login_rate_key(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class RecordingRateLimiter(InMemoryLoginRateLimiter):
        def __init__(self) -> None:
            super().__init__()
            self.client_addresses: list[str] = []

        async def reserve_attempt(
            self,
            normalized_username: str,
            client_address: str,
        ) -> object:
            self.client_addresses.append(client_address)
            return await super().reserve_attempt(
                normalized_username,
                client_address,
            )

    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    await _seed_user(auth_session_factory, username="identity-admin")
    limiter = RecordingRateLimiter()
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=limiter,
        clock=lambda: now,
        token_source=StubTokenSource(
            ["identity-pre-session", "identity-auth-session"],
            ["identity-pre-csrf", "identity-auth-csrf"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
        proxy_client_identity_required=True,
    )
    client_address = "2001:db8::10"
    csrf_request_id = "req-identity-csrf"
    csrf_headers = _proxy_headers(
        method="GET",
        target="/v1/auth/csrf",
        request_id=csrf_request_id,
    )

    with TestClient(app, base_url="https://app.example") as client:
        missing_identity = client.get("/v1/auth/csrf", headers=csrf_headers)
        assert missing_identity.status_code == 401
        assert (
            missing_identity.json()["error"]["code"]
            == "proxy_authentication_failed"
        )

        csrf_identity = sign_proxy_client_identity(
            b"test-proxy-secret",
            client_address=client_address,
            timestamp=1_700_000_000,
            request_id=csrf_request_id,
        )
        csrf_headers["x-perfpilot-client-identity"] = csrf_identity
        csrf_response = client.get("/v1/auth/csrf", headers=csrf_headers)
        assert csrf_response.status_code == 200

        body = (
            b'{"username":"identity-admin",'
            b'"password":"correct horse battery staple"}'
        )
        login_request_id = "req-identity-login"
        login_headers = _proxy_headers(
            method="POST",
            target="/v1/auth/login",
            body=body,
            request_id=login_request_id,
        )
        login_headers.update(
            {
                "content-type": "application/json",
                "origin": "https://app.example",
                "x-csrf-token": csrf_response.json()["csrf_token"],
                "x-forwarded-for": "192.0.2.200, 192.0.2.201",
                "x-perfpilot-client-identity": sign_proxy_client_identity(
                    b"test-proxy-secret",
                    client_address=client_address,
                    timestamp=1_700_000_000,
                    request_id=login_request_id,
                ),
            }
        )
        login_response = client.post(
            "/v1/auth/login",
            content=body,
            headers=login_headers,
        )

    assert login_response.status_code == 200
    assert limiter.client_addresses == [csrf_identity.split(".", maxsplit=1)[0]]


def test_duplicate_proxy_client_identity_does_not_consume_replay_id() -> None:
    request_id = "req-duplicate-client-identity"
    identity = sign_proxy_client_identity(
        b"test-proxy-secret",
        client_address="198.51.100.20",
        timestamp=1_700_000_000,
        request_id=request_id,
    )
    base_headers = list(
        _proxy_headers(
            method="GET",
            target="/v1/auth/csrf",
            request_id=request_id,
        ).items()
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
        proxy_client_identity_required=True,
    )

    with TestClient(app, base_url="https://app.example") as client:
        rejected = client.get(
            "/v1/auth/csrf",
            headers=[
                *base_headers,
                ("x-perfpilot-client-identity", identity),
                ("x-perfpilot-client-identity", identity),
            ],
        )
        accepted_past_proxy = client.get(
            "/v1/auth/csrf",
            headers=[
                *base_headers,
                ("x-perfpilot-client-identity", identity),
            ],
        )

    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "proxy_authentication_failed"
    assert accepted_past_proxy.status_code == 503
    assert accepted_past_proxy.json()["error"]["code"] == "service_unavailable"


def test_non_testing_app_requires_verified_client_identity_by_default() -> None:
    app = create_app(
        testing=False,
        settings_override=_auth_test_settings(),
        auth_service=SimpleNamespace(),
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )

    with TestClient(app, base_url="https://app.example") as client:
        response = client.get(
            "/v1/auth/csrf",
            headers=_proxy_headers(
                method="GET",
                target="/v1/auth/csrf",
                request_id="req-production-identity-required",
            ),
        )

    assert app.state.proxy_client_identity_required is True
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "proxy_authentication_failed"

@pytest.mark.asyncio
async def test_login_runs_argon2_off_loop_after_releasing_pre_auth_row_lock(
    auth_session_factory: async_sessionmaker[AsyncSession],
    auth_control_database_url: URL,
) -> None:
    await _seed_user(auth_session_factory)
    verifier_thread_ids: list[int] = []
    pre_auth_lock_was_available: list[bool] = []

    def verifier(password_hash: str, password: str) -> bool:
        verifier_thread_ids.append(threading.get_ident())
        try:
            with psycopg.connect(
                _psycopg_conninfo(auth_control_database_url)
            ) as connection:
                connection.execute(
                    """
                    SELECT id
                    FROM sessions
                    WHERE token_digest = %s
                    FOR UPDATE NOWAIT
                    """,
                    (digest_session_token("off-loop-pre-session"),),
                ).fetchone()
        except psycopg.errors.LockNotAvailable:
            pre_auth_lock_was_available.append(False)
        else:
            pre_auth_lock_was_available.append(True)
        return verify_password(password_hash, password)

    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["off-loop-pre-session", "off-loop-auth-session"],
            ["off-loop-pre-csrf", "off-loop-auth-csrf"],
        ),
        password_verifier=verifier,
        password_verify_concurrency=1,
    )
    pre_auth = await service.create_pre_auth_session()

    authenticated = await service.login(
        pre_auth_token=pre_auth.token,
        csrf_token=pre_auth.csrf_token,
        username="admin",
        password="correct horse battery staple",
        client_address="client-id-off-loop",
    )

    assert authenticated.kind == "authenticated"
    assert verifier_thread_ids and verifier_thread_ids[0] != threading.get_ident()
    assert pre_auth_lock_was_available == [True]


@pytest.mark.asyncio
async def test_concurrent_invalid_logins_reserve_rate_capacity_before_argon2(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    verifier_calls = 0
    active_verifiers = 0
    maximum_active_verifiers = 0
    verifier_lock = threading.Lock()
    verifier_delay = threading.Event()

    def always_reject(password_hash: str, password: str) -> bool:
        nonlocal active_verifiers, maximum_active_verifiers, verifier_calls
        del password_hash, password
        with verifier_lock:
            verifier_calls += 1
            active_verifiers += 1
            maximum_active_verifiers = max(
                maximum_active_verifiers,
                active_verifiers,
            )
        verifier_delay.wait(0.03)
        with verifier_lock:
            active_verifiers -= 1
        return False

    session_tokens = [f"rate-pre-session-{index}" for index in range(10)]
    csrf_tokens = [f"rate-pre-csrf-{index}" for index in range(10)]
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(failure_limit=5),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(session_tokens, csrf_tokens),
        password_verifier=always_reject,
        password_verify_concurrency=2,
    )
    pre_auth_sessions = [
        await service.create_pre_auth_session() for _ in range(10)
    ]

    results = await asyncio.gather(
        *(
            service.login(
                pre_auth_token=pre_auth.token,
                csrf_token=pre_auth.csrf_token,
                username="unknown-user",
                password="wrong password",
                client_address="stable-client-id",
            )
            for pre_auth in pre_auth_sessions
        ),
        return_exceptions=True,
    )

    assert verifier_calls == 5
    assert maximum_active_verifiers == 2
    assert all(
        isinstance(result, InvalidCredentialsError | LoginRateLimitedError)
        for result in results
    )
    assert sum(isinstance(result, LoginRateLimitedError) for result in results) >= 1


@pytest.mark.asyncio
async def test_cookie_less_csrf_issuance_is_source_limited_before_database_write(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from perfpilot_api.services.auth import InMemoryPreAuthSessionLimiter

    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        pre_auth_session_limiter=InMemoryPreAuthSessionLimiter(
            issuance_limit=2
        ),
        clock=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=UTC),
        token_source=StubTokenSource(
            ["limited-pre-session-1", "limited-pre-session-2"],
            ["limited-pre-csrf-1", "limited-pre-csrf-2"],
        ),
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        auth_service=service,
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
        client_address_resolver=lambda _: "stable-source-id",
    )

    responses = []
    with TestClient(app, base_url="https://app.example") as client:
        for index in range(3):
            client.cookies.clear()
            responses.append(
                client.get(
                    "/v1/auth/csrf",
                    headers=_proxy_headers(
                        method="GET",
                        target="/v1/auth/csrf",
                        request_id=f"req-csrf-source-limit-{index}",
                    ),
                )
            )

    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1].json()["error"]["code"] == "csrf_rate_limited"
    async with auth_session_factory() as session:
        stored_count = len((await session.scalars(select(AuthSession))).all())
    assert stored_count == 2


@pytest.mark.asyncio
async def test_pre_auth_creation_cleans_bounded_stale_session_rows(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from perfpilot_api.services.auth import InMemoryPreAuthSessionLimiter

    now = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    expired_digest = digest_session_token("expired-session")
    revoked_digest = digest_session_token("old-revoked-session")
    fresh_digest = digest_session_token("fresh-session")
    async with auth_session_factory() as session, session.begin():
        session.add_all(
            [
                AuthSession(
                    user_id=None,
                    token_digest=expired_digest,
                    kind="pre_auth",
                    csrf_secret_hash=digest_csrf_token("expired-csrf"),
                    last_seen_at=now - timedelta(minutes=11),
                    absolute_expires_at=now - timedelta(minutes=1),
                    revoked_at=None,
                ),
                AuthSession(
                    user_id=None,
                    token_digest=revoked_digest,
                    kind="pre_auth",
                    csrf_secret_hash=digest_csrf_token("revoked-csrf"),
                    last_seen_at=now - timedelta(days=2),
                    absolute_expires_at=now + timedelta(days=1),
                    revoked_at=now - timedelta(days=2),
                ),
                AuthSession(
                    user_id=None,
                    token_digest=fresh_digest,
                    kind="pre_auth",
                    csrf_secret_hash=digest_csrf_token("fresh-csrf"),
                    last_seen_at=now,
                    absolute_expires_at=now + timedelta(minutes=5),
                    revoked_at=None,
                ),
            ]
        )
    service = AuthService(
        session_factory=auth_session_factory,
        rate_limiter=InMemoryLoginRateLimiter(),
        pre_auth_session_limiter=InMemoryPreAuthSessionLimiter(),
        clock=lambda: now,
        token_source=StubTokenSource(
            ["new-cleanup-session"],
            ["new-cleanup-csrf"],
        ),
    )

    await service.get_or_create_csrf(
        None,
        client_address="cleanup-source-id",
    )

    async with auth_session_factory() as session:
        remaining_digests = set(await session.scalars(select(AuthSession.token_digest)))
    assert expired_digest not in remaining_digests
    assert revoked_digest not in remaining_digests
    assert fresh_digest in remaining_digests
    assert digest_session_token("new-cleanup-session") in remaining_digests


async def _invoke_raw_asgi_request(
    app: object,
    *,
    path: str,
    raw_path: object,
    headers: list[tuple[bytes, bytes]],
    messages: list[dict[str, object]] | None = None,
    query_string: object = b"",
) -> tuple[int, dict[str, object], int, list[dict[str, object]]]:
    pending_messages = iter(
        messages
        or [{"type": "http.request", "body": b"", "more_body": False}]
    )
    receive_calls = 0
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal receive_calls
        receive_calls += 1
        return next(pending_messages, {"type": "http.disconnect"})

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": raw_path,
            "query_string": query_string,
            "root_path": "",
            "headers": headers,
            "client": ("203.0.113.10", 12345),
            "server": ("app.example", 443),
        },
        receive,
        send,
    )
    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return (
        int(response_start["status"]),
        json.loads(response_body),
        receive_calls,
        sent,
    )


def _raw_proxy_headers(
    *,
    request_id: str,
    signature: str,
    content_length: bytes | None = None,
) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"host", b"app.example"),
        (b"x-request-id", request_id.encode("ascii")),
        (b"x-perfpilot-proxy-timestamp", b"1700000000"),
        (b"x-perfpilot-proxy-signature", signature.encode("ascii")),
    ]
    if content_length is not None:
        headers.append((b"content-length", content_length))
    return headers


@pytest.mark.asyncio
async def test_proxy_rejects_missing_required_identity_before_reading_body() -> None:
    status, payload, receive_calls, _ = await _invoke_raw_asgi_request(
        create_app(
            testing=True,
            settings_override=_auth_test_settings(),
            proxy_clock=lambda: 1_700_000_000.0,
            proxy_client_identity_required=True,
        ),
        path="/v1/auth/login",
        raw_path=b"/v1/auth/login",
        headers=_raw_proxy_headers(
            request_id="req-missing-identity-before-body",
            signature="a" * 43,
        ),
        messages=[
            {
                "type": "http.request",
                "body": b'{"username":"admin","password":"secret"}',
                "more_body": False,
            }
        ],
    )

    assert status == 401
    assert payload["error"]["code"] == "proxy_authentication_failed"
    assert receive_calls == 0


@pytest.mark.parametrize(
    "content_length",
    [b"", b"+1", b"-1", b" 1", b"1 ", b"1_0", b"1,1", b"\xff"],
)
@pytest.mark.asyncio
async def test_proxy_rejects_non_digit_content_length_without_reading_body(
    content_length: bytes,
) -> None:
    status, payload, receive_calls, _ = await _invoke_raw_asgi_request(
        create_app(testing=True),
        path="/v1/auth/csrf",
        raw_path=b"/v1/auth/csrf",
        headers=_raw_proxy_headers(
            request_id="req-invalid-content-length",
            signature="a" * 43,
            content_length=content_length,
        ),
    )

    assert status == 401
    assert payload["error"]["code"] == "proxy_authentication_failed"
    assert receive_calls == 0


@pytest.mark.parametrize("content_length", [b"1048577", b"9" * 5000])
@pytest.mark.asyncio
async def test_proxy_rejects_oversized_declared_length_without_integer_limit_or_body_read(
    content_length: bytes,
) -> None:
    status, payload, receive_calls, _ = await _invoke_raw_asgi_request(
        create_app(testing=True),
        path="/v1/auth/csrf",
        raw_path=b"/v1/auth/csrf",
        headers=_raw_proxy_headers(
            request_id="req-huge-content-length",
            signature="a" * 43,
            content_length=content_length,
        ),
    )

    assert status == 413
    assert payload["error"]["code"] == "request_body_too_large"
    assert receive_calls == 0


@pytest.mark.parametrize(
    ("body", "content_length"),
    [(b"x", b"000001"), (b"x" * (1024 * 1024), b"1048576")],
)
@pytest.mark.asyncio
async def test_proxy_accepts_leading_zero_or_exact_limit_content_length(
    body: bytes,
    content_length: bytes,
) -> None:
    request_id = f"req-valid-content-length-{len(body)}"
    signature = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=1_700_000_000,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/auth/csrf",
        body=body,
    )
    status, payload, receive_calls, _ = await _invoke_raw_asgi_request(
        create_app(
            testing=True,
            settings_override=_auth_test_settings(),
            proxy_clock=lambda: 1_700_000_000.0,
        ),
        path="/v1/auth/csrf",
        raw_path=b"/v1/auth/csrf",
        headers=_raw_proxy_headers(
            request_id=request_id,
            signature=signature,
            content_length=content_length,
        ),
        messages=[
            {"type": "http.request", "body": body, "more_body": False}
        ],
    )

    assert status == 503
    assert payload["error"]["code"] == "service_unavailable"
    assert receive_calls == 1


@pytest.mark.parametrize("declared_length", [b"0", b"2"])
@pytest.mark.asyncio
async def test_proxy_declared_length_mismatch_does_not_reserve_replay_id(
    declared_length: bytes,
) -> None:
    class RecordingReplayStore:
        def __init__(self) -> None:
            self.request_ids: list[str] = []

        async def reserve(self, request_id: str, *, ttl_seconds: int) -> bool:
            del ttl_seconds
            self.request_ids.append(request_id)
            return True

    body = b"x"
    request_id = f"req-length-mismatch-{declared_length.decode()}"
    signature = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=1_700_000_000,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/auth/csrf",
        body=body,
    )
    replay_store = RecordingReplayStore()
    status, payload, _, _ = await _invoke_raw_asgi_request(
        create_app(
            testing=True,
            replay_store=replay_store,
            proxy_clock=lambda: 1_700_000_000.0,
        ),
        path="/v1/auth/csrf",
        raw_path=b"/v1/auth/csrf",
        headers=_raw_proxy_headers(
            request_id=request_id,
            signature=signature,
            content_length=declared_length,
        ),
        messages=[
            {"type": "http.request", "body": body, "more_body": False}
        ],
    )

    assert status == 401
    assert payload["error"]["code"] == "proxy_authentication_failed"
    assert replay_store.request_ids == []


@pytest.mark.parametrize("raw_path", [None, "/v1/auth/csrf", bytearray(b"/v1/auth/csrf")])
@pytest.mark.asyncio
async def test_proxy_rejects_non_bytes_raw_path_with_stable_error(
    raw_path: object,
) -> None:
    request_id = "req-invalid-raw-path"
    signature = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=1_700_000_000,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/auth/csrf",
    )
    status, payload, _, _ = await _invoke_raw_asgi_request(
        create_app(
            testing=True,
            settings_override=_auth_test_settings(),
            proxy_clock=lambda: 1_700_000_000.0,
        ),
        path="/v1/auth/csrf",
        raw_path=raw_path,
        headers=_raw_proxy_headers(
            request_id=request_id,
            signature=signature,
        ),
    )

    assert status == 401
    assert payload["error"]["code"] == "proxy_authentication_failed"


@pytest.mark.parametrize("query_string", [None, "a=1", bytearray(b"a=1")])
@pytest.mark.asyncio
async def test_proxy_rejects_non_bytes_raw_query_with_stable_error(
    query_string: object,
) -> None:
    request_id = "req-invalid-raw-query"
    signature = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=1_700_000_000,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/auth/csrf",
    )
    status, payload, _, _ = await _invoke_raw_asgi_request(
        create_app(
            testing=True,
            settings_override=_auth_test_settings(),
            proxy_clock=lambda: 1_700_000_000.0,
        ),
        path="/v1/auth/csrf",
        raw_path=b"/v1/auth/csrf",
        query_string=query_string,
        headers=_raw_proxy_headers(
            request_id=request_id,
            signature=signature,
        ),
    )

    assert status == 401
    assert payload["error"]["code"] == "proxy_authentication_failed"


@pytest.mark.asyncio
async def test_proxy_signature_uses_exact_percent_encoded_raw_path() -> None:
    request_id = "req-percent-raw-path"
    invalid_signature = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=1_700_000_000,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/auth/csrf",
    )
    valid_signature = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=1_700_000_000,
        request_id=request_id,
        method="GET",
        raw_path=b"/v1/auth/%63srf",
    )
    app = create_app(
        testing=True,
        settings_override=_auth_test_settings(),
        replay_store=InMemoryReplayStore(clock=lambda: 1_700_000_000.0),
        proxy_clock=lambda: 1_700_000_000.0,
    )

    rejected_status, rejected_payload, _, _ = await _invoke_raw_asgi_request(
        app,
        path="/v1/auth/csrf",
        raw_path=b"/v1/auth/%63srf",
        headers=_raw_proxy_headers(
            request_id=request_id,
            signature=invalid_signature,
        ),
    )
    accepted_status, accepted_payload, _, _ = await _invoke_raw_asgi_request(
        app,
        path="/v1/auth/csrf",
        raw_path=b"/v1/auth/%63srf",
        headers=_raw_proxy_headers(
            request_id=request_id,
            signature=valid_signature,
        ),
    )

    assert rejected_status == 401
    assert rejected_payload["error"]["code"] == "proxy_authentication_failed"
    assert accepted_status == 503
    assert accepted_payload["error"]["code"] == "service_unavailable"


@pytest.mark.asyncio
async def test_proxy_replayed_body_then_delegates_to_original_disconnect() -> None:
    from perfpilot_api.api.auth import ProxyAuthenticationMiddleware

    downstream_messages: list[dict[str, object]] = []
    sent: list[dict[str, object]] = []

    async def downstream(
        scope: dict[str, object],
        receive: object,
        send: object,
    ) -> None:
        del scope
        downstream_messages.append(await receive())
        downstream_messages.append(await receive())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    body = b"part-1part-2"
    request_id = "req-receive-delegation"
    signature = sign_proxy_request(
        b"test-proxy-secret",
        timestamp=1_700_000_000,
        request_id=request_id,
        method="POST",
        raw_path=b"/v1/auth/login",
        body=body,
    )
    state_app = SimpleNamespace(
        state=SimpleNamespace(
            settings=_auth_test_settings(),
            proxy_replay_store=InMemoryReplayStore(
                clock=lambda: 1_700_000_000.0
            ),
            proxy_clock=lambda: 1_700_000_000.0,
            proxy_client_identity_required=False,
        )
    )
    source_messages = iter(
        [
            {"type": "http.request", "body": b"part-1", "more_body": True},
            {"type": "http.request", "body": b"part-2", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def source_receive() -> dict[str, object]:
        return next(source_messages)

    async def capture_send(message: dict[str, object]) -> None:
        sent.append(message)

    await ProxyAuthenticationMiddleware(downstream)(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/auth/login",
            "raw_path": b"/v1/auth/login",
            "query_string": b"",
            "headers": _raw_proxy_headers(
                request_id=request_id,
                signature=signature,
            ),
            "app": state_app,
            "state": {"request_id": request_id},
        },
        source_receive,
        capture_send,
    )

    assert downstream_messages == [
        {"type": "http.request", "body": body, "more_body": False},
        {"type": "http.disconnect"},
    ]
    assert sent[0]["status"] == 204


def test_admin_namespace_is_proxy_protected_before_route_resolution() -> None:
    with TestClient(create_app(testing=True), base_url="https://app.example") as client:
        response = client.get("/v1/admin")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "proxy_authentication_failed"
