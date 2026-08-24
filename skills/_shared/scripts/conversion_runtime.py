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


_HERE = Path(__file__).resolve()
_MARKDOWN_SCRIPTS = _HERE.parents[2] / "markdown-conversion" / "scripts"
if str(_MARKDOWN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_MARKDOWN_SCRIPTS))

import native_paths as np  # noqa: E402


class ConversionError(RuntimeError):
    """A deterministic conversion or publication failure."""


class OutputCollision(ConversionError):
    """A target already exists and neither overwrite nor rename was selected."""


_OWNED_ENTRIES: dict[str, np.OwnedEntry] = {}


def _owned_key(path: os.PathLike[str] | str) -> str:
    return os.path.normcase(str(np.logical(path)))


def new_owned_dir(parent: os.PathLike[str] | str, prefix: str) -> np.OwnedEntry:
    parent_path = np.logical(parent)
    np.mkdir(parent_path, parents=True, exist_ok=True)
    entry = np.create_owned_dir(parent_path, prefix)
    _OWNED_ENTRIES[_owned_key(entry.path)] = entry
    return entry


def new_owned_file(
    parent: os.PathLike[str] | str, prefix: str, suffix: str = ""
) -> np.OwnedEntry:
    parent_path = np.logical(parent)
    np.mkdir(parent_path, parents=True, exist_ok=True)
    entry = np.create_owned_file(parent_path, prefix, suffix)
    _OWNED_ENTRIES[_owned_key(entry.path)] = entry
    return entry


def cleanup_owned(entry: np.OwnedEntry, *, warning: str | None = None) -> bool:
    try:
        np.remove_owned(entry)
        _OWNED_ENTRIES.pop(_owned_key(entry.path), None)
        return True
    except OSError as exc:
        if warning:
            print(f"Warning: {warning} {entry.path}: {exc}", file=sys.stderr)
        return False


def _remove_owned_path(path: Path) -> None:
    entry = _OWNED_ENTRIES.get(_owned_key(path))
    if entry is None:
        raise OSError(f"Refusing to remove an entry not owned by this invocation: {path}")
    np.remove_owned(entry)
    _OWNED_ENTRIES.pop(_owned_key(path), None)


def _recovery_error(target: Path, backup: np.OwnedEntry, detail: str) -> ConversionError:
    return ConversionError(
        f"{detail}; retained exact recovery backup at {backup.path} for target {target}"
    )


def _restore_old_target(
    target: Path,
    backup: np.OwnedEntry,
    payload: Path,
    old_identity: np.EntryIdentity,
) -> bool:
    if np.exists(target) or not old_identity.matches(payload):
        return False
    try:
        np.rename_no_replace(payload, target)
    except Exception:
        pass
    return old_identity.matches(target) and not np.exists(payload)


def _finish_backup_after_commit(target: Path, backup: np.OwnedEntry) -> None:
    try:
        _remove_owned_path(backup.path)
    except OSError as exc:
        print(
            f"Warning: published {target}, but could not remove backup {backup.path}: {exc}",
            file=sys.stderr,
        )


