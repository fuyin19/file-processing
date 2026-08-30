"""Local, side-effect-free bundle-target selection.

Envelope construction, validation, repair, and stage completion belong to the
explicit anti-entropy Core runner. This module intentionally retains only the
pipeline-local target selection that happens before a stage exists.
"""
from __future__ import annotations

import unicodedata
from pathlib import Path

import native_paths as np


_FORBIDDEN_CHARS = set('<>:"/\\|?*')
_FORBIDDEN_BASENAMES = {
    "agents.md",
    "agents.override.md",
    "claude.md",
    "claude.local.md",
    ".cursorrules",
    ".mcp.json",
}
_FORBIDDEN_COMPONENTS = {".claude", ".cursor"}
_DEVICES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
    *(f"com{i}" for i in "¹²³"),
    *(f"lpt{i}" for i in "¹²³"),
}


class KnowledgeUnitError(ValueError):
    """A proposed bundle target is not a safe direct output child."""


def _component_problem(name: str) -> str | None:
    if not name or name in {".", ".."}:
        return "empty or relative component"
    folded = name.casefold()
    if folded == ".cortex" or folded.startswith(".cortex-"):
        return "reserved Cortex name"
    if name.endswith((".", " ")):
        return "trailing dot or space"
    if any(character in _FORBIDDEN_CHARS or unicodedata.category(character) == "Cc" for character in name):
        return "forbidden character"
    if name.rstrip(" .").split(".", 1)[0].casefold() in _DEVICES:
        return "Windows device name"
    try:
        name.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return "non-UTF-8 component"
    return None


def _check_name(name: str, relative: str) -> None:
    problem = _component_problem(name)
    if problem:
        raise KnowledgeUnitError(f"Unsafe path component ({problem}): {relative}")
    folded = name.casefold()
    if folded in _FORBIDDEN_COMPONENTS:
        raise KnowledgeUnitError(f"Instruction-control directory is forbidden: {relative}")
    if folded in _FORBIDDEN_BASENAMES:
        raise KnowledgeUnitError(f"Instruction-control file is forbidden: {relative}")


def bundle_target(parent: Path, stem: str, *, boundary: Path | None = None) -> Path:
    """Return one safe direct bundle child without touching the filesystem."""
    _check_name(stem, stem)
    parent = np.logical(parent)
    if boundary is not None and not np.is_within(parent, np.logical(boundary)):
        raise KnowledgeUnitError(f"Bundle parent escapes its output boundary: {parent}")
    target = np.logical(parent / stem)
    if target.name != stem or not np.paths_equal(target.parent, parent):
        raise KnowledgeUnitError(f"Bundle target must be a safe direct child: {stem}")
    return target


__all__ = ["KnowledgeUnitError", "bundle_target"]
