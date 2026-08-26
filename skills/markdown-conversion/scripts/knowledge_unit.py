"""Exact knowledge-unit envelope creation and side-effect-free validation.

The navigation resources are vendored in this repository.  Cortex vendors the
same byte contract independently; neither product imports the other.
"""
from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from pathlib import Path

import native_paths as np


CONTRACT = "knowledge-unit-navigation/v1"
AGENTS_SHA256 = "2067837a839ba3a9a452504a1f85bcff738eb7a181a77458105a8096a33f1bcc"
CLAUDE_SHA256 = "336cc4fbf19beaada7ccf9986414fa91851a8d7a07dfb3ccbe800a69eed0ab49"
_RESOURCE_ROOT = Path(__file__).resolve().parents[2] / "_shared" / "resources" / "knowledge-unit"
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
    """The proposed directory is not an exact base knowledge unit."""


def _resource(name: str, digest: str, length: int) -> bytes:
    payload = (_RESOURCE_ROOT / name).read_bytes()
    if len(payload) != length or hashlib.sha256(payload).hexdigest() != digest:
        raise KnowledgeUnitError(f"Vendored {name} does not match {CONTRACT}")
    return payload


def navigation_bytes() -> tuple[bytes, bytes]:
    return _resource("AGENTS.md", AGENTS_SHA256, 1695), _resource("CLAUDE.md", CLAUDE_SHA256, 11)


def _is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


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


def _entries(directory: Path, relative: str) -> list[os.DirEntry[str]]:
    try:
        with np.scandir(directory) as entries:
            return sorted(entries, key=lambda entry: entry.name.encode("utf-8", "strict"))
    except (OSError, UnicodeEncodeError) as exc:
        raise KnowledgeUnitError(f"Could not inspect {relative or '.'}: {exc}") from exc


def _metadata(entry: os.DirEntry[str], relative: str) -> os.stat_result:
    try:
        metadata = entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise KnowledgeUnitError(f"Could not inspect {relative}: {exc}") from exc
    if _is_reparse(metadata):
        raise KnowledgeUnitError(f"Links and reparse points are forbidden: {relative}")
    return metadata


def _check_name(name: str, relative: str, *, root_guide: bool = False) -> None:
    problem = _component_problem(name)
    if problem:
        raise KnowledgeUnitError(f"Unsafe path component ({problem}): {relative}")
    folded = name.casefold()
    if folded in _FORBIDDEN_COMPONENTS:
        raise KnowledgeUnitError(f"Instruction-control directory is forbidden: {relative}")
    if folded in _FORBIDDEN_BASENAMES and not root_guide:
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


def _walk_assets(root: Path) -> None:
    seen: dict[str, str] = {}

    def visit(directory: Path, prefix: str) -> None:
        children = _entries(directory, f"assets/{prefix}".rstrip("/"))
        if prefix and not children:
            raise KnowledgeUnitError(f"Empty nested asset directory is forbidden: assets/{prefix}")
        for entry in children:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            label = f"assets/{relative}"
            _check_name(entry.name, label)
            key = relative.casefold()
            if key in seen and seen[key] != relative:
                raise KnowledgeUnitError(f"Case-folding path collision: {seen[key]} / {relative}")
            seen[key] = relative
            metadata = _metadata(entry, label)
            if stat.S_ISDIR(metadata.st_mode):
                visit(Path(entry.path), relative)
            elif not stat.S_ISREG(metadata.st_mode):
                raise KnowledgeUnitError(f"Only ordinary files and real directories are allowed: {label}")

    children = _entries(root, "assets")
    if len(children) == 1 and children[0].name == ".keep":
        metadata = _metadata(children[0], "assets/.keep")
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
            raise KnowledgeUnitError("assets/.keep must be an ordinary zero-byte file")
        return
    if not children:
        raise KnowledgeUnitError("Semantically empty assets/ must contain exactly zero-byte .keep")
    if any(entry.name == ".keep" for entry in children):
        raise KnowledgeUnitError("assets/.keep is forbidden when assets/ contains payload")
    visit(root, "")


def _check_src(root: Path) -> None:
    children = _entries(root, "src")
    if len(children) != 1:
        raise KnowledgeUnitError("src/ must contain exactly one source file or zero-byte .keep")
    entry = children[0]
    _check_name(entry.name, f"src/{entry.name}")
    metadata = _metadata(entry, f"src/{entry.name}")
    if not stat.S_ISREG(metadata.st_mode):
        raise KnowledgeUnitError("src/ permits one direct ordinary file only")
    if entry.name == ".keep" and metadata.st_size != 0:
        raise KnowledgeUnitError("src/.keep must be zero-byte")