def publish_owned(
    stage: np.OwnedEntry,
    target: os.PathLike[str] | str,
    overwrite: bool,
    *,
    allow_stage_cleanup: bool = True,
    verify_payload: Callable[[Path], None] | None = None,
) -> None:
    """Publish one identity-pinned entry with the documented two-move replace.

    The overwrite transaction is deliberately not described as crash-atomic.
    Caught pre-commit failures restore only an identity-proven old target.
    When supplied, ``verify_payload`` runs immediately before and after the
    commit move; a caught post-commit failure rolls the owned entry back before
    restoring any displaced target.
    """
    target_path = np.logical(target)
    if not stage.matches(stage.path):
        raise ConversionError(f"Owned stage identity changed before publication: {stage.path}")
    old_identity = np.EntryIdentity.capture(target_path) if np.exists(target_path) else None
    if old_identity is not None and not overwrite:
        raise OutputCollision(f"Output already exists: {target_path}")

    if old_identity is None:
        if verify_payload is not None:
            verify_payload(stage.path)
        try:
            np.rename_no_replace(stage.path, target_path)
        except FileExistsError as exc:
            if not (stage.matches(target_path) and not np.exists(stage.path)):
                if allow_stage_cleanup and stage.matches(stage.path):
                    cleanup_owned(stage, warning="could not remove exact late-collision stage")
                raise OutputCollision(f"Output already exists: {target_path}") from exc
        except Exception:
            if not (stage.matches(target_path) and not np.exists(stage.path)):
                raise
        if not stage.matches(target_path) or np.exists(stage.path):
            raise ConversionError(f"Could not verify published target identity: {target_path}")
        if verify_payload is not None:
            try:
                verify_payload(target_path)
            except Exception as verification_error:
                try:
                    np.rename_no_replace(target_path, stage.path)
                except Exception as rollback_error:
                    raise ConversionError(
                        f"Published target payload verification failed and exact rollback failed; retained target at {target_path}"
                    ) from rollback_error
                if not stage.matches(stage.path) or np.exists(target_path):
                    raise ConversionError(
                        f"Published target payload verification failed and rollback identity was not verified; retained stage at {stage.path}"
                    ) from verification_error
                if allow_stage_cleanup:
                    cleanup_owned(stage, warning="could not remove failed payload stage")
                raise verification_error
        _OWNED_ENTRIES.pop(_owned_key(stage.path), None)
        return

    backup = new_owned_dir(target_path.parent, ".conversion-backup-")
    payload = backup.path / "original"
    try:
        try:
            np.rename_no_replace(target_path, payload)
        except Exception as move_error:
            if old_identity.matches(payload) and not np.exists(target_path):
                if _restore_old_target(target_path, backup, payload, old_identity):
                    cleanup_owned(backup, warning=f"restored {target_path}, but could not remove empty backup")
                    raise move_error
                raise _recovery_error(target_path, backup, "Old target move failed after displacement") from move_error
            if old_identity.matches(target_path) and not np.exists(payload):
                cleanup_owned(backup, warning=f"target {target_path} remained in place, but could not remove empty backup")
                raise move_error
            raise _recovery_error(target_path, backup, "Old target move entered an indeterminate state") from move_error

        if not old_identity.matches(payload) or np.exists(target_path):
            raise _recovery_error(target_path, backup, "Moved old target identity could not be verified")

        if verify_payload is not None:
            try:
                verify_payload(stage.path)
            except Exception as verification_error:
                if _restore_old_target(target_path, backup, payload, old_identity):
                    cleanup_owned(backup, warning=f"restored {target_path}, but could not remove empty backup")
                    if allow_stage_cleanup and stage.matches(stage.path):
                        cleanup_owned(stage, warning="could not remove failed payload stage")
                    raise verification_error
                raise _recovery_error(
                    target_path,
                    backup,
                    "Payload verification failed and exact restoration was not verified",
                ) from verification_error

        try:
            np.rename_no_replace(stage.path, target_path)
        except Exception as commit_error:
            if stage.matches(target_path) and not np.exists(stage.path):
                pass
            elif _restore_old_target(target_path, backup, payload, old_identity):
                cleanup_owned(backup, warning=f"restored {target_path}, but could not remove empty backup")
                if allow_stage_cleanup and stage.matches(stage.path):
                    cleanup_owned(stage, warning="could not remove exact failed publication stage")
                raise commit_error
            else:
                raise _recovery_error(
                    target_path,
                    backup,
                    "New target commit failed and exact restoration was not verified",
                ) from commit_error

        if not stage.matches(target_path) or np.exists(stage.path):
            raise _recovery_error(target_path, backup, "New target commit identity could not be verified")
        if verify_payload is not None:
            try:
                verify_payload(target_path)
            except Exception as verification_error:
                try:
                    np.rename_no_replace(target_path, stage.path)
                except Exception as rollback_error:
                    raise _recovery_error(
                        target_path,
                        backup,
                        "Committed payload verification failed and the new target could not be rolled back",
                    ) from rollback_error
                if not stage.matches(stage.path) or np.exists(target_path):
                    raise _recovery_error(
                        target_path,
                        backup,
                        "Committed payload verification failed and rollback identity was not verified",
                    ) from verification_error
                if _restore_old_target(target_path, backup, payload, old_identity):
                    cleanup_owned(backup, warning=f"restored {target_path}, but could not remove empty backup")
                    if allow_stage_cleanup and stage.matches(stage.path):
                        cleanup_owned(stage, warning="could not remove failed payload stage")
                    raise verification_error
                raise _recovery_error(
                    target_path,
                    backup,
                    "Committed payload verification failed and exact restoration was not verified",
                ) from verification_error
        _OWNED_ENTRIES.pop(_owned_key(stage.path), None)
        if not old_identity.matches(payload):
            raise _recovery_error(target_path, backup, "Committed target has an unverifiable recovery payload")
        _finish_backup_after_commit(target_path, backup)
    except Exception:
        raise


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
