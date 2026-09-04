"""Deterministic LibreOffice-to-PDF provider used by local conversion skills."""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from conversion_runtime import ConversionError, SourceSnapshot, copy_from_snapshot
import native_paths as np


PDF_SUFFIX = ".pdf"
WRITER_SUFFIXES = frozenset({".doc", ".docx", ".docm"})
IMPRESS_SUFFIXES = frozenset({".ppt", ".pptx", ".pptm", ".pps", ".ppsx"})
CALC_SUFFIXES = frozenset({".xls", ".xlsx", ".xlsm", ".xlsb"})
OFFICE_SUFFIXES = WRITER_SUFFIXES | IMPRESS_SUFFIXES | CALC_SUFFIXES
SUPPORTED_SUFFIXES = frozenset({PDF_SUFFIX}) | OFFICE_SUFFIXES
TEMPLATE_SUFFIXES = frozenset(
    {".dot", ".dotx", ".dotm", ".pot", ".potx", ".potm", ".xlt", ".xltx", ".xltm"}
)
EXPLICITLY_UNSUPPORTED_SUFFIXES = TEMPLATE_SUFFIXES | {".ppsm"}

DEFAULT_PDF_CONVERSION: dict[str, object] = {
    "libreoffice_path": "",
    "timeout_seconds": 1000,
    "validation": {
        "max_pdf_bytes": 1024 * 1024 * 1024,
        "timeout_seconds": 60,
        "job_memory_bytes": 2 * 1024 * 1024 * 1024,
    },
}

_VERSION_RE = re.compile(r"(?:^|\s)LibreOffice\s+(\d+(?:\.\d+)+)(?:\s|$)")
_PE_AMD64 = 0x8664
_DIAGNOSTIC_LIMIT = 64 * 1024
_VALIDATOR = Path(__file__).with_name("pdf_validation_worker.py")


class LibreOfficeError(ConversionError):
    pass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True)
class ValidationSettings:
    max_pdf_bytes: int = 1024 * 1024 * 1024
    timeout_seconds: float = 60.0
    job_memory_bytes: int = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class PdfConversionSettings:
    libreoffice_path: str = ""
    timeout_seconds: float = 1000.0
    validation: ValidationSettings = ValidationSettings()

    @classmethod
    def from_config(cls, config: Mapping[str, object], cli_path: str = "") -> "PdfConversionSettings":
        raw = config.get("pdf_conversion", {})
        if not isinstance(raw, Mapping):
            raise LibreOfficeError("config pdf_conversion must be an object")
        timeout = raw.get("timeout_seconds", 1000)
        validation = raw.get("validation", {})
        if not isinstance(validation, Mapping):
            raise LibreOfficeError("config pdf_conversion.validation must be an object")
        try:
            timeout_value = float(timeout)
            max_bytes = int(validation.get("max_pdf_bytes", 1024 * 1024 * 1024))
            validation_timeout = float(validation.get("timeout_seconds", 60))
            memory = int(validation.get("job_memory_bytes", 2 * 1024 * 1024 * 1024))
        except (TypeError, ValueError) as exc:
            raise LibreOfficeError("PDF conversion numeric configuration is invalid") from exc
        if timeout_value <= 0 or max_bytes <= 0 or validation_timeout <= 0 or memory <= 0:
            raise LibreOfficeError("PDF conversion limits must be positive")
        path = cli_path if cli_path else str(raw.get("libreoffice_path") or "")
        return cls(path, timeout_value, ValidationSettings(max_bytes, validation_timeout, memory))


def merge_pdf_conversion_config(config: Mapping[str, object]) -> dict[str, object]:
    result = dict(config)
    raw = config.get("pdf_conversion", {})
    if not isinstance(raw, Mapping):
        raise LibreOfficeError("config pdf_conversion must be an object")
    merged = dict(DEFAULT_PDF_CONVERSION)
    merged.update(raw)
    raw_validation = raw.get("validation", {})
    if not isinstance(raw_validation, Mapping):
        raise LibreOfficeError("config pdf_conversion.validation must be an object")
    merged["validation"] = {
        **DEFAULT_PDF_CONVERSION["validation"],  # type: ignore[arg-type]
        **raw_validation,
    }
    result["pdf_conversion"] = merged
    return result


