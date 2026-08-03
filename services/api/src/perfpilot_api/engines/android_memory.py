"""Android memory adapter for bounded, isolated capture analysis."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from json.decoder import scanstring
from typing import Any, Final
from urllib.parse import unquote
from uuid import UUID

from pydantic import SecretStr, ValidationError

from perfpilot_api.engines.android_memory_contracts import AndroidMemoryContext
from perfpilot_api.engines.android_memory_stager import AndroidMemoryStager, StagedMemoryInput
from perfpilot_api.engines.android_memory_worker import AndroidMemoryWorker
from perfpilot_api.engines.contracts import (
    AdapterDescriptor,
    EngineEvent,
    EngineEventBatch,
    EngineInput,
    EngineResult,
    EngineRunRef,
    EngineStatus,
    EngineTerminalStateValue,
    SubmitConfig,
)
from perfpilot_api.engines.errors import EngineAdapterError


_RUN_ID: Final = re.compile(r"memory-[0-9a-f]{32}\Z")
_ALLOWED_INPUTS: Final = frozenset(
    {
        "memory_capture_manifest",
        "memory_evidence",
        "capture_manifest",
        "log",
        "screenshot",
        "trace",
    }
)
_STAGER_CODES: Final = frozenset(
    {
        "missing_input",
        "manifest_invalid",
        "download_failed",
        "integrity_mismatch",
        "input_limit_exceeded",
    }
)
_PROGRESS: Final = (
    ("1", 10, "downloading"),
    ("2", 35, "verifying"),
    ("3", 65, "analyzing"),
)
_MAX_INPUTS: Final = 2049
_MAX_OUTPUT_BYTES: Final = 32 * 1024 * 1024
_MAX_JSON_DEPTH: Final = 64
_MAX_JSON_NODES: Final = 200_000
_MAX_JSON_STRING_CHARS: Final = 16 * 1024 * 1024
_CONSERVATIVE_PRIVACY_MARKERS: Final = (
    "/work/input",
    "file://",
    "x-amz-signature",
)
_DATABASE_URL: Final = re.compile(
    r"\A(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis(?:s)?|sqlite|"
    r"sqlserver|oracle)(?:\+[a-z0-9_.-]+)?://\S+\Z",
    re.IGNORECASE,
)
_CREDENTIAL_URL: Final = re.compile(
    r"\A[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@\S+\Z",
    re.IGNORECASE,
)
_OBJECT_KEY_SECRET: Final = re.compile(
    r"\Aobject[\s_-]*key(?:\s*[:=]\s*\S.*)?\Z",
    re.IGNORECASE,
)
_SENSITIVE_KEYS: Final = frozenset(
    {
        "access_key",
        "access_token",
        "api_key",
        "authorization",
        "aws_access_key_id",
        "aws_secret_access_key",
        "client_secret",
        "credential",
        "credentials",
        "object_key",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "session_token",
        "token",
    }
)
_PATH_KEYS: Final = frozenset(
    {
        "path",
        "paths",
        "directory",
        "directories",
        "dir",
        "dirs",
        "root",
        "roots",
        "location",
        "locations",
    }
)
_PATH_KEY_SUFFIXES: Final = tuple(f"_{key}" for key in _PATH_KEYS)
_WINDOWS_DRIVE_PATH: Final = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)
_CAMEL_ACRONYM_BOUNDARY: Final = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY: Final = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEPARATOR: Final = re.compile(r"[-\s]+")
_REPEATED_UNDERSCORE: Final = re.compile(r"_+")
_JSON_NUMBER: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_MAX_PRIVACY_VARIANT_CHARS: Final = _MAX_JSON_STRING_CHARS
_MISSING: Final = object()


class _DuplicateJsonKey(ValueError):
    __slots__ = ()


class _InvalidJsonConstant(ValueError):
    __slots__ = ()


class _JsonPreflightError(ValueError):
    __slots__ = ()


class _JsonPreflight:
    __slots__ = ("_index", "_nodes", "_string_chars", "_text")

    def __init__(self, text: str) -> None:
        self._text = text
        self._index = 0
        self._nodes = 0
        self._string_chars = 0

    def run(self) -> None:
        self._skip_whitespace()
        self._parse_value(0)
        self._skip_whitespace()
        if self._index != len(self._text):
            raise _JsonPreflightError

    def _skip_whitespace(self) -> None:
        text = self._text
        index = self._index
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        self._index = index

    def _take_node(self, depth: int) -> None:
        self._nodes += 1
        if self._nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise _JsonPreflightError

    def _parse_string(self) -> str:
        if self._index >= len(self._text) or self._text[self._index] != '"':
            raise _JsonPreflightError
        decoded, end = scanstring(self._text, self._index + 1, True)
        self._index = end
        self._string_chars += len(decoded)
        if self._string_chars > _MAX_JSON_STRING_CHARS or any(
            0xD800 <= ord(character) <= 0xDFFF for character in decoded
        ):
            raise _JsonPreflightError
        return decoded

    def _parse_value(self, depth: int) -> None:
        self._take_node(depth)
        if self._index >= len(self._text):
            raise _JsonPreflightError
        token = self._text[self._index]
        if token == '"':
            self._parse_string()
            return
        if token == "{":
            self._index += 1
            self._parse_object(depth)
            return
        if token == "[":
            self._index += 1
            self._parse_array(depth)
            return
        for literal in ("null", "true", "false"):
            if self._text.startswith(literal, self._index):
                self._index += len(literal)
                return
        number = _JSON_NUMBER.match(self._text, self._index)
        if number is None:
            raise _JsonPreflightError
        self._index = number.end()

    def _parse_object(self, depth: int) -> None:
        keys: set[str] = set()
        self._skip_whitespace()
        if self._consume_if("}"):
            return
        while True:
            self._take_node(depth + 1)
            key = self._parse_string()
            if key in keys:
                raise _DuplicateJsonKey
            keys.add(key)
            self._skip_whitespace()
            if not self._consume_if(":"):
                raise _JsonPreflightError
            self._skip_whitespace()
            self._parse_value(depth + 1)
            self._skip_whitespace()
            if self._consume_if("}"):
                return
            if not self._consume_if(","):
                raise _JsonPreflightError
            self._skip_whitespace()

    def _parse_array(self, depth: int) -> None:
        self._skip_whitespace()
        if self._consume_if("]"):
            return
        while True:
            self._parse_value(depth + 1)
            self._skip_whitespace()
            if self._consume_if("]"):
                return
            if not self._consume_if(","):
                raise _JsonPreflightError
            self._skip_whitespace()

    def _consume_if(self, token: str) -> bool:
        if self._index < len(self._text) and self._text[self._index] == token:
            self._index += 1
            return True
        return False


def _preflight_json(text: str) -> bool:
    try:
        _JsonPreflight(text).run()
    except Exception:
        return False
    return True


def _error(stable_code: str, *, retryable: bool = False) -> EngineAdapterError:
    return EngineAdapterError(stable_code=stable_code, retryable=retryable)


def _worker_error(error: BaseException) -> EngineAdapterError:
    if isinstance(error, TimeoutError) or (
        isinstance(error, EngineAdapterError) and error.stable_code == "engine_timeout"
    ):
        return _error("engine_timeout", retryable=True)
    return _error("worker_unavailable", retryable=True)


async def _abandon_adapter_owned(staged: StagedMemoryInput) -> None:
    abandon_task = asyncio.create_task(staged.abandon())
    control_failure: BaseException | None = None
    while not abandon_task.done():
        try:
            await asyncio.shield(abandon_task)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
            if control_failure is None:
                control_failure = caught
        except Exception:
            break

    try:
        abandon_task.result()
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
        if control_failure is None:
            control_failure = caught
    except Exception:
        pass

    if control_failure is not None:
        control_failure.__cause__ = None
        control_failure.__context__ = None
        raise control_failure


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _reject_json_constant(_: str) -> object:
    raise _InvalidJsonConstant


def _is_safe_json_structure(root: object) -> bool:
    nodes = 0
    string_chars = 0
    stack: list[tuple[object, int]] = [(root, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            return False
        if value is None or isinstance(value, bool | int):
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                return False
            continue
        if isinstance(value, str):
            string_chars += len(value)
            if string_chars > _MAX_JSON_STRING_CHARS or any(
                0xD800 <= ord(character) <= 0xDFFF for character in value
            ):
                return False
            continue
        if isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
            continue
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    return False
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
            continue
        return False
    return True


def _normalize_key(key: str) -> str:
    separated = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", key.strip())
    separated = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", separated)
    separated = _KEY_SEPARATOR.sub("_", separated)
    return _REPEATED_UNDERSCORE.sub("_", separated).strip("_").casefold()


def _is_path_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return normalized in _PATH_KEYS or normalized.endswith(_PATH_KEY_SUFFIXES)


def _is_absolute_path(value: str) -> bool:
    return (
        value.startswith("/")
        or value.startswith("\\\\")
        or _WINDOWS_DRIVE_PATH.match(value) is not None
    )


def _privacy_variants(value: str) -> tuple[str, ...] | None:
    current = value.strip()
    if len(current) > _MAX_PRIVACY_VARIANT_CHARS:
        return None
    variants = [current]
    for _ in range(2):
        if "%" not in current:
            break
        try:
            decoded = unquote(current, encoding="utf-8", errors="strict").strip()
        except UnicodeError:
            return None
        if len(decoded) > _MAX_PRIVACY_VARIANT_CHARS:
            return None
        if decoded == current:
            break
        variants.append(decoded)
        current = decoded
    return tuple(variants)


def _is_private_string(value: str, *, is_key: bool) -> bool:
    variants = _privacy_variants(value)
    if variants is None:
        return True
    for variant in variants:
        folded = variant.casefold()
        if any(marker in folded for marker in _CONSERVATIVE_PRIVACY_MARKERS):
            return True
        if _is_absolute_path(variant):
            return True
        if _DATABASE_URL.fullmatch(variant) is not None:
            return True
        if _CREDENTIAL_URL.fullmatch(variant) is not None:
            return True
        if is_key and _normalize_key(variant) in _SENSITIVE_KEYS:
            return True
        if not is_key and _OBJECT_KEY_SECRET.fullmatch(variant) is not None:
            return True
    return False


def _contains_privacy_marker(root: object) -> bool:
    stack = [(root, False, False)]
    while stack:
        value, path_semantics, is_key = stack.pop()
        if isinstance(value, str):
            if _is_private_string(value, is_key=is_key):
                return True
        elif isinstance(value, list):
            stack.extend((item, path_semantics, False) for item in value)
        elif isinstance(value, dict):
            for key, item in value.items():
                stack.append((key, path_semantics, True))
                stack.append((item, path_semantics or _is_path_key(key), False))
    return False


def _privacy_flags(root: dict[str, object]) -> tuple[object, object]:
    contract = root.get("analysis_contract")
    if not isinstance(contract, dict):
        return _MISSING, _MISSING
    privacy = contract.get("privacy")
    if not isinstance(privacy, dict):
        return _MISSING, _MISSING
    return (
        privacy.get("raw_contents_embedded", _MISSING),
        privacy.get("local_paths_included", _MISSING),
    )


class AndroidMemoryAdapter:
    """Stage verified artifacts and delegate analysis to an isolated worker."""

    descriptor = AdapterDescriptor(
        engine_id="android_memory",
        adapter_version="1.0.0",
        profiles=frozenset({"auto"}),
        required_inputs=frozenset({"memory_capture_manifest"}),
        optional_inputs=frozenset(
            {"memory_evidence", "capture_manifest", "log", "screenshot", "trace"}
        ),
        accepted_contracts=frozenset({"android-memory-ai-context-1.2"}),
        default_timeout_seconds=900,
        resource_profile="isolated_worker",
        stable_error_codes=frozenset(
            {
                "missing_input",
                "manifest_invalid",
                "download_failed",
                "integrity_mismatch",
                "input_limit_exceeded",
                "worker_unavailable",
                "engine_timeout",
                "engine_failed",
                "invalid_output",
                "incompatible_contract",
                "privacy_violation",
            }
        ),
    )

    def __init__(
        self,
        *,
        stager: AndroidMemoryStager,
        worker: AndroidMemoryWorker,
        max_timeout_seconds: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if type(max_timeout_seconds) is not int or max_timeout_seconds < 1:
            raise ValueError("Android memory adapter bounds are invalid")
        if getattr(worker, "isolation", None) not in {"local", "oci"}:
            raise ValueError("Android memory worker isolation is invalid")
        self._stager = stager
        self._worker = worker
        self._max_timeout_seconds = min(
            max_timeout_seconds, self.descriptor.default_timeout_seconds
        )
        self._now = now or (lambda: datetime.now(UTC))

    async def submit(
        self,
        inputs: tuple[EngineInput, ...],
        config: SubmitConfig,
    ) -> EngineRunRef:
        self._validate_config(config)
        self._validate_inputs(inputs)
        run_id = f"memory-{config.execution_id.hex}"

        staged = None
        stage_failure: EngineAdapterError | None = None
        try:
            staged = await self._stager.stage(run_id=run_id, inputs=inputs)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except EngineAdapterError as caught:
            stable_code = (
                caught.stable_code if caught.stable_code in _STAGER_CODES else "download_failed"
            )
            stage_failure = _error(stable_code, retryable=caught.retryable)
        except BaseException:
            stage_failure = _error("download_failed", retryable=True)
        if stage_failure is not None:
            raise stage_failure
        assert staged is not None
        if getattr(getattr(staged, "manifest", None), "analysis_id", None) != config.analysis_id:
            await _abandon_adapter_owned(staged)
            raise _error("manifest_invalid")

        start_failure: EngineAdapterError | None = None
        control_failure: BaseException | None = None
        try:
            await self._worker.start(
                run_id=run_id,
                staged=staged,
                question=config.question,
                timeout_seconds=config.timeout_seconds,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as caught:
            control_failure = caught
        except BaseException as caught:
            start_failure = _worker_error(caught)
        if control_failure is not None:
            control_failure.__cause__ = None
            control_failure.__context__ = None
            raise control_failure
        if start_failure is not None:
            raise start_failure

        return EngineRunRef(
            engine_id=self.descriptor.engine_id,
            external_session_id=None,
            external_run_id=run_id,
            cursor=None,
            external_workspace_id=None,
        )

    async def stream(
        self,
        run_ref: EngineRunRef,
        cursor: str | None,
    ) -> EngineEventBatch:
        run_id = self._validate_run_ref(run_ref)
        if cursor not in {None, "0", "1", "2", "3"} or (
            run_ref.cursor is not None and cursor != run_ref.cursor
        ):
            raise _error("incompatible_contract")
        worker_state = await self._read_worker_state(run_id)
        if worker_state != "running":
            return EngineEventBatch(run_ref=run_ref, events=())

        after = 0 if cursor is None else int(cursor)
        events = tuple(
            EngineEvent(
                event_id=event_id,
                state="running",
                progress_percent=progress,
                message_code=message_code,
                occurred_at=self._now(),
            )
            for event_id, progress, message_code in _PROGRESS
            if int(event_id) > after
        )
        refreshed = replace(
            run_ref,
            cursor=events[-1].event_id if events else run_ref.cursor,
        )
        return EngineEventBatch(run_ref=refreshed, events=events)

    async def status(self, run_ref: EngineRunRef) -> EngineStatus:
        run_id = self._validate_run_ref(run_ref)
        worker_state = await self._read_worker_state(run_id)
        if worker_state == "running":
            return EngineStatus(run_ref, "running", None, False)
        if worker_state == "completed":
            result = await self._read_worker_result(run_id)
            if result.exit_code == 0:
                return EngineStatus(run_ref, "completed", None, False)
            if result.exit_code == 2:
                return EngineStatus(run_ref, "insufficient_data", None, False)
            return EngineStatus(run_ref, "failed", "engine_failed", False)
        if worker_state == "failed":
            return EngineStatus(run_ref, "failed", "engine_failed", False)
        if worker_state == "timed_out":
            return EngineStatus(run_ref, "failed", "engine_timeout", True)
        if worker_state == "canceled":
            return EngineStatus(run_ref, "canceled", None, False)
        return EngineStatus(run_ref, "failed", "worker_unavailable", True)

    async def fetch_result(self, run_ref: EngineRunRef) -> EngineResult:
        run_id = self._validate_run_ref(run_ref)
        worker_state = await self._read_worker_state(run_id)
        if worker_state == "timed_out":
            raise _error("engine_timeout", retryable=True)
        if worker_state == "lost":
            raise _error("worker_unavailable", retryable=True)
        if worker_state == "running":
            raise _error("worker_unavailable", retryable=True)
        if worker_state in {"failed", "canceled"}:
            raise _error("engine_failed")
        result = await self._read_worker_result(run_id)
        if result.exit_code not in {0, 2}:
            raise _error("engine_failed")
        if not isinstance(result.payload, bytes) or not result.payload:
            raise _error("invalid_output")

        payload, parse_failure = self._parse_payload(result.payload)
        if parse_failure is not None:
            raise _error(parse_failure)
        assert payload is not None

        try:
            validated = AndroidMemoryContext.model_validate(payload, strict=True)
        except ValidationError:
            validated = None
        if validated is None:
            raise _error("incompatible_contract")
        support_level = validated.analysis_contract.support_level
        if (result.exit_code == 2) != (support_level == "insufficient"):
            raise _error("invalid_output")

        canonical = validated.model_dump(mode="json")
        if not isinstance(canonical, dict) or not _is_safe_json_structure(canonical):
            raise _error("invalid_output")
        canonical_privacy_flags = _privacy_flags(canonical)
        if any(flag is True for flag in canonical_privacy_flags) or _contains_privacy_marker(
            canonical
        ):
            raise _error("privacy_violation")
        state: EngineTerminalStateValue = (
            "completed" if result.exit_code == 0 else "insufficient_data"
        )
        return EngineResult(
            contract="android-memory-ai-context-1.2",
            state=state,
            payload=canonical,
        )

    async def cancel(self, run_ref: EngineRunRef) -> EngineTerminalStateValue:
        run_id = self._validate_run_ref(run_ref)
        worker_state = await self._read_worker_state(run_id)
        if worker_state == "lost":
            raise _error("worker_unavailable", retryable=True)
        if worker_state == "running":
            cancel_failure: EngineAdapterError | None = None
            try:
                await self._worker.cancel(run_id)
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except BaseException as caught:
                cancel_failure = _worker_error(caught)
            if cancel_failure is not None:
                raise cancel_failure
            worker_state = await self._read_worker_state(run_id)

        if worker_state == "completed":
            result = await self._read_worker_result(run_id)
            if result.exit_code == 0:
                return "completed"
            if result.exit_code == 2:
                return "insufficient_data"
            return "failed"
        if worker_state == "canceled":
            return "canceled"
        if worker_state in {"failed", "timed_out"}:
            return "failed"
        raise _error("worker_unavailable", retryable=True)

    def _validate_config(self, config: SubmitConfig) -> None:
        if (
            not isinstance(config, SubmitConfig)
            or type(config.execution_id) is not UUID
            or type(config.analysis_id) is not UUID
            or config.profile != "auto"
            or config.external_workspace_id is not None
            or not (
                config.question is None
                or (
                    type(config.question) is str
                    and "\x00" not in config.question
                    and len(config.question) <= 16_384
                )
            )
        ):
            raise _error("manifest_invalid")
        if (
            type(config.timeout_seconds) is not int
            or not 0 < config.timeout_seconds <= self._max_timeout_seconds
        ):
            raise _error("engine_timeout")

    def _validate_inputs(self, inputs: tuple[EngineInput, ...]) -> None:
        if type(inputs) is not tuple or len(inputs) > _MAX_INPUTS:
            raise _error("input_limit_exceeded")
        manifests = tuple(
            item for item in inputs if getattr(item, "kind", None) == "memory_capture_manifest"
        )
        if not manifests:
            raise _error("missing_input")
        if len(manifests) != 1:
            raise _error("manifest_invalid")

        artifact_ids: list[UUID] = []
        for item in inputs:
            if not isinstance(item, EngineInput):
                raise _error("manifest_invalid")
            artifact_ids.append(item.artifact_id)
            if (
                type(item.artifact_id) is not UUID
                or type(item.kind) is not str
                or item.kind not in _ALLOWED_INPUTS
                or type(item.mime) is not str
                or not item.mime
                or len(item.mime) > 255
                or any(character in item.mime for character in "\x00\r\n")
                or type(item.size_bytes) is not int
                or item.size_bytes < 0
                or type(item.sha256_b64) is not str
                or not isinstance(item.download_url, SecretStr)
            ):
                raise _error("manifest_invalid")
            raw_url = item.download_url.get_secret_value()
            if (
                not raw_url
                or len(raw_url) > 16_384
                or any(character in raw_url for character in "\x00\r\n")
            ):
                raise _error("manifest_invalid")
            try:
                digest = base64.b64decode(item.sha256_b64, validate=True)
            except (ValueError, binascii.Error):
                raise _error("manifest_invalid") from None
            if (
                len(digest) != hashlib.sha256().digest_size
                or base64.b64encode(digest).decode("ascii") != item.sha256_b64
            ):
                raise _error("manifest_invalid")
        if len(set(artifact_ids)) != len(artifact_ids):
            raise _error("manifest_invalid")

    def _validate_run_ref(self, run_ref: EngineRunRef) -> str:
        if (
            not isinstance(run_ref, EngineRunRef)
            or run_ref.engine_id != self.descriptor.engine_id
            or run_ref.external_session_id is not None
            or run_ref.external_workspace_id is not None
            or not isinstance(run_ref.external_run_id, str)
            or _RUN_ID.fullmatch(run_ref.external_run_id) is None
            or run_ref.cursor not in {None, "0", "1", "2", "3"}
        ):
            raise _error("incompatible_contract")
        return run_ref.external_run_id

    async def _read_worker_state(self, run_id: str) -> str:
        failure: EngineAdapterError | None = None
        state: str | None = None
        try:
            state = await self._worker.status(run_id)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as caught:
            failure = _worker_error(caught)
        if failure is not None:
            raise failure
        if state not in {"running", "completed", "failed", "timed_out", "canceled", "lost"}:
            return "lost"
        return state

    async def _read_worker_result(self, run_id: str) -> Any:
        failure: EngineAdapterError | None = None
        result: Any = None
        try:
            result = await self._worker.result(run_id)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as caught:
            failure = _worker_error(caught)
        if failure is not None:
            raise failure
        if type(getattr(result, "exit_code", None)) is not int:
            raise _error("invalid_output")
        return result

    @staticmethod
    def _parse_payload(
        raw_payload: bytes,
    ) -> tuple[dict[str, object] | None, str | None]:
        if len(raw_payload) > _MAX_OUTPUT_BYTES:
            return None, "invalid_output"
        parsed: object | None = None
        try:
            decoded = raw_payload.decode("utf-8", errors="strict")
            if not _preflight_json(decoded):
                return None, "invalid_output"
            parsed = json.loads(
                decoded,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, ValueError, RecursionError, MemoryError):
            return None, "invalid_output"
        if not isinstance(parsed, dict) or not _is_safe_json_structure(parsed):
            return None, "invalid_output"
        privacy_flags = _privacy_flags(parsed)
        if any(flag is True for flag in privacy_flags) or _contains_privacy_marker(parsed):
            return None, "privacy_violation"
        if any(flag is not False for flag in privacy_flags):
            return None, "incompatible_contract"
        return parsed, None


__all__ = ["AndroidMemoryAdapter"]
