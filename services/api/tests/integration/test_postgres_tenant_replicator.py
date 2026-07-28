from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import URL, make_url

from perfpilot_api.services import provisioning

_TENANT_ADMIN_URL_ENV = "PERFPILOT_TEST_TENANT_ADMIN_URL"


def _conninfo(
    admin_url: URL,
    *,
    database: str,
    username: str | None = None,
    password: str | None = None,
) -> str:
    return URL.create(
        "postgresql",
        username=username or admin_url.username,
        password=password if username is not None else admin_url.password,
        host=admin_url.host,
        port=admin_url.port,
        database=database,
        query=dict(admin_url.query),
    ).render_as_string(hide_password=False)


@pytest.fixture
async def tenant_replication_databases() -> AsyncIterator[dict[str, object]]:
    raw_admin_url = os.getenv(_TENANT_ADMIN_URL_ENV)
    if raw_admin_url is None:
        pytest.skip(f"set {_TENANT_ADMIN_URL_ENV} to run tenant replication integration")
    admin_url = make_url(raw_admin_url)
    if admin_url.host is None:
        pytest.skip("tenant replication integration requires an explicit cluster host")

    suffix = uuid4().hex[:16]
    source_database = f"pp_test_src_{suffix}"
    target_database = f"pp_test_dst_{suffix}"
    source_role = f"pp_test_sm_{suffix}"
    target_role = f"pp_test_tm_{suffix}"
    source_password = f"source-{uuid4().hex}"
    target_password = f"target-{uuid4().hex}"
    admin_conninfo = _conninfo(admin_url, database=admin_url.database or "postgres")

    async with await psycopg.AsyncConnection.connect(admin_conninfo, autocommit=True) as connection:
        for role, password in (
            (source_role, source_password),
            (target_role, target_password),
        ):
            await connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        for database, role in (
            (source_database, source_role),
            (target_database, target_role),
        ):
            await connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
            )
            await connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )

    try:
        for database, role in (
            (source_database, source_role),
            (target_database, target_role),
        ):
            async with await psycopg.AsyncConnection.connect(
                _conninfo(admin_url, database=database), autocommit=True
            ) as connection:
                await connection.execute(
                    sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(sql.Identifier(role))
                )
            async with await psycopg.AsyncConnection.connect(
                _conninfo(
                    admin_url,
                    database=database,
                    username=role,
                    password=source_password if role == source_role else target_password,
                ),
                autocommit=True,
            ) as connection:
                await connection.execute(
                    "CREATE TABLE public.parents (id bigserial PRIMARY KEY, name text NOT NULL)"
                )
                await connection.execute(
                    "CREATE TABLE public.children ("
                    "id bigserial PRIMARY KEY, "
                    "parent_id bigint NOT NULL REFERENCES public.parents(id), "
                    "payload bytea NOT NULL)"
                )
                await connection.execute(
                    "CREATE TABLE public.empty_items (id bigint PRIMARY KEY, note text)"
                )

        async with await psycopg.AsyncConnection.connect(
            _conninfo(
                admin_url,
                database=source_database,
                username=source_role,
                password=source_password,
            ),
            autocommit=True,
        ) as connection:
            await connection.execute(
                "INSERT INTO public.parents (name) VALUES ('first'), ('second')"
            )
            await connection.execute(
                "INSERT INTO public.children (parent_id, payload) "
                "VALUES (1, decode('00ff', 'hex')), (2, decode('abcd', 'hex'))"
            )

        async with await psycopg.AsyncConnection.connect(
            admin_conninfo, autocommit=True
        ) as connection:
            await connection.execute(
                sql.SQL("ALTER DATABASE {} SET default_transaction_read_only = on").format(
                    sql.Identifier(source_database)
                )
            )

        yield {
            "admin_url": admin_url,
            "source_database": source_database,
            "target_database": target_database,
            "source_role": source_role,
            "target_role": target_role,
            "source_password": source_password,
            "target_password": target_password,
        }
    finally:
        async with await psycopg.AsyncConnection.connect(
            admin_conninfo, autocommit=True
        ) as connection:
            for database in (source_database, target_database):
                await connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )
            for role in (source_role, target_role):
                await connection.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                )


