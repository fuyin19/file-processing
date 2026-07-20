"""Tests for the OKF frontmatter prepare -> plan -> apply pipeline."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SCRIPT_PATH = ROOT / "skills" / "okf-frontmatter" / "scripts" / "frontmatter_pipeline.py"
SCRIPT = [sys.executable, str(SCRIPT_PATH)]


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("frontmatter_pipeline", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(SCRIPT + list(args), text=True, capture_output=True)


def _last_json(result: subprocess.CompletedProcess[str]) -> dict:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stderr
    return json.loads(lines[-1])


def _proposal(run: dict, fields_by_name: dict[str, dict]) -> dict:
    items = []
    for item in run["items"]:
        fields = fields_by_name.get(Path(item["input_path"]).name, {})
        items.append(
            {
                "path": item["input_path"],
                "input_sha256": item["input_sha256"],
                "fields": fields,
                "evidence": {key: "test evidence" for key in fields},
            }
        )
    return {
        "schema_version": "FrontmatterProposal/v1",
        "run_id": run["run_id"],
        "complete_coverage": True,
        "items": items,
    }


def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _prepare(source: Path, run_dir: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = _run("prepare", "--input", str(source), "--run-dir", str(run_dir), *extra)
    assert result.returncode in {0, 2}, result.stderr
    payload = _last_json(result)
    run = json.loads(Path(payload["state_path"]).read_text(encoding="utf-8"))
    return result, run


def test_prepare_plan_apply_preserves_round_trip_yaml_and_body(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text(
        "---\n"
        "# keep this comment\n"
        "source: \"https://example.com/note\"\n"
        "custom: 'keep-me'\n"
        "---\n"
        "\n# Existing heading\n\nBody text.\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir)
    assert run["schema_version"] == "FrontmatterRun/v1"
    assert run["status"] == "awaiting_proposal"
    assert run["items"][0]["suggested_fields"]["title"] == "Existing heading"
    assert run["items"][0]["suggested_fields"]["resource"] == "https://example.com/note"

    proposal = _proposal(
        run,
        {
            "note.md": {
                "type": "Reference",
                "title": "Existing heading",
                "description": "A concise test note.",
                "timestamp": "2026-07-20T00:00:00Z",
                "tags": ["test"],
            }
        },
    )
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    assert planned.returncode == 0, planned.stderr
    plan_payload = _last_json(planned)
    assert plan_payload["plan_id"].startswith("frontmatter-plan@sha256:")
    assert plan_payload["readiness"] == "okf_ready"

    applied = _run("apply", "--plan", plan_payload["plan_path"])
    assert applied.returncode == 0, applied.stderr
    receipt = _last_json(applied)
    assert receipt["schema_version"] == "FrontmatterApplyReceipt/v1"
    assert receipt["status"] == "applied"
    text = source.read_text(encoding="utf-8")
    assert "# keep this comment" in text
    assert "custom: 'keep-me'" in text
    assert "type: Reference" in text
    assert "description: A concise test note." in text
    assert "\n# Existing heading\n\nBody text.\n" in text
    assert not run_dir.exists()


def test_existing_nonempty_field_requires_replace_fields(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text(
        "---\ntype: Reference\ntitle: Original\ndescription: Existing.\n"
        "timestamp: 2026-01-01T00:00:00Z\n---\n# Body\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir, "--keep-workspace")
    proposal = _proposal(run, {"note.md": {"title": "Replacement"}})
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    assert planned.returncode == 0, planned.stderr
    applied = _run("apply", "--plan", _last_json(planned)["plan_path"])
    assert applied.returncode == 0, applied.stderr
    assert "title: Original" in source.read_text(encoding="utf-8")

    run_dir2 = tmp_path / "run2"
    _, run2 = _prepare(source, run_dir2, "--replace-fields", "title", "--keep-workspace")
    proposal2 = _proposal(run2, {"note.md": {"title": "Replacement"}})
    proposal_path2 = _write_json(tmp_path / "proposal2.json", proposal2)
    planned2 = _run("plan", "--state", str(run_dir2 / "run.json"), "--proposal", str(proposal_path2))
    assert planned2.returncode == 0, planned2.stderr
    applied2 = _run("apply", "--plan", _last_json(planned2)["plan_path"])
    assert applied2.returncode == 0, applied2.stderr
    assert "title: Replacement" in source.read_text(encoding="utf-8")


def test_explicit_metadata_has_priority_over_agent_replacement(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text(
        "---\ntype: Reference\ntitle: Original\ndescription: Existing.\n"
        "timestamp: 2026-01-01T00:00:00Z\n---\n# Deterministic title\n",
        encoding="utf-8",
    )
    metadata = _write_json(tmp_path / "metadata.json", {"title": "Explicit title"})
    run_dir = tmp_path / "run"
    _, run = _prepare(
        source,
        run_dir,
        "--replace-fields",
        "title",
        "--metadata-json",
        str(metadata),
        "--keep-workspace",
    )
    proposal = _proposal(run, {"note.md": {"title": "Agent title"}})
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    assert planned.returncode == 0, planned.stderr
    applied = _run("apply", "--plan", _last_json(planned)["plan_path"])
    assert applied.returncode == 0, applied.stderr
    assert "title: Explicit title" in source.read_text(encoding="utf-8")


def test_source_ids_cannot_be_supplied(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Note\n", encoding="utf-8")
    metadata = _write_json(tmp_path / "metadata.json", {"source_ids": ["managed"]})
    result = _run(
        "prepare",
        "--input",
        str(source),
        "--metadata-json",
        str(metadata),
        "--run-dir",
        str(tmp_path / "run"),
    )
    assert result.returncode == 1
    assert "source_ids" in result.stderr


@pytest.mark.parametrize(
    "name,text",
    [
        ("broken.md", "---\ntype: [broken\n---\nbody\n"),
        ("mapping.md", "---\n- not\n- a mapping\n---\nbody\n"),
        ("duplicate.md", "---\ntype: one\ntype: two\n---\nbody\n"),
    ],
)
def test_prepare_rejects_invalid_frontmatter(tmp_path: Path, name: str, text: str) -> None:
    source = tmp_path / name
    source.write_text(text, encoding="utf-8")
    result = _run("prepare", "--input", str(source), "--run-dir", str(tmp_path / "run"))
    assert result.returncode == 1
    assert "frontmatter" in result.stderr.lower()


@pytest.mark.parametrize("reserved", ["index.md", "log.md"])
def test_prepare_rejects_reserved_okf_documents(tmp_path: Path, reserved: str) -> None:
    source = tmp_path / reserved
    source.write_text("# Reserved\n", encoding="utf-8")
    result = _run("prepare", "--input", str(source), "--run-dir", str(tmp_path / "run"))
    assert result.returncode == 1
    assert "reserved" in result.stderr.lower()


def test_validate_complete_generic_okf_document(tmp_path: Path) -> None:
    source = tmp_path / "ready.md"
    source.write_text(
        "---\ntype: Reference\ntitle: Ready\ndescription: Ready note.\n"
        "timestamp: 2026-07-20T00:00:00Z\n---\n# Ready\n",
        encoding="utf-8",
    )
    result = _run("validate", "--input", str(source))
    assert result.returncode == 0, result.stderr
    payload = _last_json(result)
    assert payload["readiness"] == "okf_ready"
    assert payload["valid"] is True


def test_apply_rejects_plan_tampering(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Note\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir, "--keep-workspace")
    proposal = _proposal(
        run,
        {"note.md": {"type": "Reference", "title": "Note", "description": "A note.", "timestamp": "2026-07-20T00:00:00Z"}},
    )
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    plan_path = Path(_last_json(planned)["plan_path"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["operations"][0]["target_path"] = str(tmp_path / "other.md")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    applied = _run("apply", "--plan", str(plan_path))
    assert applied.returncode == 3
    assert "digest" in applied.stderr.lower()


def test_plan_rejects_tampered_run_state(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Note\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir, "--keep-workspace")
    proposal = _proposal(
        run,
        {"note.md": {"type": "Reference", "description": "A note."}},
    )
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    state_path = run_dir / "run.json"
    tampered = json.loads(state_path.read_text(encoding="utf-8"))
    tampered["required_fields"].append("unreviewed_field")
    state_path.write_text(json.dumps(tampered), encoding="utf-8")
    planned = _run("plan", "--state", str(state_path), "--proposal", str(proposal_path))
    assert planned.returncode == 3
    assert "digest" in planned.stderr.lower()
    assert run_dir.exists()


def test_apply_rejects_changed_source(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Note\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir, "--keep-workspace")
    proposal = _proposal(
        run,
        {"note.md": {"type": "Reference", "title": "Note", "description": "A note.", "timestamp": "2026-07-20T00:00:00Z"}},
    )
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    source.write_text("Changed\n", encoding="utf-8")
    applied = _run("apply", "--plan", _last_json(planned)["plan_path"])
    assert applied.returncode == 3
    assert "changed" in applied.stderr.lower()


def test_apply_is_idempotent_when_workspace_is_retained(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Note\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir, "--keep-workspace")
    proposal = _proposal(
        run,
        {"note.md": {"type": "Reference", "title": "Note", "description": "A note.", "timestamp": "2026-07-20T00:00:00Z"}},
    )
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    plan_path = _last_json(planned)["plan_path"]
    first = _run("apply", "--plan", plan_path)
    second = _run("apply", "--plan", plan_path)
    assert first.returncode == second.returncode == 0
    assert _last_json(second)["status"] == "already_applied"


def test_batch_requires_complete_proposal_unless_accept_partial(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("# One\n", encoding="utf-8")
    (docs / "two.md").write_text("# Two\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _, run = _prepare(docs, run_dir, "--accept-partial", "--keep-workspace")
    proposal = _proposal(
        run,
        {"one.md": {"type": "Reference", "title": "One", "description": "First.", "timestamp": "2026-07-20T00:00:00Z"}},
    )
    proposal["items"] = [item for item in proposal["items"] if Path(item["path"]).name == "one.md"]
    proposal["complete_coverage"] = False
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    assert planned.returncode == 0, planned.stderr
    payload = _last_json(planned)
    assert payload["operation_count"] == 1
    assert payload["skipped_count"] == 1


def test_batch_incomplete_proposal_is_blocked_by_default(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("# One\n", encoding="utf-8")
    (docs / "two.md").write_text("# Two\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _, run = _prepare(docs, run_dir, "--keep-workspace")
    proposal = _proposal(
        run,
        {"one.md": {"type": "Reference", "description": "First."}},
    )
    proposal["items"] = [item for item in proposal["items"] if Path(item["path"]).name == "one.md"]
    proposal["complete_coverage"] = False
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    assert planned.returncode == 2
    assert "cover every" in planned.stderr.lower()


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", ["Reference"]),
        ("timestamp", "not-a-date"),
        ("tags", "not-a-list"),
    ],
)
def test_plan_blocks_invalid_field_types(tmp_path: Path, field: str, value: object) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Note\n", encoding="utf-8")
    metadata = _write_json(tmp_path / "metadata.json", {field: value})
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir, "--metadata-json", str(metadata), "--keep-workspace")
    fields = {
        "type": "Reference",
        "title": "Note: escaped safely",
        "description": "A note.",
        "timestamp": "2026-07-20T00:00:00Z",
    }
    proposal_path = _write_json(tmp_path / "proposal.json", _proposal(run, {"note.md": fields}))
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    assert planned.returncode == 2
    assert not list((run_dir / "plans").glob("*.json"))


def test_plan_only_retains_workspace_after_apply(tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Note\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir, "--plan-only")
    assert run["keep_workspace"] is True
    proposal = _proposal(run, {"note.md": {"type": "Reference", "description": "A note."}})
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    assert planned.returncode == 0, planned.stderr
    applied = _run("apply", "--plan", _last_json(planned)["plan_path"])
    assert applied.returncode == 0, applied.stderr
    assert (run_dir / "receipt.json").is_file()


def test_prepare_rejects_symlink_input(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    source = tmp_path / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = _run("prepare", "--input", str(link), "--run-dir", str(tmp_path / "run"))
    assert result.returncode == 1
    assert "symlink" in result.stderr.lower()


def test_collect_inputs_checks_symlink_before_resolution() -> None:
    module = _load_module()

    class Directory:
        def is_file(self) -> bool:
            return False

        def is_dir(self) -> bool:
            return True

        def rglob(self, _: str) -> list[object]:
            return [LinkedMarkdown()]

        def glob(self, _: str) -> list[object]:
            return [LinkedMarkdown()]

    class LinkedMarkdown:
        def is_symlink(self) -> bool:
            return True

        def __str__(self) -> str:
            return "linked.md"

    with pytest.raises(module.PipelineError, match="Symlink"):
        module._collect_inputs(Directory(), recursive=True)


def test_target_path_checks_symlink_component_before_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    target = tmp_path / "output" / "target.md"
    monkeypatch.setattr(module, "_has_symlink_component", lambda path: Path(path) == target)
    with pytest.raises(module.PipelineError, match="Symlink"):
        module._target_for(source, source, str(target), overwrite=True, rename=False)


def test_prepare_rejects_symlinked_markdown_inside_directory(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    real = docs / "real.md"
    real.write_text("# Real\n", encoding="utf-8")
    link = docs / "linked.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = _run("prepare", "--input", str(docs), "--run-dir", str(tmp_path / "run"))
    assert result.returncode == 1
    assert "symlink" in result.stderr.lower()


@pytest.mark.parametrize("target_kind", ["file", "directory"])
def test_prepare_rejects_symlink_target_or_target_parent(tmp_path: Path, target_kind: str) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Source\n", encoding="utf-8")
    if target_kind == "file":
        target_real = tmp_path / "real-target.md"
        target_real.write_text("# Existing\n", encoding="utf-8")
        target_link = tmp_path / "target.md"
        link_target = target_real
    else:
        target_real = tmp_path / "real-output"
        target_real.mkdir()
        target_link = tmp_path / "output"
        link_target = target_real
    try:
        target_link.symlink_to(link_target, target_is_directory=target_kind == "directory")
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = _run(
        "prepare",
        "--input",
        str(source),
        "--target",
        str(target_link),
        "--overwrite",
        "--run-dir",
        str(tmp_path / "run"),
    )
    assert result.returncode == 1
    assert "symlink" in result.stderr.lower()


def test_cortex_policy_context_states(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    module = _load_module()

    workspace = tmp_path / "workspace"
    bundle = workspace / "bundles" / "knowledge"
    policy_dir = bundle / "profiles" / "policy"
    policy_dir.mkdir(parents=True)
    rules = policy_dir / "rules.json"
    rules.write_text("{}\n", encoding="utf-8")
    rules_sha = hashlib.sha256(rules.read_bytes()).hexdigest()
    method = {
        "data": {
            "items": [
                {
                    "attributes": [
                        {"name": "version", "value": "2.1.0"},
                        {"name": "schema_count", "value": 18},
                        {"name": "target_actions", "value": ["fork", "seal", "activate", "rollback", "retire", "gc"]},
                    ]
                }
            ]
        }
    }
    target = {"id": "knowledge", "path": "bundles/knowledge", "content_digest": "sha256:" + "a" * 64}
    validation = {"data": {"target": target, "effective_policy": {"state": "valid", "digest": "sha256:policy", "manifest_path": "profiles/policy-package.json"}}}
    config = {
        "data": {
            "target": target,
            "items": [
                {
                    "attributes": [
                        {"name": "required_frontmatter_fields", "value": ["reviewed_by"]},
                        {"name": "files", "value": [{"role": "custom", "path": "profiles/policy/rules.json", "sha256": rules_sha}]},
                    ]
                }
            ],
        }
    }
    payloads = iter([method, validation, config])

    class Result:
        returncode = 0
        stderr = ""

        def __init__(self, payload: dict):
            self.stdout = json.dumps(payload)

    monkeypatch.setattr(module.shutil, "which", lambda _: "cortex")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result(next(payloads)))
    context = module.inspect_cortex_workspace(workspace)
    assert context["readiness"] == "cortex_policy_ready"
    assert context["required_fields"][-1] == "reviewed_by"
    assert context["policy_files"][0]["sha256"] == rules_sha


def test_cortex_cli_missing_and_incompatible_contract_are_hard_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_module()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    with pytest.raises(module.CortexError):
        module.inspect_cortex_workspace(workspace)

    class Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "data": {
                    "items": [
                        {
                            "attributes": [
                                {"name": "version", "value": "2.0.0"},
                                {"name": "schema_count", "value": 18},
                                {"name": "target_actions", "value": sorted(module.TARGET_ACTIONS)},
                            ]
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr(module.shutil, "which", lambda _: "cortex")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Result())
    with pytest.raises(module.CortexError):
        module.inspect_cortex_workspace(workspace)


@pytest.mark.parametrize("policy_changed", [True, False])
def test_apply_rechecks_policy_and_active_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, policy_changed: bool
) -> None:
    module = _load_module()
    source = tmp_path / "note.md"
    source.write_text("# Note\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    _, run = _prepare(source, run_dir, "--keep-workspace")
    proposal = _proposal(run, {"note.md": {"type": "Reference", "description": "A note."}})
    proposal_path = _write_json(tmp_path / "proposal.json", proposal)
    planned = _run("plan", "--state", str(run_dir / "run.json"), "--proposal", str(proposal_path))
    original_plan_path = Path(_last_json(planned)["plan_path"])
    plan = json.loads(original_plan_path.read_text(encoding="utf-8"))
    plan["workspace"] = str(tmp_path / "workspace")
    plan["policy_digest"] = "sha256:planned"
    plan.pop("plan_id")
    plan["plan_id"] = module._artifact_id("frontmatter-plan", plan)
    digest = plan["plan_id"].rsplit(":", 1)[1]
    plan_path = run_dir / "plans" / f"sha256-{digest}.json"
    _write_json(plan_path, plan)
    context = {
        "policy_digest": "sha256:changed" if policy_changed else "sha256:planned",
        "bundle_path": None if policy_changed else str(tmp_path),
    }
    monkeypatch.setattr(module, "inspect_cortex_workspace", lambda _: context)
    with pytest.raises(module.ConflictError, match="policy digest|active Cortex bundle"):
        module.apply_plan(plan_path)


def test_version_reports_ruamel_and_cortex_status() -> None:
    result = _run("--version")
    assert result.returncode == 0
    assert "okf-frontmatter 1.0.0" in result.stdout
    assert "ruamel.yaml" in result.stdout
    assert "cortex=" in result.stdout
