"""Small test-only anti-entropy Core JSONL runner.

It is intentionally a subprocess fixture, so adapter tests exercise the same
``python -I <absolute-runner>`` boundary used by the conversion paths.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = ROOT / "skills" / "_shared" / "resources" / "knowledge-unit"
GUIDES = {
    "AGENTS.md": (RESOURCE_ROOT / "AGENTS.md").read_bytes(),
    "CLAUDE.md": (RESOURCE_ROOT / "CLAUDE.md").read_bytes(),
}


def _emit(
    command: str,
    *,
    data: dict[str, Any] | None = None,
    status: str = "ok",
    exit_code: int = 0,
    issues: list[dict[str, str]] | None = None,
) -> int:
    print(
        json.dumps(
            {
                "abi": "anti-entropy-core.runner/v1",
                "status": status,
                "exit_code": exit_code,
                "command": command,
                "data": data or {},
                "issues": issues or [],
            },
            ensure_ascii=False,
        )
    )
    # Core reports per-line validation/usage outcomes in Result.exit_code and
    # keeps serving until EOF; the process exit code signals runner health.
    return 0


def _failure(command: str, message: str) -> int:
    return _emit(
        command,
        status="error",
        exit_code=1,
        issues=[{"message": message}],
    )


def _path(request: dict[str, Any]) -> Path:
    raw = request.get("path")
    if not isinstance(raw, str):
        raise ValueError("request.path must be a string")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("request.path must be absolute")
    return path


def _filesystem_path(path: Path) -> Path:
    """Use the Windows extended form only at the fixture's filesystem boundary."""
    if os.name != "nt":
        return path
    value = str(path)
    if value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def _validate(path: Path, private_root_files: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    # This fixture exercises the adapter boundary only. It intentionally does
    # not duplicate Core's envelope validation authority in the test suite.
    return {"path": str(path), "private_root_files": sorted(private_root_files)}, None


def _complete(path: Path, private_root_files: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    filesystem_path = _filesystem_path(path)
    if not filesystem_path.is_dir():
        return None, "stage.complete path must name an existing stage directory"
    for name, expected in GUIDES.items():
        target = filesystem_path / name
        if target.exists() and (not target.is_file() or target.read_bytes() != expected):
            return None, f"existing {name} does not match the navigation contract"
        if not target.exists():
            target.write_bytes(expected)
    for name in ("assets", "src"):
        directory = filesystem_path / name
        if directory.exists() and not directory.is_dir():
            return None, f"existing {name}/ is not a directory"
        directory.mkdir(exist_ok=True)
        if not any(directory.iterdir()):
            (directory / ".keep").write_bytes(b"")
    return _validate(path, private_root_files)


def main() -> int:
    lines = [line for line in sys.stdin.read().splitlines() if line.strip()]
    if len(lines) != 1:
        return _failure("unknown", "expected exactly one JSONL request")
    try:
        envelope = json.loads(lines[0])
        command = envelope["command"]
        request = envelope["request"]
        if not isinstance(command, str) or not isinstance(request, dict):
            raise ValueError("request envelope is invalid")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _failure("unknown", str(exc))
    if command == "capabilities":
        return _emit(command, data={"version": "1.2.1", "commands": ["capabilities", "inspect", "validate", "repair", "stage.complete"]})
    try:
        path = _path(request)
        private = request.get("private_root_files", [])
        if not isinstance(private, list) or not all(isinstance(item, str) for item in private):
            raise ValueError("private_root_files must be a string array")
    except ValueError as exc:
        return _failure(command, str(exc))
    private_files = set(private)
    if command == "inspect":
        return _emit(
            command,
            data={
                "path": str(path),
                "exists": _filesystem_path(path).exists(),
                "private_root_files": private,
            },
        )
    if command == "validate":
        data, problem = _validate(path, private_files)
    elif command == "repair":
        data, problem = _validate(path, private_files)
    elif command == "stage.complete":
        data, problem = _complete(path, private_files)
    else:
        return _failure(command, "unsupported command")
    if problem:
        return _failure(command, problem)
    return _emit(command, data=data)


if __name__ == "__main__":
    raise SystemExit(main())
