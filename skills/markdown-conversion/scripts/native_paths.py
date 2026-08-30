"""Long-path-aware, no-follow filesystem primitives for publication.

The public pipeline keeps ordinary absolute paths as logical identities.  On
Windows only, this module adds an extended-length prefix at the last possible
moment for an operating-system call.  Callers must never persist ``native()``
results in canonical output.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
import stat as stat_module
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, TextIO


CREATE_ATTEMPTS = 32
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class UnsafeContainmentError(OSError):
    """A purported bundle child cannot be proved no-follow contained."""


def _strip_extended(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def logical(path: os.PathLike[str] | str) -> Path:
    """Return a normalized absolute path without a Windows native prefix."""
    value = _strip_extended(os.fspath(path))
    return Path(os.path.normpath(os.path.abspath(value)))


def native(path: os.PathLike[str] | str) -> str:
    """Return an operational path, extended-length on Windows."""
    value = str(logical(path))
    if os.name != "nt":
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def display(path: os.PathLike[str] | str) -> str:
    return str(logical(path))


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
    )


def is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def lstat(path: os.PathLike[str] | str) -> os.stat_result:
    return os.lstat(native(path))


def stat(path: os.PathLike[str] | str) -> os.stat_result:
    return os.stat(native(path))


def exists(path: os.PathLike[str] | str) -> bool:
    try:
        lstat(path)
        return True
    except FileNotFoundError:
        return False


def is_file(path: os.PathLike[str] | str) -> bool:
    try:
        info = lstat(path)
    except FileNotFoundError:
        return False
    return stat_module.S_ISREG(info.st_mode) and not is_reparse(info)


def is_dir(path: os.PathLike[str] | str) -> bool:
    try:
        info = lstat(path)
    except FileNotFoundError:
        return False
    return stat_module.S_ISDIR(info.st_mode) and not is_reparse(info)


def open_file(
    path: os.PathLike[str] | str,
    mode: str = "r",
    *,
    encoding: str | None = None,
    newline: str | None = None,
) -> BinaryIO | TextIO:
    return open(native(path), mode, encoding=encoding, newline=newline)


def read_bytes(path: os.PathLike[str] | str) -> bytes:
    with open_file(path, "rb") as stream:
        return stream.read()


def write_bytes(path: os.PathLike[str] | str, value: bytes) -> None:
    with open_file(path, "wb") as stream:
        stream.write(value)


def read_text(path: os.PathLike[str] | str, encoding: str = "utf-8") -> str:
    with open_file(path, "r", encoding=encoding) as stream:
        return stream.read()


def write_text(
    path: os.PathLike[str] | str,
    value: str,
    encoding: str = "utf-8",
    newline: str | None = None,
) -> None:
    with open_file(path, "w", encoding=encoding, newline=newline) as stream:
        stream.write(value)


def mkdir(path: os.PathLike[str] | str, *, parents: bool = False, exist_ok: bool = False) -> None:
    value = logical(path)
    if not parents:
        os.mkdir(native(value))
        return
    missing: list[Path] = []
    cursor = value
    while not exists(cursor):
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if exists(cursor) and not is_dir(cursor):
        raise NotADirectoryError(display(cursor))
    for item in reversed(missing):
        try:
            os.mkdir(native(item))
        except FileExistsError:
            if not is_dir(item):
                raise
    if not missing and not exist_ok:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), display(value))


def scandir(path: os.PathLike[str] | str):
    return os.scandir(native(path))


def unlink(path: os.PathLike[str] | str) -> None:
    os.unlink(native(path))


def rmdir(path: os.PathLike[str] | str) -> None:
    os.rmdir(native(path))


def sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with open_file(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_file(source: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
    """Stream-copy one regular source without following a destination entry."""
    source_info = lstat(source)
    if not stat_module.S_ISREG(source_info.st_mode) or is_reparse(source_info):
        raise OSError(f"Source is not an ordinary regular file: {display(source)}")
    if exists(destination):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), display(destination))
    shutil.copyfile(native(source), native(destination))


def paths_equal(left: os.PathLike[str] | str, right: os.PathLike[str] | str) -> bool:
    return os.path.normcase(str(logical(left))) == os.path.normcase(str(logical(right)))


def is_within(child: os.PathLike[str] | str, parent: os.PathLike[str] | str) -> bool:
    child_value = os.path.normcase(str(logical(child)))
    parent_value = os.path.normcase(str(logical(parent)))
    try:
        return os.path.commonpath([child_value, parent_value]) == parent_value
    except ValueError:
        return False


def verified_bundle_file(
    bundle_root: os.PathLike[str] | str,
    child: os.PathLike[str] | str,
) -> Path:
    """Prove ``child`` is a regular file beneath one ordinary bundle root.

    The check is component-by-component with ``lstat``.  It rejects symlinks
    and Windows reparse points at the root, every ancestor, and the leaf, then
    rechecks the captured identities before returning.  This detects unsafe or
    changed paths without claiming general TOCTOU protection.
    """
    root = logical(bundle_root)
    leaf = logical(child)
    if not is_within(leaf, root) or paths_equal(leaf, root):
        raise UnsafeContainmentError(
            f"Bundle child escapes the bundle root: {display(leaf)}"
        )

    root_info = lstat(root)
    if (
        not stat_module.S_ISDIR(root_info.st_mode)
        or stat_module.S_ISLNK(root_info.st_mode)
        or is_reparse(root_info)
    ):
        raise UnsafeContainmentError(
            f"Bundle root is not an ordinary directory: {display(root)}"
        )

    relative = os.path.relpath(str(leaf), str(root))
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise UnsafeContainmentError(
            f"Bundle child has unsafe components: {display(leaf)}"
        )

    identities: list[tuple[Path, os.stat_result]] = [(root, root_info)]
    cursor = root
    for index, part in enumerate(parts):
        cursor = cursor / part
        info = lstat(cursor)
        if stat_module.S_ISLNK(info.st_mode) or is_reparse(info):
            raise UnsafeContainmentError(
                f"Bundle child traverses a link or reparse point: {display(cursor)}"
            )
        is_leaf = index == len(parts) - 1
        if is_leaf:
            if not stat_module.S_ISREG(info.st_mode):
                raise UnsafeContainmentError(
                    f"Bundle child is not an ordinary regular file: {display(cursor)}"
                )
        elif not stat_module.S_ISDIR(info.st_mode):
            raise UnsafeContainmentError(
                f"Bundle child ancestor is not an ordinary directory: {display(cursor)}"
            )
        identities.append((cursor, info))

    for path, expected in identities:
        try:
            current = lstat(path)
        except FileNotFoundError as exc:
            raise UnsafeContainmentError(
                f"Bundle path changed during containment validation: {display(path)}"
            ) from exc
        if not _same_identity(expected, current):
            raise UnsafeContainmentError(
                f"Bundle path identity changed during containment validation: {display(path)}"
            )
    return leaf


def _rename_no_replace_windows(source: Path, destination: Path) -> None:
    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move.restype = ctypes.c_int
    if not move(native(source), native(destination), 0):
        code = ctypes.get_last_error()
        if code in {80, 183}:
            raise FileExistsError(errno.EEXIST, "destination already exists", display(destination))
        raise ctypes.WinError(code)


def _rename_no_replace_posix(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, source_bytes, -100, destination_bytes, 1) == 0:
            return
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        renamex_np = libc.renamex_np
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        if renamex_np(source_bytes, destination_bytes, 0x00000004) == 0:
            return
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), display(destination))
    raise OSError(code, os.strerror(code), display(destination))


def rename_no_replace(source: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
    """Atomically rename a file or directory only when destination is absent."""
    source_path = logical(source)
    destination_path = logical(destination)
    if os.name == "nt":
        _rename_no_replace_windows(source_path, destination_path)
    else:
        _rename_no_replace_posix(source_path, destination_path)


def replace(source: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
    """Perform one operating-system replacement operation."""
    os.replace(native(source), native(destination))


@dataclass(frozen=True)
class EntryIdentity:
    path: Path
    st_dev: int
    st_ino: int
    st_mode: int

    @classmethod
    def capture(cls, path: os.PathLike[str] | str) -> "EntryIdentity":
        value = logical(path)
        info = lstat(value)
        return cls(value, info.st_dev, info.st_ino, info.st_mode)

    def matches(self, path: os.PathLike[str] | str | None = None) -> bool:
        value = logical(path) if path is not None else self.path
        try:
            info = lstat(value)
        except FileNotFoundError:
            return False
        return (
            info.st_dev == self.st_dev
            and info.st_ino == self.st_ino
            and info.st_mode == self.st_mode
        )


@dataclass(frozen=True)
class OwnedEntry(EntryIdentity):
    """Identity of an entry created exclusively by this invocation."""


def _owned(path: Path) -> OwnedEntry:
    info = lstat(path)
    return OwnedEntry(path, info.st_dev, info.st_ino, info.st_mode)


def create_owned_dir(parent: os.PathLike[str] | str, prefix: str) -> OwnedEntry:
    parent_path = logical(parent)
    for _ in range(CREATE_ATTEMPTS):
        candidate = parent_path / f"{prefix}{uuid.uuid4().hex}"
        try:
            mkdir(candidate)
        except FileExistsError:
            continue
        return _owned(candidate)
    raise RuntimeError(f"Could not create owned {prefix} entry after {CREATE_ATTEMPTS} attempts")


def create_owned_file(parent: os.PathLike[str] | str, prefix: str, suffix: str = "") -> OwnedEntry:
    parent_path = logical(parent)
    for _ in range(CREATE_ATTEMPTS):
        candidate = parent_path / f"{prefix}{uuid.uuid4().hex}{suffix}"
        try:
            with open_file(candidate, "xb"):
                pass
        except FileExistsError:
            continue
        return _owned(candidate)
    raise RuntimeError(f"Could not create owned {prefix} entry after {CREATE_ATTEMPTS} attempts")


def _verify(info: os.stat_result, path: Path) -> None:
    current = lstat(path)
    if not _same_identity(info, current):
        raise OSError(f"Entry identity changed during exact cleanup: {display(path)}")


def _inventory_tree_exact(root: Path, root_info: os.stat_result) -> list[tuple[Path, os.stat_result]]:
    if is_reparse(root_info) or stat_module.S_ISLNK(root_info.st_mode) or stat_module.S_ISREG(root_info.st_mode):
        return [(root, root_info)]
    if not stat_module.S_ISDIR(root_info.st_mode):
        raise OSError(f"Unexpected entry type during exact cleanup: {display(root)}")
    _verify(root_info, root)
    with scandir(root) as entries:
        children = sorted((entry.name, lstat(root / entry.name)) for entry in entries)
    result: list[tuple[Path, os.stat_result]] = []
    for name, child_info in children:
        result.extend(_inventory_tree_exact(root / name, child_info))
    result.append((root, root_info))
    return result


def _remove_tree_exact(root: Path, root_info: os.stat_result) -> None:
    inventory = _inventory_tree_exact(root, root_info)
    # Reject every unexpected initial state before the first unlink.  Each
    # entry is checked again immediately before removal; this is detection, not
    # a claim of general TOCTOU protection.
    for path, info in inventory:
        _verify(info, path)
        if is_reparse(info) or stat_module.S_ISLNK(info.st_mode):
            if stat_module.S_ISDIR(info.st_mode):
                rmdir(path)
            else:
                unlink(path)
        elif stat_module.S_ISREG(info.st_mode):
            unlink(path)
        elif stat_module.S_ISDIR(info.st_mode):
            rmdir(path)
        else:  # guarded during inventory; retained for defensive clarity
            raise OSError(f"Unexpected entry type during exact cleanup: {display(path)}")


def remove_owned(entry: OwnedEntry) -> None:
    """Remove only the identity-pinned entry, never a prefix or parent sweep."""
    try:
        root_info = lstat(entry.path)
    except FileNotFoundError:
        return
    if not (
        root_info.st_dev == entry.st_dev
        and root_info.st_ino == entry.st_ino
        and root_info.st_mode == entry.st_mode
    ):
        raise OSError(f"Owned entry identity changed; retained: {display(entry.path)}")
    _remove_tree_exact(entry.path, root_info)


def walk_files(root: os.PathLike[str] | str, recursive: bool) -> Iterator[Path]:
    root_path = logical(root)
    with scandir(root_path) as entries:
        children = sorted((entry.name, lstat(root_path / entry.name)) for entry in entries)
    for name, info in children:
        path = root_path / name
        if stat_module.S_ISREG(info.st_mode) and not is_reparse(info):
            yield path
        elif recursive and stat_module.S_ISDIR(info.st_mode) and not is_reparse(info):
            yield from walk_files(path, True)
