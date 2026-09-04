"""Bind conversion skills to one complete, relocatable skills installation."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Iterable


_REPARSE_POINT = 0x400
_RESTORE = (
    "restore the complete unified installation so sibling file-processing and "
    "conversion skills come from one skills root"
)


@dataclass(frozen=True)
class RuntimeLayout:
    skill_id: str
    skills_root: Path
    scripts: Path
    carrier_scripts: Path

    def fail(self, path: os.PathLike[str] | str, reason: str) -> "NoReturn":
        _fail(self.skill_id, self.skills_root, Path(path), reason)

    def require_file(self, path: os.PathLike[str] | str) -> Path:
        return _require_file(self.skill_id, self.skills_root, Path(path))

    def verify_module(
        self,
        module: ModuleType,
        *,
        label: str,
        expected: os.PathLike[str] | str | None = None,
    ) -> Path:
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str) or not origin:
            self.fail(expected or self.skills_root, f"{label} has no filesystem module origin")
        actual = self.require_file(origin)
        if expected is not None:
            wanted = self.require_file(expected)
            if os.path.normcase(os.path.realpath(actual)) != os.path.normcase(os.path.realpath(wanted)):
                self.fail(actual, f"{label} loaded from {actual}; expected {wanted}")
        return actual


def _fail(skill_id: str, skills_root: Path, path: Path, reason: str) -> "NoReturn":
    print(
        f"ERROR: {skill_id}: incomplete unified file-processing installation; "
        f"skills root: {skills_root}; required path: {path}; reason: {reason}; {_RESTORE}.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _within(root: str, candidate: str) -> bool:
    try:
        common = os.path.commonpath((root, candidate))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(root)


def _require_file(skill_id: str, skills_root: Path, path: Path) -> Path:
    root = os.path.abspath(os.fspath(skills_root))
    candidate = os.path.abspath(os.fspath(path))
    if not _within(root, candidate):
        _fail(skill_id, Path(root), Path(candidate), "path escapes the selected skills root")

    relative = os.path.relpath(candidate, root)
    current = root
    components = [] if relative == os.curdir else list(Path(relative).parts)
    for index, component in enumerate([None, *components]):
        if component is not None:
            current = os.path.join(current, component)
        try:
            info = os.lstat(current)
        except OSError as exc:
            _fail(skill_id, Path(root), Path(current), f"unavailable ({exc})")
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            _fail(skill_id, Path(root), Path(current), "link or Windows reparse point is forbidden")
        final = index == len(components)
        if final:
            if not stat.S_ISREG(info.st_mode):
                _fail(skill_id, Path(root), Path(current), "required dependency is not an ordinary file")
        elif not stat.S_ISDIR(info.st_mode):
            _fail(skill_id, Path(root), Path(current), "path component is not an ordinary directory")

    resolved_root = os.path.realpath(root)
    resolved_candidate = os.path.realpath(candidate)
    if not _within(resolved_root, resolved_candidate):
        _fail(skill_id, Path(root), Path(candidate), "resolved path escapes the selected skills root")
    return Path(candidate)


def bootstrap(
    *,
    entrypoint: os.PathLike[str] | str,
    skill_id: str,
    carrier_files: Iterable[str] = (),
    sibling_files: Iterable[tuple[str, str]] = (),
    import_siblings: Iterable[str] = (),
) -> RuntimeLayout:
    entry = Path(os.path.abspath(os.fspath(entrypoint)))
    scripts = entry.parent
    skill_dir = scripts.parent
    skills_root = skill_dir.parent
    carrier_scripts = skills_root / "file-processing" / "scripts"
    layout = RuntimeLayout(skill_id, skills_root, scripts, carrier_scripts)

    required = [
        skill_dir / "SKILL.md",
        entry,
        skills_root / "file-processing" / "SKILL.md",
        carrier_scripts / "runtime_layout.py",
        *(carrier_scripts / name for name in carrier_files),
        *(skills_root / sibling / relative for sibling, relative in sibling_files),
    ]
    for path in required:
        layout.require_file(path)

    search = [scripts]
    search.extend(skills_root / sibling / "scripts" for sibling in import_siblings)
    search.append(carrier_scripts)
    normalized = [str(path) for path in search]
    for value in normalized:
        while value in sys.path:
            sys.path.remove(value)
    for value in reversed(normalized):
        sys.path.insert(0, value)
    return layout


__all__ = ["RuntimeLayout", "bootstrap"]