def validate_representation_names(names: list[str]) -> str:
    """Validate the platform-independent representation-name contract."""
    if not names:
        raise KnowledgeUnitError("At least one root representation file is required")
    folded_names: dict[str, str] = {}
    for name in names:
        key = name.casefold()
        if key in folded_names:
            raise KnowledgeUnitError(
                f"Representation names collide under case folding: {folded_names[key]} / {name}"
            )
        folded_names[key] = name
    stems = {Path(name).stem for name in names}
    if len(stems) != 1 or any(Path(name).suffix == "" for name in names):
        raise KnowledgeUnitError("Root representation files must share one exact stem and have extensions")
    suffixes: dict[str, str] = {}
    for name in names:
        suffix = Path(name).suffix
        key = suffix.casefold()
        if key in suffixes:
            raise KnowledgeUnitError(f"Representation extensions collide under case folding: {suffixes[key]} / {suffix}")
        suffixes[key] = suffix
    return next(iter(stems))


def validate(root: Path) -> str:
    """Validate without creating, deleting, or rewriting any entry; return the stem."""
    root = np.logical(root)
    try:
        root_metadata = np.lstat(root)
    except OSError as exc:
        raise KnowledgeUnitError(f"Knowledge-unit root is unreadable: {exc}") from exc
    if _is_reparse(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise KnowledgeUnitError("Knowledge-unit root must be a real directory")
    entries = _entries(root, ".")
    folded: dict[str, str] = {}
    files: list[str] = []
    directories: set[str] = set()
    for entry in entries:
        key = entry.name.casefold()
        if key in folded and folded[key] != entry.name:
            raise KnowledgeUnitError(f"Case-folding root collision: {folded[key]} / {entry.name}")
        folded[key] = entry.name
        root_guide = entry.name in {"AGENTS.md", "CLAUDE.md"}
        _check_name(entry.name, entry.name, root_guide=root_guide)
        metadata = _metadata(entry, entry.name)
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(entry.name)
        elif stat.S_ISREG(metadata.st_mode):
            files.append(entry.name)
        else:
            raise KnowledgeUnitError(f"Only ordinary files and real directories are allowed: {entry.name}")
    if directories != {"assets", "src"}:
        raise KnowledgeUnitError("Knowledge-unit root directories must be exactly assets/ and src/")
    agents, claude = navigation_bytes()
    for name, expected in (("AGENTS.md", agents), ("CLAUDE.md", claude)):
        path = root / name
        if name not in files or np.read_bytes(path) != expected:
            raise KnowledgeUnitError(f"{name} must exactly match {CONTRACT}")
    representations = [name for name in files if name not in {"AGENTS.md", "CLAUDE.md"}]
    stem = validate_representation_names(representations)
    _walk_assets(root / "assets")
    _check_src(root / "src")
    return stem


def finalize_owned_stage(root: Path) -> str:
    """Fill only the base-envelope items owned by a fresh conversion stage."""
    root = np.logical(root)
    agents, claude = navigation_bytes()
    for name, payload in (("AGENTS.md", agents), ("CLAUDE.md", claude)):
        target = root / name
        if np.exists(target):
            metadata = np.lstat(target)
            if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise KnowledgeUnitError(f"Existing {name} is not an ordinary file")
            if np.read_bytes(target) != payload:
                raise KnowledgeUnitError(f"Existing {name} does not match {CONTRACT}")
        else:
            np.write_bytes(target, payload)
    for name in ("assets", "src"):
        directory = root / name
        if np.exists(directory):
            metadata = np.lstat(directory)
            if _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise KnowledgeUnitError(f"Existing {name}/ is not a real directory")
        else:
            np.mkdir(directory)
        with np.scandir(directory) as entries:
            empty = next(entries, None) is None
        if empty:
            np.write_bytes(directory / ".keep", b"")
    return validate(root)


__all__ = [
    "AGENTS_SHA256",
    "CLAUDE_SHA256",
    "CONTRACT",
    "KnowledgeUnitError",
    "bundle_target",
    "finalize_owned_stage",
    "navigation_bytes",
    "validate",
    "validate_representation_names",
]
