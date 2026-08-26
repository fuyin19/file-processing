from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "markdown-conversion" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import knowledge_unit


def _unit(tmp_path: Path, *representations: str) -> Path:
    root = tmp_path / "unit"
    root.mkdir()
    agents, claude = knowledge_unit.navigation_bytes()
    (root / "AGENTS.md").write_bytes(agents)
    (root / "CLAUDE.md").write_bytes(claude)
    for name in representations or ("memo.md",):
        (root / name).write_bytes(name.encode("utf-8"))
    for name in ("assets", "src"):
        (root / name).mkdir()
        (root / name / ".keep").write_bytes(b"")
    return root


def test_sc001_exact_navigation_contract_and_standalone_vendor():
    agents, claude = knowledge_unit.navigation_bytes()
    assert len(agents) == 1695
    assert hashlib.sha256(agents).hexdigest() == knowledge_unit.AGENTS_SHA256
    assert claude == b"@AGENTS.md\n" and len(claude) == 11
    assert hashlib.sha256(claude).hexdigest() == knowledge_unit.CLAUDE_SHA256
    assert "record.json" not in (SCRIPTS / "knowledge_unit.py").read_text(encoding="utf-8")


def test_sc008_sc009_open_representations_markers_and_source(tmp_path):
    root = _unit(tmp_path, "memo.md", "memo.PDF", "memo.custom")
    assert knowledge_unit.validate(root) == "memo"
    (root / "src/.keep").unlink()
    (root / "src/original.weird").write_bytes(b"")
    (root / "assets/.keep").unlink()
    (root / "assets/image.bin").write_bytes(b"")
    assert knowledge_unit.validate(root) == "memo"


def test_sc008_casefold_name_and_suffix_collision_is_platform_independent():
    with pytest.raises(knowledge_unit.KnowledgeUnitError, match="case folding"):
        knowledge_unit.validate_representation_names(["memo.md", "memo.MD"])


@pytest.mark.parametrize("stem", [".", ".."])
def test_sc010_bundle_target_rejects_relative_components_without_writing(tmp_path, stem):
    parent = tmp_path / "output"
    with pytest.raises(knowledge_unit.KnowledgeUnitError, match="relative component"):
        knowledge_unit.bundle_target(parent, stem, boundary=parent)
    assert not parent.exists()


def test_sc010_bundle_target_rejects_escaped_batch_parent_without_writing(tmp_path):
    boundary = tmp_path / "output"
    with pytest.raises(knowledge_unit.KnowledgeUnitError, match="escapes its output boundary"):
        knowledge_unit.bundle_target(boundary / "..", "memo", boundary=boundary)
    assert not boundary.exists()


@pytest.mark.parametrize("location", ["root", "assets-file", "assets-dir", "src"])
def test_sc010_reserved_cortex_components_are_rejected_everywhere(tmp_path, location):
    root = _unit(tmp_path, "memo.md")
    if location == "root":
        (root / ".CoRtEx-item.md").write_bytes(b"reserved")
    elif location == "assets-file":
        (root / "assets/.keep").unlink()
        (root / "assets/.cortex-item.bin").write_bytes(b"reserved")
    elif location == "assets-dir":
        (root / "assets/.keep").unlink()
        (root / "assets/.CORTEX").mkdir()
        (root / "assets/.CORTEX/payload.bin").write_bytes(b"reserved")
    else:
        (root / "src/.keep").unlink()
        (root / "src/.cortex-source.txt").write_bytes(b"reserved")

    with pytest.raises(knowledge_unit.KnowledgeUnitError, match="reserved Cortex name"):
        knowledge_unit.validate(root)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda root: (root / "other.json").write_bytes(b"{}"),
        lambda root: (root / "memo").write_bytes(b"missing extension"),
        lambda root: (root / "assets/.keep").write_bytes(b"not empty"),
        lambda root: (root / "src/nested").mkdir(),
        lambda root: (root / "assets/CLAUDE.local.md").write_bytes(b"instructions"),
    ],
)
def test_sc008_to_sc012_strict_validator_rejects_ambiguous_or_control_state(tmp_path, mutate):
    root = _unit(tmp_path, "memo.md")
    mutate(root)
    with pytest.raises(knowledge_unit.KnowledgeUnitError):
        knowledge_unit.validate(root)


def test_sc011_navigation_tamper_is_rejected_without_repair(tmp_path):
    root = _unit(tmp_path, "memo.md")
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    (root / "AGENTS.md").write_bytes(b"tampered\n")
    with pytest.raises(knowledge_unit.KnowledgeUnitError):
        knowledge_unit.validate(root)
    after = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == {**before, "AGENTS.md": b"tampered\n"}
