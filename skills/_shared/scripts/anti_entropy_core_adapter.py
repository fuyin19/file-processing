"""Explicit JSONL client for the local anti-entropy Core runner.

The runner location is deliberately supplied by ``ANTI_ENTROPY_CORE_RUNNER``;
there is no import-time or in-process fallback to an envelope implementation.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Any


RUNNER_ENV = "ANTI_ENTROPY_CORE_RUNNER"
EXPECTED_ABI = "anti-entropy-core.runner/v1"
_RESULT_FIELDS = ("abi", "status", "exit_code", "command", "data", "issues")


class CoreAdapterError(RuntimeError):
    """The explicit Core runner could not satisfy its JSONL protocol."""


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


def _runner_path() -> Path:
    configured = os.environ.get(RUNNER_ENV, "")
    if not configured:
        raise CoreAdapterError(f"{RUNNER_ENV} must name an absolute Core runner script")
    runner = _absolute_path(configured, label=RUNNER_ENV)
    if not runner.is_file():
        raise CoreAdapterError(f"{RUNNER_ENV} does not name a runner script: {runner}")
    return runner


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
            f"Core Result ABI mismatch: expected {EXPECTED_ABI!r}, got {raw['abi']!r}"
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


def call(command: str, request: dict[str, Any]) -> CoreResult:
    """Invoke one named Core route through the explicit isolated runner."""
    runner = _runner_path()
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
        )
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
    "RUNNER_ENV",
    "call",
    "capabilities",
    "inspect",
    "repair",
    "stage_complete",
    "validate",
]
