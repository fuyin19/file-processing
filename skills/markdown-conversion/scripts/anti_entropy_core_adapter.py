"""Standard-library JSONL client; generated skill copies are byte-identical.

Maintain this file in skills/_shared/scripts and run tools/sync_core_clients.py.
The active pipeline supplies its own skill boundary; direct clients require an
explicit runner. Core implementation is never imported in this process.
"""
from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Any


RUNNER_ENV = "ANTI_ENTROPY_CORE_RUNNER"
EXPECTED_ABI = "anti-entropy-core.runner/v1"
EXPECTED_CORE_VERSION = "1.2.1"
PREFLIGHT_TIMEOUT_SECONDS = 30
_RESULT_FIELDS = ("abi", "status", "exit_code", "command", "data", "issues")


class CoreAdapterError(RuntimeError):
    """The explicit Core runner could not satisfy its JSONL protocol."""

    def __init__(self, message: str, *, actual_abi: Any = "unknown", actual_version: Any = "unknown"):
        super().__init__(message)
        self.actual_abi = actual_abi
        self.actual_version = actual_version


@dataclass(frozen=True)
class CoreResult:
    abi: Any
    status: str
    exit_code: int
    command: str
    data: dict[str, Any]
    issues: list[Any]


def _absolute_path(path: os.PathLike[str] | str, *, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise CoreAdapterError(f"{label} must be absolute: {candidate}")
    return candidate.resolve(strict=False)


def _configuration_error(
    message: str, *, path: object = "unknown", actual_abi: Any = "unknown", actual_version: Any = "unknown"
) -> CoreAdapterError:
    return CoreAdapterError(
        f"{message}; selected runner={path!s}; actual ABI={actual_abi!r}, "
        f"actual Core version={actual_version!r}; expected ABI={EXPECTED_ABI!r}, "
        f"Core version={EXPECTED_CORE_VERSION!r}. Install/update the matching "
        "anti-entropy-core skill and consumer release, or correct "
        f"{RUNNER_ENV}; no fallback or automatic update is performed.",
        actual_abi=actual_abi, actual_version=actual_version,
    )


def _ordinary_path(path: Path, *, directory: bool = False) -> None:
    """Check every component without resolving away links or reparse points."""
    for current in (*reversed(path.parents), path):
        try:
            info = current.lstat()
        except OSError as exc:
            raise _configuration_error(
                f"Core path is missing or inaccessible: {current}: {exc}", path=path
            ) from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _configuration_error(f"Core path must be ordinary, not a link/reparse point: {current}", path=path)
        expected_directory = current != path or directory
        valid = stat.S_ISDIR(info.st_mode) if expected_directory else stat.S_ISREG(info.st_mode)
        if not valid:
            expected = "directory" if expected_directory else "file"
            raise _configuration_error(f"Core path must be an ordinary {expected}: {current}", path=path)


def _runner_path(
    runner: os.PathLike[str] | str | None = None,
    *,
    skill_entrypoint: os.PathLike[str] | str | None = None,
    skill_id: str | None = None,
) -> Path:
    if runner is not None:
        selected = os.fspath(runner)
    elif RUNNER_ENV in os.environ:
        selected = os.environ[RUNNER_ENV]
    else:
        if skill_entrypoint is None or skill_id is None:
            raise _configuration_error(f"{RUNNER_ENV} must name an absolute Core runner script")
        entrypoint = Path(skill_entrypoint)
        if not entrypoint.is_absolute():
            raise _configuration_error("Consumer skill entrypoint must be absolute", path=entrypoint)
        _ordinary_path(entrypoint)
        boundary = next((parent for parent in entrypoint.parents if parent.name == skill_id), None)
        if boundary is None:
            raise _configuration_error(f"Consumer skill boundary {skill_id!r} is absent", path=entrypoint)
        _ordinary_path(boundary / "SKILL.md")
        core_skill = boundary.parent / "anti-entropy-core"
        _ordinary_path(core_skill / "SKILL.md")
        selected = str(core_skill / "scripts" / "knowledge_unit_runner.py")
    candidate = Path(selected)
    if not selected or not candidate.is_absolute():
        raise _configuration_error(f"{RUNNER_ENV} must be absolute and nonempty; actual={selected!r}", path=selected)
    _ordinary_path(candidate)
    return candidate


def _result_from_output(
    command: str,
    completed: subprocess.CompletedProcess[str],
) -> CoreResult:
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise CoreAdapterError(
            f"Core {command} must emit exactly one JSON Result line; got {len(lines)}"
        )
    try:
        raw = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise CoreAdapterError(f"Core {command} emitted invalid JSON Result") from exc
    if not isinstance(raw, dict):
        raise CoreAdapterError(f"Core {command} Result must be an object")
    missing = [field for field in _RESULT_FIELDS if field not in raw]
    if missing:
        raise CoreAdapterError(f"Core {command} Result is missing semantic fields: {', '.join(missing)}")
    if raw["command"] != command:
        raise CoreAdapterError(f"Core Result command mismatch: expected {command}, got {raw['command']!r}")
    if raw["abi"] != EXPECTED_ABI:
        raise CoreAdapterError(
            f"Core Result ABI mismatch: expected {EXPECTED_ABI!r}, got {raw['abi']!r}",
            actual_abi=raw["abi"],
            actual_version=raw["data"].get("version", "unknown") if isinstance(raw["data"], dict) else "unknown",
        )
    if not isinstance(raw["status"], str):
        raise CoreAdapterError(f"Core {command} Result status must be a string")
    if isinstance(raw["exit_code"], bool) or not isinstance(raw["exit_code"], int):
        raise CoreAdapterError(f"Core {command} Result exit_code must be an integer")
    if not isinstance(raw["data"], dict):
        raise CoreAdapterError(f"Core {command} Result data must be an object")
    if not isinstance(raw["issues"], list):
        raise CoreAdapterError(f"Core {command} Result issues must be an array")
    return CoreResult(
        abi=raw["abi"],
        status=raw["status"],
        exit_code=raw["exit_code"],
        command=raw["command"],
        data=raw["data"],
        issues=raw["issues"],
    )


def _issue_detail(issues: list[Any]) -> str:
    if not issues:
        return "no issues supplied"
    return json.dumps(issues, ensure_ascii=False, sort_keys=True)


def _invoke(runner: Path, command: str, request: dict[str, Any], *, timeout: float | None = None) -> CoreResult:
    payload = json.dumps({"command": command, "request": request}, ensure_ascii=False) + "\n"
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(runner)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _configuration_error(f"Core {command} timed out after {timeout} seconds", path=runner) from exc
    except OSError as exc:
        raise CoreAdapterError(f"Could not start Core {command}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise CoreAdapterError(f"Core {command} runner exited {completed.returncode}{suffix}")
    result = _result_from_output(command, completed)
    if result.status != "ok" or result.exit_code != 0:
        raise CoreAdapterError(
            f"Core {command} failed with status={result.status!r}, "
            f"exit_code={result.exit_code}: {_issue_detail(result.issues)}"
        )
    return result


class CoreBinding:
    """One selected runner and one successful preflight per operation."""

    def __init__(self, runner: Path):
        self.path = runner
        try:
            result = _invoke(runner, "capabilities", {}, timeout=PREFLIGHT_TIMEOUT_SECONDS)
        except CoreAdapterError as exc:
            raise _configuration_error(
                str(exc), path=runner, actual_abi=exc.actual_abi, actual_version=exc.actual_version
            ) from exc
        actual = result.data.get("version", "unknown (missing)")
        if actual != EXPECTED_CORE_VERSION:
            raise _configuration_error(
                f"Core version mismatch: actual={actual!r}, expected={EXPECTED_CORE_VERSION!r}; "
                f"actual ABI={result.abi!r}", path=runner, actual_abi=result.abi, actual_version=actual,
            )
        self.capabilities = result

    def call(self, command: str, request: dict[str, Any]) -> CoreResult:
        if command == "capabilities" and request == {}:
            return self.capabilities
        return _invoke(self.path, command, request)


_ACTIVE_BINDING: ContextVar[CoreBinding | None] = ContextVar("anti_entropy_core_binding", default=None)


@contextmanager
def operation(
    *,
    skill_entrypoint: os.PathLike[str] | str | None = None,
    skill_id: str | None = None,
    runner: os.PathLike[str] | str | None = None,
):
    """Preflight before caller writes; nested helpers retain the active binding."""
    active = _ACTIVE_BINDING.get()
    if active is not None:
        yield active
        return
    binding = CoreBinding(_runner_path(runner, skill_entrypoint=skill_entrypoint, skill_id=skill_id))
    token = _ACTIVE_BINDING.set(binding)
    try:
        yield binding
    finally:
        _ACTIVE_BINDING.reset(token)


def call(command: str, request: dict[str, Any]) -> CoreResult:
    """Use the operation binding, or one explicit binding for this direct call."""
    with operation() as binding:
        return binding.call(command, request)


def _path_request(
    path: os.PathLike[str] | str,
    private_root_files: list[str] | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {"path": str(_absolute_path(path, label="Core request path"))}
    if private_root_files is not None:
        request["private_root_files"] = list(private_root_files)
    return request


def capabilities() -> CoreResult:
    return call("capabilities", {})


def inspect(
    path: os.PathLike[str] | str,
    *,
    private_root_files: list[str] | None = None,
) -> CoreResult:
    return call("inspect", _path_request(path, private_root_files))


def validate(
    path: os.PathLike[str] | str,
    *,
    private_root_files: list[str] | None = None,
) -> CoreResult:
    return call("validate", _path_request(path, private_root_files))


def repair(
    path: os.PathLike[str] | str,
    *,
    private_root_files: list[str] | None = None,
) -> CoreResult:
    return call("repair", _path_request(path, private_root_files))


def stage_complete(
    path: os.PathLike[str] | str,
    *,
    private_root_files: list[str] | None = None,
) -> CoreResult:
    return call("stage.complete", _path_request(path, private_root_files))


__all__ = [
    "CoreAdapterError",
    "CoreResult",
    "EXPECTED_ABI",
    "EXPECTED_CORE_VERSION",
    "RUNNER_ENV",
    "call",
    "capabilities",
    "inspect",
    "operation",
    "repair",
    "stage_complete",
    "validate",
]