def classify_suffix(path: os.PathLike[str] | str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == PDF_SUFFIX:
        return "pdf"
    if suffix in WRITER_SUFFIXES:
        return "writer"
    if suffix in IMPRESS_SUFFIXES:
        return "impress"
    if suffix in CALC_SUFFIXES:
        return "calc"
    if suffix in EXPLICITLY_UNSUPPORTED_SUFFIXES:
        raise LibreOfficeError(f"Template or unsupported presentation format is not supported: {suffix}")
    raise LibreOfficeError(f"Unsupported local file type for PDF conversion: {suffix or '<none>'}")


def _is_x64_pe(path: Path) -> bool:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            return False
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                return False
            stream.seek(0x3C)
            offset_raw = stream.read(4)
            if len(offset_raw) != 4:
                return False
            pe_offset = struct.unpack("<I", offset_raw)[0]
            if pe_offset > 64 * 1024 * 1024:
                return False
            stream.seek(pe_offset)
            if stream.read(4) != b"PE\0\0":
                return False
            machine_raw = stream.read(2)
            return len(machine_raw) == 2 and struct.unpack("<H", machine_raw)[0] == _PE_AMD64
    except OSError:
        return False


def _normalize_candidate(value: str) -> Path:
    candidate = Path(os.path.abspath(os.path.expandvars(value.strip().strip('"'))))
    if candidate.is_dir():
        candidate = candidate / "soffice.com"
    elif candidate.suffix.lower() == ".exe":
        candidate = candidate.with_suffix(".com")
    elif candidate.suffix.lower() != ".com":
        raise LibreOfficeError("LibreOffice path must be a directory, soffice.com, or soffice.exe")
    return candidate


def _bounded_reader(pipe, limit: int, destination: list[bytes], truncated: list[bool]) -> None:
    kept = bytearray()
    while True:
        chunk = pipe.read(8192)
        if not chunk:
            break
        remaining = limit - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated[0] = True
    destination.append(bytes(kept))


if os.name == "nt":
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _job_api():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32


def _run_job_process(
    argv: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
    memory_limit: int | None = None,
    diagnostic_limit: int = _DIAGNOSTIC_LIMIT,
) -> ProcessResult:
    """Run without a shell, attach a kill-on-close Job, and bound diagnostics."""
    job = None
    kernel32 = None
    creationflags = 0
    if os.name == "nt":
        kernel32 = _job_api()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        if memory_limit is not None:
            limits.BasicLimitInformation.LimitFlags |= 0x00000200  # JOB_MEMORY
            limits.JobMemoryLimit = int(memory_limit)
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(job)
            raise error
        # CREATE_SUSPENDED closes the launch-before-assignment escape window:
        # no provider code can run or spawn a child before Job containment.
        creationflags = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED

    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
        )
    except Exception:
        if job is not None:
            assert kernel32 is not None
            kernel32.CloseHandle(job)
        raise

    if os.name == "nt":
        assert kernel32 is not None and job is not None
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
            error = ctypes.WinError(ctypes.get_last_error())
            kernel32.CloseHandle(job)
            job = None
            process.kill()
            process.wait(timeout=10)
            raise error
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = int(ntdll.NtResumeProcess(wintypes.HANDLE(process._handle)))
        if status != 0:
            kernel32.CloseHandle(job)
            job = None
            process.wait(timeout=10)
            raise LibreOfficeError(
                f"Could not resume contained process (NTSTATUS 0x{status & 0xFFFFFFFF:08x})"
            )

    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []
    stdout_truncated = [False]
    stderr_truncated = [False]
    threads = [
        threading.Thread(target=_bounded_reader, args=(process.stdout, diagnostic_limit, stdout_parts, stdout_truncated), daemon=True),
        threading.Thread(target=_bounded_reader, args=(process.stderr, diagnostic_limit, stderr_parts, stderr_truncated), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if job is not None:
                assert kernel32 is not None
                kernel32.CloseHandle(job)
                job = None
            else:
                process.kill()
            process.wait(timeout=10)
            raise LibreOfficeError(f"Process exceeded {timeout:g} seconds") from exc
    finally:
        for thread in threads:
            thread.join(timeout=10)
        if job is not None:
            assert kernel32 is not None
            kernel32.CloseHandle(job)
    return ProcessResult(
        returncode,
        stdout_parts[0] if stdout_parts else b"",
        stderr_parts[0] if stderr_parts else b"",
        stdout_truncated[0],
        stderr_truncated[0],
    )


def _probe_candidate(path: Path) -> str | None:
    if path.name.lower() != "soffice.com" or not _is_x64_pe(path):
        return None
    environment = dict(os.environ)
    try:
        result = _run_job_process(
            [str(path), "--version"],
            cwd=path.parent,
            environment=environment,
            timeout=10.0,
        )
    except (OSError, LibreOfficeError):
        return None
    text = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")
    match = _VERSION_RE.search(text)
    if result.returncode != 0 or match is None:
        return None
    return match.group(1)


def resolve_libreoffice(cli_path: str, config_path: str) -> tuple[Path, str]:
    authoritative = cli_path or config_path
    if authoritative:
        origin = "--libreoffice-path" if cli_path else "config pdf_conversion.libreoffice_path"
        try:
            candidate = _normalize_candidate(authoritative)
        except LibreOfficeError as exc:
            raise LibreOfficeError(
                f"Invalid authoritative LibreOffice executable from {origin}: {exc}"
            ) from exc
        version = _probe_candidate(candidate)
        if version is None:
            raise LibreOfficeError(
                f"Invalid authoritative LibreOffice executable from {origin}: expected regular x64 soffice.com with a valid --version response"
            )
        return candidate, version

    candidates: list[Path] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "LibreOffice" / "program" / "soffice.com")
    for name in ("soffice.com", "soffice.exe"):
        found = shutil.which(name)
        if found:
            try:
                candidates.append(_normalize_candidate(found))
            except LibreOfficeError:
                pass
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        version = _probe_candidate(candidate)
        if version is not None:
            return candidate, version
    raise LibreOfficeError(
        "LibreOffice was not found; install x64 LibreOffice or pass --libreoffice-path to its program directory/soffice.com"
    )


def _property(property_type: str, value: object) -> dict[str, str]:
    if property_type == "boolean":
        rendered = "true" if bool(value) else "false"
    else:
        rendered = str(value)
    return {"type": property_type, "value": rendered}


def filter_properties(family: str) -> dict[str, dict[str, str]]:
    properties = {
        "EmbedStandardFonts": _property("boolean", True),
        "ExportBookmarks": _property("boolean", True),
        "ExportFormFields": _property("boolean", False),
        "ExportNotes": _property("boolean", False),
        "IsAddStream": _property("boolean", False),
        "Quality": _property("long", 100),
        "ReduceImageResolution": _property("boolean", False),
        "SelectPdfVersion": _property("long", 17),
        "UseLosslessCompression": _property("boolean", True),
        "UseTaggedPDF": _property("boolean", True),
    }
    additions = {
        "writer": {
            "ExportNotesInMargin": _property("boolean", False),
            "IsSkipEmptyPages": _property("boolean", False),
        },
        "impress": {
            "ExportHiddenSlides": _property("boolean", False),
            "ExportNotesPages": _property("boolean", False),
            "ExportOnlyNotesPages": _property("boolean", False),
            "UseTransitionEffects": _property("boolean", False),
        },
        "calc": {"SinglePageSheets": _property("boolean", False)},
    }
    if family not in additions:
        raise LibreOfficeError(f"Unsupported LibreOffice document family: {family}")
    properties.update(additions[family])
    return properties


def filter_argument(family: str) -> str:
    filters = {
        "writer": "writer_pdf_Export",
        "impress": "impress_pdf_Export",
        "calc": "calc_pdf_Export",
    }
    payload = json.dumps(
        filter_properties(family), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return f"pdf:{filters[family]}:{payload}"


_REGISTRY = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
 <item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop><prop oor:name="SecureURL" oor:op="fuse"><value/></prop></item>
 <item oor:path="/org.openoffice.Office.Jobs/Jobs/org.openoffice.Office.Jobs:Job['UpdateCheck']/Arguments"><prop oor:name="AutoCheckEnabled" oor:op="fuse"><value>false</value></prop></item>
 <item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
 <item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
</oor:items>
"""


def seed_profile(profile: Path) -> None:
    user = profile / "user"
    user.mkdir(parents=True, exist_ok=False)
    registry = user / "registrymodifications.xcu"
    registry.write_text(_REGISTRY, encoding="utf-8", newline="\n")


def validate_pdf(path: Path, settings: ValidationSettings) -> dict[str, object]:
    info = np.lstat(path)
    if info.st_size > settings.max_pdf_bytes:
        raise LibreOfficeError(f"PDF exceeds validation byte limit ({settings.max_pdf_bytes})")
    environment = dict(os.environ)
    result = _run_job_process(
        [
            sys.executable,
            "-I",
            str(_VALIDATOR),
            "--input",
            np.native(path),
            "--max-bytes",
            str(settings.max_pdf_bytes),
        ],
        cwd=_VALIDATOR.parent,
        environment=environment,
        timeout=settings.timeout_seconds,
        memory_limit=settings.job_memory_bytes,
    )
    raw = result.stdout.decode("utf-8", "replace").strip()
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LibreOfficeError(
            f"PDF validation worker returned invalid diagnostics (exit {result.returncode})"
        ) from exc
    if result.returncode != 0 or envelope.get("ok") is not True:
        raise LibreOfficeError(str(envelope.get("error") or "PDF validation failed"))
    value = envelope.get("result")
    if not isinstance(value, dict):
        raise LibreOfficeError("PDF validation worker returned an invalid result")
    return value


def verify_validated_pdf(
    path: Path,
    settings: ValidationSettings,
    expected: Mapping[str, object],
) -> None:
    """Revalidate a staged/published PDF against its recorded exact result."""
    current = validate_pdf(path, settings)
    for key in ("sha256", "size_bytes", "pages"):
        if current.get(key) != expected.get(key):
            raise LibreOfficeError(
                f"PDF artifact changed after validation ({key}: {expected.get(key)!r} -> {current.get(key)!r})"
            )


class LibreOfficePdfEngine:
    def __init__(self, settings: PdfConversionSettings, cli_path: str = ""):
        self.settings = settings
        self._cli_path = cli_path
        self._resolved: tuple[Path, str] | None = None

    @property
    def executable(self) -> Path:
        if self._resolved is None:
            self._resolved = resolve_libreoffice(self._cli_path, self.settings.libreoffice_path)
        return self._resolved[0]

    @property
    def version(self) -> str:
        _ = self.executable
        assert self._resolved is not None
        return self._resolved[1]

    def with_cli_path(self, cli_path: str) -> "LibreOfficePdfEngine":
        return LibreOfficePdfEngine(self.settings, cli_path)

    def convert(self, snapshot: SourceSnapshot, destination: Path, workspace: Path) -> dict[str, object]:
        family = classify_suffix(snapshot.original_name)
        destination = Path(destination)
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=False)
        if family == "pdf":
            copy_from_snapshot(snapshot, destination)
            return validate_pdf(destination, self.settings.validation)

        work = workspace / "work"
        output = workspace / "output"
        profile = workspace / "profile"
        temporary = workspace / "temp"
        for directory in (work, output, profile, temporary):
            directory.mkdir()
        seed_profile(profile)
        work_copy = copy_from_snapshot(snapshot, work / snapshot.original_name)
        environment = dict(os.environ)
        environment.update({"TEMP": str(temporary), "TMP": str(temporary)})
        argv = [
            str(self.executable),
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--convert-to",
            filter_argument(family),
            "--outdir",
            str(output),
            str(work_copy),
        ]
        result = _run_job_process(
            argv,
            cwd=work,
            environment=environment,
            timeout=self.settings.timeout_seconds,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
            raise LibreOfficeError(
                f"LibreOffice conversion failed with exit {result.returncode}: {detail[:1000]}"
            )
        entries = list(output.iterdir())
        expected = output / f"{Path(snapshot.original_name).stem}.pdf"
        if len(entries) != 1 or entries[0].name.lower() != expected.name.lower():
            raise LibreOfficeError("LibreOffice did not produce exactly one expected PDF")
        produced = entries[0]
        produced_info = produced.lstat()
        if not stat.S_ISREG(produced_info.st_mode) or bool(
            getattr(produced_info, "st_file_attributes", 0) & 0x400
        ):
            raise LibreOfficeError("LibreOffice output is not an ordinary regular PDF")
        validated = validate_pdf(produced, self.settings.validation)
        destination.parent.mkdir(parents=True, exist_ok=True)
        np.copy_file(produced, destination)
        copied = validate_pdf(destination, self.settings.validation)
        if validated.get("sha256") != copied.get("sha256"):
            raise LibreOfficeError("Copied LibreOffice PDF hash mismatch")
        snapshot.verify()
        return copied
