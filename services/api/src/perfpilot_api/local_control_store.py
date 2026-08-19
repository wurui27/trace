"""Private, atomic persistence for local-runtime identities and sessions.

The supplied root is intentionally a dedicated control-state directory.  It is
not shared with local analysis artifacts, which remain owned by
``LocalAnalysisStore``.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from perfpilot_api.security.passwords import (
    hash_password,
    normalize_username,
    verify_password,
)


LOCAL_SESSION_TTL = timedelta(hours=8)
_SCHEMA_VERSION = 2
_CONTROL_FILE_NAME = "control.json"
_LOCK_FILE_NAME = ".control.lock"
_MAX_CONTROL_BYTES = 2 * 1024 * 1024
_MAX_PREAUTH_SESSIONS = 64
_MAX_AUTHENTICATED_SESSIONS = 256
_MAX_AUTHENTICATED_SESSIONS_PER_USER = 8
_CSRF_CONTEXT = b"perfpilot-local-csrf-v1"
_DUMMY_PASSWORD_HASH = hash_password("perfpilot-local-control-dummy-password")


class LocalControlStoreError(RuntimeError):
    """A redacted local-control persistence or credential error."""


class LocalControlStoreNotFoundError(LocalControlStoreError):
    """A deliberately redacted principal lookup failure."""


@dataclass(frozen=True, slots=True)
class LocalPrincipal:
    user_id: UUID
    username: str
    team_id: UUID
    team_name: str
    is_platform_admin: bool
    must_change_password: bool


@dataclass(frozen=True, slots=True)
class EnsureUserResult:
    principal: LocalPrincipal
    created: bool


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_token() -> str:
    return secrets.token_urlsafe(32)


def _reject_constant(_value: str) -> object:
    raise ValueError


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


class LocalControlStore:
    """Filesystem-backed local users, one private team each, and sessions."""

    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
        uuid_factory: Callable[[], UUID] = uuid4,
        token_factory: Callable[[], str] = _default_token,
    ) -> None:
        if not isinstance(state_root, Path):
            raise TypeError("state_root must be a Path")
        if state_root.is_symlink():
            raise LocalControlStoreError("unsafe local control path")
        try:
            state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise LocalControlStoreError("local control persistence failed") from None
        self._root = state_root.absolute()
        try:
            self._root_fd = os.open(
                self._root,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            root_status = os.fstat(self._root_fd)
            self._root_identity = (root_status.st_dev, root_status.st_ino)
            self._active_root_fd = -1
            self._verify_trusted_root()
            os.fchmod(self._root_fd, 0o700)
            self._verify_trusted_root()
            if self._entry_is_symlink(_CONTROL_FILE_NAME) or self._entry_is_symlink(
                _LOCK_FILE_NAME
            ):
                raise LocalControlStoreError("unsafe local control path")
            self._secure_existing_control_file()
        except LocalControlStoreError:
            self.close()
            raise
        except OSError:
            self.close()
            raise LocalControlStoreError("unsafe local control path") from None
        self._clock = clock
        self._uuid_factory = uuid_factory
        self._token_factory = token_factory

    def close(self) -> None:
        descriptor = getattr(self, "_root_fd", -1)
        if descriptor >= 0:
            self._root_fd = -1
            os.close(descriptor)

    def __del__(self) -> None:
        try:
            self.close()
        except OSError:
            pass

    def ensure_user(self, username: str, password: str, admin: bool) -> EnsureUserResult:
        normalized = self._validate_new_credentials(username, password)
        if not isinstance(admin, bool):
            raise LocalControlStoreError("invalid local credentials")
        result: EnsureUserResult | None = None
        with self._exclusive_lock():
            document = self._read_document()
            pruned = self._prune_sessions(document, self._now())
            existing = self._find_user(document, normalized)
            if existing is not None:
                result = EnsureUserResult(self._principal_from_user(document, existing), False)
                if pruned:
                    self._write_document(document)
            else:
                user_id = str(self._new_uuid())
                team_id = str(self._new_uuid())
                team = {
                    "team_id": team_id,
                    "name": f"{normalized} local team",
                }
                user = {
                    "user_id": user_id,
                    "username": normalized,
                    "team_id": team["team_id"],
                    "password_hash": self._password_hash(password),
                    "is_platform_admin": admin,
                    "must_change_password": True,
                }
                document["teams"].append(team)
                document["users"].append(user)
                self._write_document(document)
                result = EnsureUserResult(self._principal_from_user(document, user), True)
        assert result is not None
        return result

    def find_user(self, username: str) -> LocalPrincipal | None:
        """Return an existing local principal without changing credentials or role."""
        if not isinstance(username, str):
            return None
        normalized = normalize_username(username)
        if not normalized or len(normalized) > 128:
            return None
        with self._exclusive_lock():
            document = self._read_document()
            user = self._find_user(document, normalized)
            return None if user is None else self._principal_from_user(document, user)

    def unique_platform_admin(self) -> LocalPrincipal | None:
        """Return the sole platform administrator used by explicit auto-enrollment."""
        with self._exclusive_lock():
            document = self._read_document()
            admins = [
                user for user in document["users"] if user["is_platform_admin"] is True
            ]
            if len(admins) != 1:
                return None
            return self._principal_from_user(document, admins[0])

    def authenticate(self, username: str, password: str) -> LocalPrincipal | None:
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        normalized = normalize_username(username)
        if not normalized or len(normalized) > 128:
            verify_password(_DUMMY_PASSWORD_HASH, password)
            return None
        with self._exclusive_lock():
            document = self._read_document()
            user = self._find_user(document, normalized)
            password_hash = str(user["password_hash"]) if user is not None else _DUMMY_PASSWORD_HASH
            verified = verify_password(password_hash, password)
            if user is None or not verified:
                return None
            return self._principal_from_user(document, user)

    def issue_session(self, user_id: UUID) -> tuple[str, str]:
        if not isinstance(user_id, UUID):
            raise TypeError("user_id must be a UUID")
        with self._exclusive_lock():
            document = self._read_document()
            if self._find_user_by_id(document, user_id) is None:
                raise LocalControlStoreNotFoundError("local principal not found")
            now = self._now()
            self._prune_sessions(document, now)
            token = self._new_token()
            csrf_token = self._csrf_token(token)
            self._append_session(document, token, csrf_token, str(user_id), "authenticated", now)
            self._prune_sessions(document, now)
            self._write_document(document)
            return token, csrf_token

    def issue_preauth_session(self) -> tuple[str, str]:
        """Issue a short-lived anonymous session used only to bind login CSRF."""
        with self._exclusive_lock():
            document = self._read_document()
            now = self._now()
            self._prune_sessions(document, now)
            token = self._new_token()
            csrf_token = self._csrf_token(token)
            self._append_session(document, token, csrf_token, None, "preauth", now)
            self._prune_sessions(document, now)
            self._write_document(document)
            return token, csrf_token

    def csrf_for_session(self, token: str) -> str | None:
        """Return an idempotent CSRF token without rotating the session cookie."""
        if not isinstance(token, str) or not token:
            return None
        digest = self._digest(token)
        with self._exclusive_lock():
            document = self._read_document()
            now = self._now()
            changed = self._prune_sessions(document, now)
            for session in document["sessions"]:
                if not hmac.compare_digest(str(session["token_digest"]), digest):
                    continue
                expected = self._csrf_token(token)
                if not hmac.compare_digest(str(session["csrf_token_digest"]), self._digest(expected)):
                    session["csrf_token_digest"] = self._digest(expected)
                    changed = True
                if changed:
                    self._write_document(document)
                return expected
            if changed:
                self._write_document(document)
        return None

    def resolve_session(self, token: str) -> LocalPrincipal | None:
        if not isinstance(token, str) or not token:
            return None
        digest = self._digest(token)
        with self._exclusive_lock():
            document = self._read_document()
            now = self._now()
            changed = self._prune_sessions(document, now)
            for session in document["sessions"]:
                if not hmac.compare_digest(str(session["token_digest"]), digest):
                    continue
                if (
                    session["purpose"] != "authenticated"
                    or self._parse_timestamp(session["expires_at"]) <= now
                ):
                    if changed:
                        self._write_document(document)
                    return None
                principal = self._principal_from_user_id(document, UUID(str(session["user_id"])))
                if changed:
                    self._write_document(document)
                return principal
            if changed:
                self._write_document(document)
        return None

    def verify_csrf(self, token: str, csrf_token: str, *, purpose: str) -> bool:
        if purpose not in {"preauth", "authenticated"}:
            raise ValueError("invalid session purpose")
        if not isinstance(token, str) or not isinstance(csrf_token, str) or not token or not csrf_token:
            return False
        token_digest = self._digest(token)
        csrf_digest = self._digest(csrf_token)
        with self._exclusive_lock():
            document = self._read_document()
            now = self._now()
            changed = self._prune_sessions(document, now)
            for session in document["sessions"]:
                if not hmac.compare_digest(str(session["token_digest"]), token_digest):
                    continue
                result = (
                    session["purpose"] == purpose
                    and self._parse_timestamp(session["expires_at"]) > now
                    and hmac.compare_digest(str(session["csrf_token_digest"]), csrf_digest)
                )
                if changed:
                    self._write_document(document)
                return result
            if changed:
                self._write_document(document)
        return False

    def revoke_session(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            return
        digest = self._digest(token)
        with self._exclusive_lock():
            document = self._read_document()
            changed = self._prune_sessions(document, self._now())
            remaining = [
                session
                for session in document["sessions"]
                if not hmac.compare_digest(str(session["token_digest"]), digest)
            ]
            if len(remaining) != len(document["sessions"]) or changed:
                document["sessions"] = remaining
                self._write_document(document)

    def rotate_session(self, token: str) -> tuple[str, str] | None:
        """Replace an authenticated session and its CSRF secret atomically."""
        if not isinstance(token, str) or not token:
            return None
        digest = self._digest(token)
        with self._exclusive_lock():
            document = self._read_document()
            now = self._now()
            changed = self._prune_sessions(document, now)
            for index, session in enumerate(document["sessions"]):
                if not hmac.compare_digest(str(session["token_digest"]), digest):
                    continue
                if (
                    session["purpose"] != "authenticated"
                    or self._parse_timestamp(session["expires_at"]) <= now
                ):
                    return None
                token_value = self._new_token()
                csrf_token = self._csrf_token(token_value)
                document["sessions"][index] = {
                    "token_digest": self._digest(token_value),
                    "csrf_token_digest": self._digest(csrf_token),
                    "user_id": session["user_id"],
                    "purpose": "authenticated",
                    "created_at": now.isoformat(),
                    "expires_at": (now + LOCAL_SESSION_TTL).isoformat(),
                }
                self._write_document(document)
                return token_value, csrf_token
            if changed:
                self._write_document(document)
        return None

    def change_password(
        self,
        user_id: UUID,
        current_password: str,
        new_password: str,
    ) -> LocalPrincipal:
        if not isinstance(user_id, UUID):
            raise TypeError("user_id must be a UUID")
        if not isinstance(current_password, str):
            raise LocalControlStoreError("invalid local credentials")
        with self._exclusive_lock():
            document = self._read_document()
            user = self._find_user_by_id(document, user_id)
            if user is None:
                raise LocalControlStoreNotFoundError("local principal not found")
            if not verify_password(str(user["password_hash"]), current_password):
                raise LocalControlStoreError("invalid local credentials")
            self._validate_password(new_password, username=str(user["username"]))
            user["password_hash"] = self._password_hash(new_password)
            user["must_change_password"] = False
            document["sessions"] = [
                session for session in document["sessions"] if session["user_id"] != str(user_id)
            ]
            self._prune_sessions(document, self._now())
            self._write_document(document)
            return self._principal_from_user(document, user)

    def require_team(self, token: str, team_id: UUID) -> LocalPrincipal:
        if not isinstance(team_id, UUID):
            raise TypeError("team_id must be a UUID")
        principal = self.resolve_session(token)
        if principal is None or principal.team_id != team_id:
            raise LocalControlStoreNotFoundError("local principal not found")
        return principal

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self._verify_trusted_root()
        operation_fd = self._open_transaction_root()
        self._active_root_fd = operation_fd
        try:
            if self._entry_is_symlink(_LOCK_FILE_NAME) or self._entry_is_symlink(
                _CONTROL_FILE_NAME
            ):
                raise LocalControlStoreError("unsafe local control path")
            descriptor = self._open_lock_file()
            with os.fdopen(descriptor, "r+b", closefd=True) as lock_file:
                if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                    raise LocalControlStoreError("unsafe local control path")
                os.fchmod(lock_file.fileno(), 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    self._verify_trusted_root()
                    if self._entry_is_symlink(_LOCK_FILE_NAME) or self._entry_is_symlink(
                        _CONTROL_FILE_NAME
                    ):
                        raise LocalControlStoreError("unsafe local control path")
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except LocalControlStoreError:
            raise
        except OSError:
            raise LocalControlStoreError("local control persistence failed") from None
        finally:
            self._active_root_fd = -1
            os.close(operation_fd)

    def _read_document(self) -> dict[str, Any]:
        if self._entry_is_symlink(_CONTROL_FILE_NAME):
            raise LocalControlStoreError("unsafe local control path")
        try:
            descriptor = os.open(
                _CONTROL_FILE_NAME,
                os.O_RDONLY
                | os.O_NONBLOCK
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self._operation_root_fd,
            )
        except FileNotFoundError:
            return self._empty_document()
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                file_status = os.fstat(stream.fileno())
                if not stat.S_ISREG(file_status.st_mode):
                    raise ValueError
                size = file_status.st_size
                if not 0 < size <= _MAX_CONTROL_BYTES:
                    raise ValueError
                raw = stream.read()
            document = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_constant,
            )
            migrated = self._migrate_v1_document(document)
            self._validate_document(document)
            if migrated:
                self._write_document(document)
            return document
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise LocalControlStoreError("invalid local control state") from None

    def _write_document(self, document: dict[str, Any]) -> None:
        self._validate_document(document)
        self._verify_trusted_root()
        if self._entry_is_symlink(_CONTROL_FILE_NAME):
            raise LocalControlStoreError("unsafe local control path")
        temporary_name: str | None = None
        try:
            payload = json.dumps(
                document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            if len(payload) > _MAX_CONTROL_BYTES:
                raise LocalControlStoreError("local control persistence failed")
            temporary_name = f".{_CONTROL_FILE_NAME}.{uuid4().hex}.tmp"
            descriptor = os.open(
                temporary_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._operation_root_fd,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._verify_trusted_root()
            if self._entry_is_symlink(_CONTROL_FILE_NAME):
                raise LocalControlStoreError("unsafe local control path")
            os.replace(
                temporary_name,
                _CONTROL_FILE_NAME,
                src_dir_fd=self._operation_root_fd,
                dst_dir_fd=self._operation_root_fd,
            )
            os.fsync(self._operation_root_fd)
        except LocalControlStoreError:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=self._operation_root_fd)
                except OSError:
                    pass
            raise
        except OSError:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=self._operation_root_fd)
                except OSError:
                    pass
            raise LocalControlStoreError("local control persistence failed") from None

    @staticmethod
    def _empty_document() -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "users": [],
            "teams": [],
            "sessions": [],
        }

    @staticmethod
    def _migrate_v1_document(document: object) -> bool:
        """Upgrade only the prior authenticated-session layout in place."""
        if (
            not isinstance(document, dict)
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or set(document) != {"schema_version", "users", "teams", "sessions"}
            or not isinstance(document.get("sessions"), list)
        ):
            return False
        for session in document["sessions"]:
            if not isinstance(session, dict) or set(session) != {
                "token_digest",
                "csrf_token_digest",
                "user_id",
                "created_at",
                "expires_at",
            }:
                return False
            session["purpose"] = "authenticated"
        document["schema_version"] = _SCHEMA_VERSION
        return True

    def _verify_trusted_root(self) -> None:
        try:
            current = os.lstat(self._root)
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != self._root_identity
                or self._root_fd < 0
            ):
                raise ValueError
            held = os.fstat(self._root_fd)
            if (held.st_dev, held.st_ino) != self._root_identity:
                raise ValueError
        except (OSError, ValueError):
            raise LocalControlStoreError("unsafe local control path") from None

    def _entry_is_symlink(self, name: str) -> bool:
        try:
            entry = os.stat(name, dir_fd=self._operation_root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            raise LocalControlStoreError("local control persistence failed") from None
        return stat.S_ISLNK(entry.st_mode)

    def _secure_existing_control_file(self) -> None:
        try:
            descriptor = os.open(
                _CONTROL_FILE_NAME,
                os.O_RDONLY
                | os.O_NONBLOCK
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self._operation_root_fd,
            )
        except FileNotFoundError:
            return
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LocalControlStoreError("unsafe local control path")
            os.fchmod(stream.fileno(), 0o600)

    def _open_lock_file(self) -> int:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(
                _LOCK_FILE_NAME,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=self._operation_root_fd,
            )
        except FileExistsError:
            return os.open(
                _LOCK_FILE_NAME,
                flags,
                dir_fd=self._operation_root_fd,
            )

    @property
    def _operation_root_fd(self) -> int:
        active = getattr(self, "_active_root_fd", -1)
        return active if active >= 0 else self._root_fd

    def _open_transaction_root(self) -> int:
        try:
            descriptor = os.open(
                self._root,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != self._root_identity:
                os.close(descriptor)
                raise LocalControlStoreError("unsafe local control path")
            return descriptor
        except LocalControlStoreError:
            raise
        except OSError:
            raise LocalControlStoreError("unsafe local control path") from None

    @staticmethod
    def _validate_document(document: object) -> None:
        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "users",
            "teams",
            "sessions",
        }:
            raise ValueError
        if type(document["schema_version"]) is not int or document["schema_version"] != _SCHEMA_VERSION:
            raise ValueError
        users = document["users"]
        teams = document["teams"]
        sessions = document["sessions"]
        if not isinstance(users, list) or not isinstance(teams, list) or not isinstance(sessions, list):
            raise ValueError
        team_ids: set[str] = set()
        for team in teams:
            if not isinstance(team, dict) or set(team) != {"team_id", "name"}:
                raise ValueError
            team_id = LocalControlStore._validated_uuid(team["team_id"])
            if (
                not isinstance(team["name"], str)
                or not team["name"]
                or str(team_id) in team_ids
            ):
                raise ValueError
            team_ids.add(str(team_id))
        usernames: set[str] = set()
        user_ids: set[str] = set()
        referenced_team_ids: set[str] = set()
        for user in users:
            if not isinstance(user, dict) or set(user) != {
                "user_id",
                "username",
                "team_id",
                "password_hash",
                "is_platform_admin",
                "must_change_password",
            }:
                raise ValueError
            user_id = LocalControlStore._validated_uuid(user["user_id"])
            team_id = LocalControlStore._validated_uuid(user["team_id"])
            username = user["username"]
            if (
                not isinstance(username, str)
                or normalize_username(username) != username
                or not username
                or len(username) > 128
                or not isinstance(user["password_hash"], str)
                or not user["password_hash"]
                or not isinstance(user["is_platform_admin"], bool)
                or not isinstance(user["must_change_password"], bool)
                or username in usernames
                or str(user_id) in user_ids
                or str(team_id) not in team_ids
                or str(team_id) in referenced_team_ids
            ):
                raise ValueError
            usernames.add(username)
            user_ids.add(str(user_id))
            referenced_team_ids.add(str(team_id))
        if referenced_team_ids != team_ids:
            raise ValueError
        for session in sessions:
            if not isinstance(session, dict) or set(session) != {
                "token_digest",
                "csrf_token_digest",
                "user_id",
                "purpose",
                "created_at",
                "expires_at",
            }:
                raise ValueError
            if (
                not isinstance(session["token_digest"], str)
                or len(session["token_digest"]) != 64
                or not isinstance(session["csrf_token_digest"], str)
                or len(session["csrf_token_digest"]) != 64
                or session["purpose"] not in {"preauth", "authenticated"}
            ):
                raise ValueError
            if session["purpose"] == "authenticated":
                if str(LocalControlStore._validated_uuid(session["user_id"])) not in user_ids:
                    raise ValueError
            elif session["user_id"] is not None:
                raise ValueError
            created_at = LocalControlStore._parse_timestamp(session["created_at"])
            expires_at = LocalControlStore._parse_timestamp(session["expires_at"])
            if expires_at <= created_at:
                raise ValueError

    @staticmethod
    def _validated_uuid(value: object) -> UUID:
        if not isinstance(value, str):
            raise ValueError
        parsed = UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError
        return parsed

    @staticmethod
    def _parse_timestamp(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError
        return parsed.astimezone(UTC)

    def _find_user(self, document: dict[str, Any], username: str) -> dict[str, Any] | None:
        return next((user for user in document["users"] if user["username"] == username), None)

    @staticmethod
    def _find_user_by_id(document: dict[str, Any], user_id: UUID) -> dict[str, Any] | None:
        return next(
            (user for user in document["users"] if user["user_id"] == str(user_id)), None
        )

    def _principal_from_user_id(
        self, document: dict[str, Any], user_id: UUID
    ) -> LocalPrincipal | None:
        user = self._find_user_by_id(document, user_id)
        return self._principal_from_user(document, user) if user is not None else None

    @staticmethod
    def _principal_from_user(document: dict[str, Any], user: dict[str, Any]) -> LocalPrincipal:
        team = next(
            (team for team in document["teams"] if team["team_id"] == user["team_id"]), None
        )
        if team is None:
            raise LocalControlStoreError("invalid local control state")
        return LocalPrincipal(
            user_id=UUID(str(user["user_id"])),
            username=str(user["username"]),
            team_id=UUID(str(user["team_id"])),
            team_name=str(team["name"]),
            is_platform_admin=bool(user["is_platform_admin"]),
            must_change_password=bool(user["must_change_password"]),
        )

    def _new_uuid(self) -> UUID:
        candidate = self._uuid_factory()
        if not isinstance(candidate, UUID) or candidate.version != 4:
            raise LocalControlStoreError("local control persistence failed")
        return candidate

    def _new_token(self) -> str:
        token = self._token_factory()
        if not isinstance(token, str) or not token:
            raise LocalControlStoreError("local control persistence failed")
        return token

    @staticmethod
    def _csrf_token(token: str) -> str:
        return hmac.new(token.encode("utf-8"), _CSRF_CONTEXT, hashlib.sha256).hexdigest()

    def _append_session(
        self,
        document: dict[str, Any],
        token: str,
        csrf_token: str,
        user_id: str | None,
        purpose: str,
        now: datetime,
    ) -> None:
        document["sessions"].append(
            {
                "token_digest": self._digest(token),
                "csrf_token_digest": self._digest(csrf_token),
                "user_id": user_id,
                "purpose": purpose,
                "created_at": now.isoformat(),
                "expires_at": (now + LOCAL_SESSION_TTL).isoformat(),
            }
        )

    @staticmethod
    def _session_order(session: dict[str, Any]) -> tuple[datetime, str]:
        return (
            LocalControlStore._parse_timestamp(session["created_at"]),
            str(session["token_digest"]),
        )

    def _prune_sessions(self, document: dict[str, Any], now: datetime) -> bool:
        active = [
            session
            for session in document["sessions"]
            if self._parse_timestamp(session["expires_at"]) > now
        ]
        preauth = sorted(
            (session for session in active if session["purpose"] == "preauth"),
            key=self._session_order,
        )[-_MAX_PREAUTH_SESSIONS:]
        per_user: dict[str, list[dict[str, Any]]] = {}
        for session in active:
            if session["purpose"] == "authenticated":
                per_user.setdefault(str(session["user_id"]), []).append(session)
        authenticated = [
            session
            for user_id in sorted(per_user)
            for session in sorted(per_user[user_id], key=self._session_order)[
                -_MAX_AUTHENTICATED_SESSIONS_PER_USER:
            ]
        ]
        authenticated = sorted(authenticated, key=self._session_order)[
            -_MAX_AUTHENTICATED_SESSIONS:
        ]
        kept = sorted([*authenticated, *preauth], key=self._session_order)
        changed = kept != document["sessions"]
        document["sessions"] = kept
        return changed

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise LocalControlStoreError("local control persistence failed")
        return now.astimezone(UTC)

    @staticmethod
    def _validate_new_credentials(username: str, password: str) -> str:
        if not isinstance(username, str):
            raise LocalControlStoreError("invalid local credentials")
        normalized = normalize_username(username)
        if not normalized or len(normalized) > 128:
            raise LocalControlStoreError("invalid local credentials")
        LocalControlStore._validate_password(password, username=normalized)
        return normalized

    @staticmethod
    def _validate_password(password: str, *, username: str | None = None) -> None:
        if (
            not isinstance(password, str)
            or len(password) < 12
            or (username is not None and normalize_username(password) == username)
        ):
            raise LocalControlStoreError("invalid local credentials")

    @staticmethod
    def _password_hash(password: str) -> str:
        try:
            return hash_password(password)
        except Exception:
            raise LocalControlStoreError("invalid local credentials") from None

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "EnsureUserResult",
    "LOCAL_SESSION_TTL",
    "LocalControlStore",
    "LocalControlStoreError",
    "LocalControlStoreNotFoundError",
    "LocalPrincipal",
]
