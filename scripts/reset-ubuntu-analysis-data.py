#!/usr/bin/env python3
"""Permanently reset one verified analysis directory without pathname races."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _reject() -> None:
    raise RuntimeError("unsafe analysis data directory")


def _validated_path(text: str) -> Path:
    path = Path(text)
    if not text or not path.is_absolute() or os.path.normpath(text) != text:
        _reject()
    return path


def _open_parent(path: Path) -> tuple[int, str]:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:-1]:
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, path.name
    except BaseException:
        os.close(descriptor)
        raise


def _empty_tree(directory_fd: int) -> None:
    for child in os.listdir(directory_fd):
        status = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(status.st_mode):
            child_fd = os.open(child, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (status.st_dev, status.st_ino) != (opened.st_dev, opened.st_ino):
                    _reject()
                _empty_tree(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(child, dir_fd=directory_fd)
        else:
            os.unlink(child, dir_fd=directory_fd)
    os.fsync(directory_fd)


def reset_analysis_root(
    configured_text: str,
    expected_text: str,
    state_text: str,
    *,
    after_open: Callable[[], None] | None = None,
) -> None:
    if configured_text != expected_text:
        _reject()
    data = _validated_path(configured_text)
    state = _validated_path(state_text)
    if data == Path("/") or data == Path.home() or data.name in {
        "home",
        "state",
        "config",
        "project",
    }:
        _reject()
    if data == state or data in state.parents or state in data.parents:
        _reject()

    parent_fd, root_name = _open_parent(data)
    root_fd = -1
    try:
        before = os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            _reject()
        root_fd = os.open(root_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(root_fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            _reject()
        if after_open is not None:
            after_open()

        tombstone = f".{root_name}.reset-{secrets.token_hex(16)}"
        os.rename(root_name, tombstone, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        moved = os.stat(tombstone, dir_fd=parent_fd, follow_symlinks=False)
        if (moved.st_dev, moved.st_ino) != (opened.st_dev, opened.st_ino):
            try:
                os.stat(root_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(tombstone, root_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            _reject()
        _empty_tree(root_fd)
        os.rmdir(tombstone, dir_fd=parent_fd)
        os.close(root_fd)
        root_fd = -1
        os.mkdir(root_name, mode=0o700, dir_fd=parent_fd)
        recreated_fd = os.open(root_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            os.fchmod(recreated_fd, 0o700)
            os.fsync(recreated_fd)
        finally:
            os.close(recreated_fd)
        os.fsync(parent_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def main() -> None:
    setting = os.environ.get("PERFPILOT_RESET_ANALYSIS_DATA", "")
    if setting == "false":
        return
    if setting != "true":
        _reject()
    reset_analysis_root(
        os.environ.get("PERFPILOT_LOCAL_DATA_DIR", ""),
        os.environ.get("PERFPILOT_EXPECTED_ANALYSIS_ROOT", ""),
        os.environ.get("PERFPILOT_LOCAL_STATE_DIR", ""),
    )


if __name__ == "__main__":
    main()
