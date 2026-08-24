#!/usr/bin/env python3
"""Memory-bounded subprocess worker for structural PDF validation."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import BinaryIO


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _open_locked(path: Path) -> BinaryIO:
    """Open one identity-pinned stream while denying writes and replacement."""
    if os.name != "nt":
        return path.open("rb")

    import msvcrt
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
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ: deny write and delete sharing
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb", closefd=True)


def _sha256(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


def _decode_page_contents(page, page_number: int) -> None:
    """Resolve and fully decode every content stream present on one page."""
    from pypdf.generic import ArrayObject, NullObject, StreamObject

    value = page.get("/Contents")
    if value is None:
        return
    try:
        resolved = value.get_object()
        if isinstance(resolved, NullObject):
            return
        streams = resolved if isinstance(resolved, ArrayObject) else [resolved]
        for stream_number, candidate in enumerate(streams, start=1):
            stream = candidate.get_object()
            if not isinstance(stream, StreamObject):
                raise TypeError(
                    f"content entry {stream_number} resolved to {type(stream).__name__}, not a stream"
                )
            decoded = stream.get_data()
            if not isinstance(decoded, bytes):
                raise TypeError(
                    f"content stream {stream_number} decoded to {type(decoded).__name__}, not bytes"
                )
    except Exception as exc:
        detail = str(exc).replace("\r", " ").replace("\n", " ")[:500]
        raise ValueError(
            f"PDF page {page_number} content stream is unreadable: {type(exc).__name__}: {detail}"
        ) from exc


def validate(path: Path, max_bytes: int) -> dict[str, object]:
    named = path.lstat()
    if not stat.S_ISREG(named.st_mode) or bool(
        getattr(named, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError("PDF is not an ordinary regular file")
    with _open_locked(path) as stream:
        before = os.fstat(stream.fileno())
        if _identity(before) != _identity(named):
            raise ValueError("PDF path identity changed before validation")
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("PDF is not an ordinary regular file")
        if before.st_size > max_bytes:
            raise ValueError(f"PDF exceeds validation byte limit ({max_bytes})")

        digest = _sha256(stream)
        if not stream.read(8).startswith(b"%PDF-"):
            raise ValueError("PDF header is missing or invalid")
        if before.st_size:
            stream.seek(max(0, before.st_size - 4096))
            tail = stream.read()
            if b"%%EOF" not in tail:
                raise ValueError("PDF trailer EOF marker is missing")

        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("pypdf is required for PDF validation") from exc
        try:
            stream.seek(0)
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted:
                raise ValueError("Encrypted or password-protected PDFs are not supported")
            root = reader.trailer.get("/Root")
            if root is None or root.get_object().get("/Pages") is None:
                raise ValueError("PDF trailer or page tree is unreadable")
            pages = len(reader.pages)
            if pages < 1:
                raise ValueError("PDF must contain at least one page")
            for page_number, page in enumerate(reader.pages, start=1):
                _ = page.mediabox
                _decode_page_contents(page, page_number)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                f"PDF structure is unreadable: {type(exc).__name__}: {exc}"
            ) from exc

        after = os.fstat(stream.fileno())
        if _identity(after) != _identity(before):
            raise ValueError("PDF identity changed during validation")
        if _sha256(stream) != digest:
            raise ValueError("PDF bytes changed during validation")
        final_named = path.lstat()
        if _identity(final_named) != _identity(before):
            raise ValueError("PDF path identity changed during validation")
        return {"sha256": digest, "size_bytes": before.st_size, "pages": pages}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--max-bytes", required=True, type=int)
    args = parser.parse_args()
    try:
        result = validate(Path(args.input), args.max_bytes)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
