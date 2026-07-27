from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_tenant_engine(database_url: str, *, pool_pre_ping: bool = True) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=pool_pre_ping)


def create_tenant_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
