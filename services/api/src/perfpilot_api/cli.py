import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from perfpilot_api.config import get_settings
from perfpilot_api.db.control.models import User
from perfpilot_api.db.control.session import (
    create_control_engine,
    create_control_session_factory,
)
from perfpilot_api.security.passwords import hash_password, normalize_username

_BOOTSTRAP_PASSWORD_ENV = "PERFPILOT_BOOTSTRAP_ADMIN_PASSWORD"


class _SecretValue(Protocol):
    def get_secret_value(self) -> str: ...


class _CliSettings(Protocol):
    app_env: str
    control_database_url: _SecretValue


class UserAlreadyExistsError(Exception):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(prog="perfpilot-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    create_user = commands.add_parser("create-user")
    create_user.add_argument("--username", required=True)
    create_user.add_argument("--role", choices=("platform_admin",), required=True)
    create_user.add_argument("--idempotent", action="store_true")
    return parser


async def _insert_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    password: str,
    idempotent: bool,
) -> UUID:
    async with session_factory() as session:
        existing_user_id = await session.scalar(
            select(User.id).where(User.username == username)
        )
        if existing_user_id is not None:
            if idempotent:
                return existing_user_id
            raise UserAlreadyExistsError

        user = User(
            username=username,
            password_hash=hash_password(password),
            is_platform_admin=True,
            state="active",
        )
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing_user_id = await session.scalar(
                select(User.id).where(User.username == username)
            )
            if existing_user_id is None:
                raise
            if idempotent:
                return existing_user_id
            raise UserAlreadyExistsError from None
        return user.id


async def _create_user(
    settings: _CliSettings,
    username: str,
    password: str,
    *,
    idempotent: bool,
) -> UUID:
    engine = create_control_engine(settings.control_database_url.get_secret_value())
    try:
        return await _insert_user(
            create_control_session_factory(engine),
            username=username,
            password=password,
            idempotent=idempotent,
        )
    finally:
        await engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    settings_override: _CliSettings | None = None,
) -> int:
    args = _parser().parse_args(argv)
    password = os.environ.pop(_BOOTSTRAP_PASSWORD_ENV, None)
    if password is None:
        print("bootstrap password is required", file=sys.stderr)
        return 1

    settings = settings_override or get_settings()
    normalized_username = normalize_username(args.username)
    if not normalized_username or len(normalized_username) > 128:
        print("invalid username", file=sys.stderr)
        return 1
    if settings.app_env == "production" and (
        len(password) < 12
        or normalize_username(password) == normalized_username
    ):
        print("invalid password", file=sys.stderr)
        return 1
    try:
        user_id = asyncio.run(
            _create_user(
                settings,
                normalized_username,
                password,
                idempotent=args.idempotent,
            )
        )
    except UserAlreadyExistsError:
        print("user already exists", file=sys.stderr)
        return 1
    print(user_id)
    return 0
