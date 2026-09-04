"""Shared local-file conversion and publication primitives.

This module is intentionally internal to the plugin.  It keeps the small set
of identity-sensitive operations shared by the Markdown, PDF, and file-router
pipelines in one place without turning their public CLIs into a common parser.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import mimetypes
import os
import shutil
import stat as stat_module
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable


import native_paths as np


class ConversionError(RuntimeError):
    """A deterministic conversion or publication failure."""


class OutputCollision(ConversionError):
    """A target already exists and neither overwrite nor rename was selected."""


def new_owned_dir(parent: os.PathLike[str] | str, prefix: str) -> np.OwnedEntry:
    parent_path = np.logical(parent)
    np.mkdir(parent_path, parents=True, exist_ok=True)
    return np.create_owned_dir(parent_path, prefix)


def new_owned_file(
    parent: os.PathLike[str] | str, prefix: str, suffix: str = ""
) -> np.OwnedEntry:
    parent_path = np.logical(parent)
    np.mkdir(parent_path, parents=True, exist_ok=True)
    return np.create_owned_file(parent_path, prefix, suffix)


def cleanup_owned(entry: np.OwnedEntry, *, warning: str | None = None) -> bool:
    try:
        np.remove_owned(entry)
        return True
    except OSError as exc:
        if warning:
            print(f"Warning: {warning} {entry.path}: {exc}", file=sys.stderr)
        return False


def _remove_overwrite_target(path: Path) -> None:
    """Remove exactly one existing target selected by an explicit overwrite.

    Directory replacement is not a single portable filesystem operation.  The
    caller has already opted into overwrite, so remove the identity-checked
    target and then let publication perform a no-replace rename.  This keeps
    the contract small: there is no backup, rollback, journal, or recovery
    service.
    """
    expected = np.EntryIdentity.capture(path)
    current = np.EntryIdentity.capture(path)
    if current != expected:
        raise ConversionError(f"Overwrite target identity changed before removal: {path}")
    info = np.lstat(path)
    if stat_module.S_ISLNK(info.st_mode) or np.is_reparse(info):
        raise ConversionError(f"Refusing to overwrite a link or reparse point: {path}")
    if stat_module.S_ISDIR(info.st_mode):
        shutil.rmtree(np.native(path))
    elif stat_module.S_ISREG(info.st_mode):
        np.unlink(path)
    else:
        raise ConversionError(f"Refusing to overwrite a non-file target: {path}")


def publish_owned(
    stage: np.OwnedEntry,
    target: os.PathLike[str] | str,
    overwrite: bool,
    *,
    verify_payload: Callable[[Path], None] | None = None,
) -> None:
    """Publish one validated owned entry without a recovery subsystem.

    Existing targets are rejected unless ``overwrite`` is explicit.  The
    replacement has no backup, rollback, or automatic recovery path.  Regular
    files use the operating-system replace operation; other target shapes are
    removed after an identity check and followed by a no-replace rename.  A
    failed publication retains the owned stage for manual handling.
    """
    target_path = np.logical(target)
    if not stage.matches(stage.path):
        raise ConversionError(f"Owned stage identity changed before publication: {stage.path}")
    target_exists = np.exists(target_path)
    if target_exists and not overwrite:
        raise OutputCollision(f"Output already exists: {target_path}")

    if verify_payload is not None:
        verify_payload(stage.path)

    try:
        if target_exists:
            stage_info = np.lstat(stage.path)
            target_info = np.lstat(target_path)
            if stat_module.S_ISREG(stage_info.st_mode) and stat_module.S_ISREG(target_info.st_mode):
                np.replace(stage.path, target_path)
            else:
                _remove_overwrite_target(target_path)
                np.rename_no_replace(stage.path, target_path)
        else:
            np.rename_no_replace(stage.path, target_path)
    except FileExistsError as exc:
        if not target_exists:
            raise OutputCollision(f"Output already exists: {target_path}") from exc
        raise ConversionError(
            f"Final publication failed for {target_path}: {exc}; retained owned stage at {stage.path}. "
            f"Manual next step: inspect {stage.path}, then manually publish or remove it before retrying."
        ) from exc
    except Exception as exc:
        raise ConversionError(
            f"Final publication failed for {target_path}: {exc}; retained owned stage at {stage.path}. "
            f"Manual next step: inspect {stage.path}, then manually publish or remove it before retrying."
        ) from exc


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> "FileIdentity":
        return cls(info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


@dataclass(frozen=True)
class SourceSnapshot:
    logical_path: Path
    physical_path: Path
    original_name: str
    media_type: str | None
    size_bytes: int
    sha256: str
    source_identity: FileIdentity
    snapshot_identity: np.EntryIdentity

    def canonical_identity(self) -> tuple[dict[str, object], str]:
        record: dict[str, object] = {
            "kind": "file",
            "file_name": self.original_name,
            "locator": str(self.logical_path),
            "sha256": self.sha256,
            "hash_basis": "source_bytes",
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }
        return record, f"sha256:{self.sha256}"

    def verify(self) -> None:
        if not self.snapshot_identity.matches(self.physical_path):
            raise ConversionError("Source snapshot was replaced during conversion")
        actual = np.sha256_file(self.physical_path)
        if actual != self.sha256:
            raise ConversionError(
                f"Source snapshot hash mismatch after conversion: expected {self.sha256}, got {actual}"
            )


def _media_type(name: str) -> str | None:
    suffix = Path(name).suffix.lower()
    office = {
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docm": "application/vnd.ms-word.document.macroEnabled.12",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
        ".pps": "application/vnd.ms-powerpoint",
        ".ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
        ".xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
    }
    return office.get(suffix) or mimetypes.guess_type(name)[0]


def _open_locked_source(path: Path) -> BinaryIO:
    """Open an original source for reading while denying write/delete sharing.

    On Windows this uses CreateFileW explicitly.  The POSIX fallback cannot
    express share denial, but retains the open-descriptor identity checks used
    by tests and by non-Windows development.
    """
    if os.name != "nt":
        return open(np.native(path), "rb")

    import msvcrt

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        np.native(path),
        GENERIC_READ,
        FILE_SHARE_READ,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb", closefd=True)


def _hash_stream(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def acquire_source_snapshot(
    source: os.PathLike[str] | str,
    destination: os.PathLike[str] | str,
    *,
    compatibility_copier: Callable[[os.PathLike[str] | str, os.PathLike[str] | str], None] | None = None,
) -> SourceSnapshot:
    """Acquire one immutable, identity-bound copy from the original source."""
    logical_source = np.logical(source)
    physical = np.logical(destination)
    if not np.is_file(logical_source):
        raise ConversionError(f"Source is not an ordinary local file: {logical_source}")
    if np.exists(physical):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(physical))
    np.mkdir(physical.parent, parents=True, exist_ok=True)

    with _open_locked_source(logical_source) as original:
        before_stat = os.fstat(original.fileno())
        if not stat_module.S_ISREG(before_stat.st_mode):
            raise ConversionError(f"Source is not an ordinary regular file: {logical_source}")
        before = FileIdentity.from_stat(before_stat)
        digest = _hash_stream(original)
        if compatibility_copier is not None:
            compatibility_copier(logical_source, physical)
            with open(np.native(physical), "r+b") as copied:
                copied.flush()
                os.fsync(copied.fileno())
        else:
            with open(np.native(physical), "xb") as copied:
                for chunk in iter(lambda: original.read(1024 * 1024), b""):
                    copied.write(chunk)
                copied.flush()
                os.fsync(copied.fileno())
        after = FileIdentity.from_stat(os.fstat(original.fileno()))
        if after != before:
            raise ConversionError("Original source identity changed while its snapshot was acquired")

    snapshot_digest = np.sha256_file(physical)
    if snapshot_digest != digest:
        raise ConversionError(
            f"Source snapshot hash mismatch: expected {digest}, got {snapshot_digest}"
        )
    copied_stat = np.stat(physical)
    if copied_stat.st_size != before.size:
        raise ConversionError(
            f"Source snapshot size mismatch: expected {before.size}, got {copied_stat.st_size}"
        )
    return SourceSnapshot(
        logical_path=logical_source,
        physical_path=physical,
        original_name=logical_source.name,
        media_type=_media_type(logical_source.name),
        size_bytes=before.size,
        sha256=digest,
        source_identity=before,
        snapshot_identity=np.EntryIdentity.capture(physical),
    )


def copy_from_snapshot(snapshot: SourceSnapshot, destination: os.PathLike[str] | str) -> Path:
    """Derive and verify a provider/output copy from the owned snapshot."""
    snapshot.verify()
    target = np.logical(destination)
    np.mkdir(target.parent, parents=True, exist_ok=True)
    np.copy_file(snapshot.physical_path, target)
    if np.sha256_file(target) != snapshot.sha256:
        raise ConversionError("Snapshot-derived copy hash mismatch")
    return target


def validate_target_not_source(target: os.PathLike[str] | str, snapshot_or_source) -> None:
    source = (
        snapshot_or_source.logical_path
        if isinstance(snapshot_or_source, SourceSnapshot)
        else np.logical(snapshot_or_source)
    )
    target_path = np.logical(target)
    if np.paths_equal(target_path, source):
        raise ConversionError("Refusing to overwrite or alias the source file")
    if np.exists(target_path) and np.is_file(target_path):
        try:
            left = np.stat(target_path)
            right = np.stat(source)
        except OSError:
            return
        if left.st_dev == right.st_dev and left.st_ino == right.st_ino:
            raise ConversionError("Refusing output that aliases the source file identity")
