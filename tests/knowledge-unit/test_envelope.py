from __future__ import annotations

from pathlib import Path
import os
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "skills" / "_shared" / "scripts"
SCRIPTS = ROOT / "skills" / "markdown-conversion" / "scripts"
for value in (str(SHARED), str(SCRIPTS)):
    if value not in sys.path:
        sys.path.insert(0, value)

import anti_entropy_core_adapter as core
import knowledge_unit


ACTUAL_CORE_RUNNER = os.environ.get("FILE_PROCESSING_REAL_CORE_RUNNER", "")


def test_explicit_core_runner_exposes_all_required_routes(tmp_path):
    stage = tmp_path / "unit"
    stage.mkdir()
    (stage / "memo.md").write_text("body\n", encoding="utf-8")

    capabilities = core.capabilities()
    assert capabilities.command == "capabilities"
    assert capabilities.data["commands"] == [
        "capabilities", "inspect", "validate", "repair", "stage.complete"
    ]

    inspected = core.inspect(stage, private_root_files=["record.json"])
    assert inspected.command == "inspect"
    assert inspected.data == {
        "path": str(stage.resolve()),
        "exists": True,
        "private_root_files": ["record.json"],
    }

    completed = core.stage_complete(stage)
    assert completed.command == "stage.complete"
    assert completed.data["path"] == str(stage.resolve())
    assert (stage / "AGENTS.md").is_file()
    assert (stage / "CLAUDE.md").is_file()
    assert (stage / "assets" / ".keep").read_bytes() == b""
    assert (stage / "src" / ".keep").read_bytes() == b""

    assert core.validate(stage).data["path"] == str(stage.resolve())
    assert core.repair(stage).command == "repair"


def test_actual_core_completes_and_validates_a_bundle_stage(tmp_path, monkeypatch):
    assert ACTUAL_CORE_RUNNER, "Set FILE_PROCESSING_REAL_CORE_RUNNER to the current Core Candidate runner"
    stage = tmp_path / "actual-core-unit"
    stage.mkdir()
    (stage / "memo.md").write_text("body\n", encoding="utf-8")
    monkeypatch.setenv(core.RUNNER_ENV, str(Path(ACTUAL_CORE_RUNNER).absolute()))

    assert core.capabilities().abi == core.EXPECTED_ABI
    completed = core.stage_complete(stage)
    assert completed.abi == core.EXPECTED_ABI
    assert core.validate(stage).status == "ok"
    assert (stage / "AGENTS.md").is_file()
    assert (stage / "CLAUDE.md").is_file()


def test_core_adapter_requires_an_explicit_absolute_runner_without_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv(core.RUNNER_ENV)
    with pytest.raises(core.CoreAdapterError, match=core.RUNNER_ENV):
        core.stage_complete(tmp_path)

    monkeypatch.setenv(core.RUNNER_ENV, "relative-runner.py")
    with pytest.raises(core.CoreAdapterError, match="must be absolute"):
        core.capabilities()


def test_core_adapter_rejects_a_malformed_normal_result(tmp_path, monkeypatch):
    runner = tmp_path / "bad_core.py"
    runner.write_text("print('{}')\n", encoding="utf-8")
    monkeypatch.setenv(core.RUNNER_ENV, str(runner))

    with pytest.raises(core.CoreAdapterError, match="missing semantic fields"):
        core.capabilities()


def test_core_adapter_sends_only_the_command_and_absolute_request(tmp_path, monkeypatch):
    runner = tmp_path / "capture_core.py"
    runner.write_text(
        "import json, sys\n"
        "incoming = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'abi': 'anti-entropy-core.runner/v1', 'status': 'ok', 'exit_code': 0, "
        "'command': incoming['command'], 'data': {'incoming': incoming, 'version': '1.2.1'}, 'issues': []}))\n",
        encoding="utf-8",
    )
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setenv(core.RUNNER_ENV, str(runner))

    result = core.inspect(stage, private_root_files=["record.json"])

    assert result.data["incoming"] == {
        "command": "inspect",
        "request": {"path": str(stage.resolve()), "private_root_files": ["record.json"]},
    }


def test_core_adapter_distinguishes_runner_failure_from_result_failure(tmp_path, monkeypatch):
    line_error = tmp_path / "line_error_core.py"
    line_error.write_text(
        "import json, sys\n"
        "incoming = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'abi': 'anti-entropy-core.runner/v1', 'status': 'error', 'exit_code': 1, "
        "'command': incoming['command'], 'data': {}, 'issues': [{'message': 'invalid'}]}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(core.RUNNER_ENV, str(line_error))
    with pytest.raises(core.CoreAdapterError, match="status='error'"):
        core.validate(tmp_path)

    broken_runner = tmp_path / "broken_core.py"
    broken_runner.write_text("raise SystemExit(7)\n", encoding="utf-8")
    monkeypatch.setenv(core.RUNNER_ENV, str(broken_runner))
    with pytest.raises(core.CoreAdapterError, match="runner exited 7"):
        core.capabilities()


@pytest.mark.parametrize("stem", [".", ".."])
def test_safe_target_selection_remains_local_and_side_effect_free(tmp_path, stem):
    parent = tmp_path / "output"
    with pytest.raises(knowledge_unit.KnowledgeUnitError, match="relative component"):
        knowledge_unit.bundle_target(parent, stem, boundary=parent)
    assert not parent.exists()


def test_safe_target_selection_rejects_escaped_batch_parent_without_writing(tmp_path):
    boundary = tmp_path / "output"
    with pytest.raises(knowledge_unit.KnowledgeUnitError, match="escapes its output boundary"):
        knowledge_unit.bundle_target(boundary / "..", "memo", boundary=boundary)
    assert not boundary.exists()


def test_local_module_no_longer_contains_envelope_authority():
    source = (SCRIPTS / "knowledge_unit.py").read_text(encoding="utf-8")
    for authority in ("navigation_bytes", "def validate(", "finalize_owned_stage"):
        assert authority not in source