@pytest.mark.asyncio
async def test_real_postgres_replicator_copies_fk_empty_tables_and_sequence_state(
    tenant_replication_databases: dict[str, object],
) -> None:
    setup = tenant_replication_databases
    admin_url = setup["admin_url"]
    assert isinstance(admin_url, URL)
    replicator_type = getattr(provisioning, "PsycopgTenantReplicator")
    replicator = replicator_type(
        cluster_host=admin_url.host,
        cluster_port=admin_url.port or 5432,
        sslmode=str(admin_url.query.get("sslmode", "disable")),
    )

    await replicator.copy_and_validate(
        source_database_name=setup["source_database"],
        source_migration_role_name=setup["source_role"],
        source_password=str(setup["source_password"]).encode(),
        target_database_name=setup["target_database"],
        target_migration_role_name=setup["target_role"],
        target_password=str(setup["target_password"]).encode(),
    )

    target_conninfo = _conninfo(
        admin_url,
        database=str(setup["target_database"]),
        username=str(setup["target_role"]),
        password=str(setup["target_password"]),
    )
    async with await psycopg.AsyncConnection.connect(target_conninfo) as connection:
        parents = await (
            await connection.execute("SELECT id, name FROM public.parents ORDER BY id")
        ).fetchall()
        children = await (
            await connection.execute(
                "SELECT id, parent_id, encode(payload, 'hex') FROM public.children ORDER BY id"
            )
        ).fetchall()
        empty_count = await (
            await connection.execute("SELECT count(*) FROM public.empty_items")
        ).fetchone()
        parent_sequence = await (
            await connection.execute("SELECT last_value, is_called FROM public.parents_id_seq")
        ).fetchone()
        child_sequence = await (
            await connection.execute("SELECT last_value, is_called FROM public.children_id_seq")
        ).fetchone()

    assert parents == [(1, "first"), (2, "second")]
    assert children == [(1, 1, "00ff"), (2, 2, "abcd")]
    assert empty_count == (0,)
    assert parent_sequence == (2, True)
    assert child_sequence == (2, True)


@pytest.mark.asyncio
async def test_real_postgres_replicator_rejects_tampered_copy_without_leaking_passwords(
    tenant_replication_databases: dict[str, object],
) -> None:
    setup = tenant_replication_databases
    admin_url = setup["admin_url"]
    assert isinstance(admin_url, URL)
    target_conninfo = _conninfo(
        admin_url,
        database=str(setup["target_database"]),
        username=str(setup["target_role"]),
        password=str(setup["target_password"]),
    )
    async with await psycopg.AsyncConnection.connect(
        target_conninfo, autocommit=True
    ) as connection:
        await connection.execute(
            "CREATE FUNCTION public.tamper_child_copy() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "NEW.payload = decode('deadbeef', 'hex'); RETURN NEW; END $$"
        )
        await connection.execute(
            "CREATE TRIGGER tamper_child_copy BEFORE INSERT ON public.children "
            "FOR EACH ROW EXECUTE FUNCTION public.tamper_child_copy()"
        )

    replicator_type = getattr(provisioning, "PsycopgTenantReplicator")
    replicator = replicator_type(
        cluster_host=admin_url.host,
        cluster_port=admin_url.port or 5432,
        sslmode=str(admin_url.query.get("sslmode", "disable")),
    )
    with pytest.raises(provisioning.ProvisioningInterrupted) as exc_info:
        await replicator.copy_and_validate(
            source_database_name=setup["source_database"],
            source_migration_role_name=setup["source_role"],
            source_password=str(setup["source_password"]).encode(),
            target_database_name=setup["target_database"],
            target_migration_role_name=setup["target_role"],
            target_password=str(setup["target_password"]).encode(),
        )

    rendered = f"{exc_info.value!s} {exc_info.value!r}"
    assert str(setup["source_password"]) not in rendered
    assert str(setup["target_password"]) not in rendered
    assert exc_info.value.__cause__ is None
    async with await psycopg.AsyncConnection.connect(target_conninfo) as connection:
        row = await (await connection.execute("SELECT count(*) FROM public.children")).fetchone()
    assert row == (0,)
