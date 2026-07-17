"""Contract tests for the ReviewRun/v4 content-review planner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent / "skills" / "content-review" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import review_plan  # noqa: E402
import trigger_harness  # noqa: E402


CONFIG_PATH = HERE / "fixtures" / "test_config.json"
SCRIPT = [sys.executable, str(SCRIPTS / "review_plan.py")]
CONFIG_ARG = ["--config", str(CONFIG_PATH)]


def run_cli(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        SCRIPT + CONFIG_ARG + list(args), input=input_text, text=True,
        encoding="utf-8", capture_output=True,
    )


def plan_run(
    tmp_path: Path, text: str, *, focus: str = "all", reference: str | None = None,
    chunk_lines: int = 400, keep: bool = True,
) -> tuple[Path, dict, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.md"
    source.write_text(text, encoding="utf-8")
    workspace = tmp_path / "workspace"
    args = ["plan", "--input", str(source), "--focus", focus, "--chunk-lines", str(chunk_lines), "--workspace", str(workspace)]
    if reference is not None:
        ref = tmp_path / "reference.md"
        ref.write_text(reference, encoding="utf-8")
        args.extend(["--references", str(ref)])
    if keep:
        args.append("--keep-workspace")
    result = run_cli(*args)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    return Path(payload["state"]), payload, source


def read_state(state: Path) -> dict:
    return json.loads(state.read_text(encoding="utf-8"))


def task_map(payload: dict) -> dict[str, dict]:
    return {task["cell_id"]: task for task in payload.get("ready", [])}


def local_payload(task: dict, *, findings: list | None = None, observations: list | None = None) -> dict:
    return {
        "checks_completed": task["checks"],
        "findings": findings or [],
        "observations": observations or [],
    }


def envelope(task: dict, payload: dict, *, generation: int | None = None, dependency_hash: str | None = None) -> dict:
    return {
        "cell_id": task["cell_id"],
        "dispatch_id": task["dispatch_id"],
        "state_generation": task["state_generation"] if generation is None else generation,
        "dependency_hash": task["dependency_hash"] if dependency_hash is None else dependency_hash,
        "payload": payload,
    }


def ingest(tmp_path: Path, state: Path, entries: list[dict]) -> subprocess.CompletedProcess[str]:
    results = tmp_path / f"results-{len(list(tmp_path.glob('results-*.json'))):03d}.json"
    results.write_text(json.dumps({"results": entries}, ensure_ascii=False), encoding="utf-8")
    return run_cli("ingest", "--state", str(state), "--results", str(results))


def accept_initial(tmp_path: Path, state: Path, plan: dict, *, claims: list[dict] | None = None, observations: list | None = None) -> dict:
    entries = []
    for task in plan["ready"]:
        if task["stage"] == "local":
            entries.append(envelope(task, local_payload(task, observations=observations)))
        elif task["stage"] == "claim":
            entries.append(envelope(task, {"claims": claims or []}))
        else:
            raise AssertionError(task)
    result = ingest(tmp_path, state, entries)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def semantic_payload(state: Path, task: dict, *, status: str = "supported", facts: bool = True) -> dict:
    cell = read_state(state)["cells"][task["cell_id"]]
    assessments = [
        {"claim_id": claim_id, "passage_id": passage_id, "status": status, "evidence": []}
        for claim_id in cell["input"]["claim_ids"]
        for passage_id in cell["input"]["passage_ids"]
    ]
    reference_facts = []
    if facts:
        reference_facts = [
            {"passage_id": passage_id, "quote": "Reference fact", "summary": "A verified fact"}
            for passage_id in cell["input"]["passage_ids"]
        ]
    return {"assessments": assessments, "reference_facts": reference_facts}


class TestRoutingAndCoreHelpers:
    def test_frontmatter_preserves_dual_trigger_and_exclusions(self):
        skill = HERE.parent.parent / "skills" / "content-review" / "SKILL.md"
        name, description = trigger_harness.parse_skill_frontmatter(skill)
        lowered = description.lower()
        assert name == "content-review"
        assert "only when both conditions hold" in lowered
        assert "human-facing, non-code prose" in lowered
        for excluded in ("source code", "prs or diffs", "tests", "configs", "logs", "apis", "skill.md", "agents.md", "prompts"):
            assert excluded in lowered

    def test_completion_handoff_contract(self):
        skill_dir = HERE.parent.parent / "skills" / "content-review"
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        lowered = text.lower()

        assert "## completion handoff" in lowered
        for required in (
            "reviewassembly/v4",
            "status: complete",
            "complete: true",
            "status: partial",
            "incomplete: true",
            "legacy_single_agent",
            "outside the rendered report",
            "explicitly selects",
            "acknowledgement or thanks is not authorization",
            "non-zero exit",
        ):
            assert required in lowered

        template = (skill_dir / "assets" / "report-template.md").read_text(encoding="utf-8")
        assert "completion handoff" not in template.lower()

    def test_chunking_keeps_fences_and_tables_atomic(self):
        text = "intro\n\n```python\n" + "\n".join(f"x{i}" for i in range(30)) + "\n```\n\n| h |\n|---|\n| v |"
        chunks = review_plan.chunk_text(text, 5)
        assert all(sum(line.startswith("```") for line in chunk["text"].splitlines()) % 2 == 0 for chunk in chunks)
        assert sum("| h |" in chunk["text"] for chunk in chunks) == 1

    def test_v4_constants_and_cli_surface(self):
        assert review_plan.RUN_SCHEMA == "ReviewRun/v4"
        assert review_plan.MAX_REDUCER_INPUT_CHARS == 50_000
        assert review_plan.MAX_REDUCER_OUTPUT_CHARS == 10_000
        assert review_plan.RESULT_MAX_ATTEMPTS == 3
        help_result = run_cli("--help")
        assert help_result.returncode == 0
        assert "{plan,ingest,status,assemble}" in help_result.stdout
        assert "validate-cell" not in help_result.stdout


class TestSlimPlanning:
    def test_single_chunk_grammar_only_has_one_llm_cell(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "One sentence.", focus="grammar")
        assert len(plan["ready"]) == 1
        assert plan["ready"][0]["stage"] == "local"
        assert plan["ready"][0]["checks"] == ["grammar"]
        assert not list((state.parent / "artifacts").glob("source.md"))
        assert not (state.parent / "attempts.json").exists()

    def test_single_chunk_all_has_two_local_cells(self, tmp_path):
        _, plan, _ = plan_run(tmp_path, "One sentence.", focus="all")
        assert len(plan["ready"]) == 2
        assert {tuple(task["checks"]) for task in plan["ready"]} == {("grammar", "style"), ("logic", "consistency")}

    def test_reference_baseline_reaches_five_cells_without_adjudication(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "The launch is in June.", reference="The launch is in June.")
        after_initial = accept_initial(tmp_path, state, plan, claims=[{"text": "Launch date"}])
        semantic = after_initial["ready"]
        assert len(semantic) == 1 and semantic[0]["stage"] == "semantic"
        result = ingest(tmp_path, state, [envelope(semantic[0], semantic_payload(state, semantic[0]))])
        assert result.returncode == 0, result.stderr
        after_semantic = json.loads(result.stdout)
        assert len(read_state(state)["cells"]) == 5  # 2 local + claim + semantic + coverage
        assert [task["stage"] for task in after_semantic["ready"]] == ["coverage"]

    def test_multichunk_grammar_skips_global_reducer(self, tmp_path):
        text = "first\n\nsecond\n\nthird\n\nfourth"
        state, plan, _ = plan_run(tmp_path, text, focus="grammar", chunk_lines=1)
        after = accept_initial(tmp_path, state, plan)
        assert after["ready"] == []
        assert all(cell["stage"] != "global" for cell in read_state(state)["cells"].values())

    def test_local_observations_create_dynamic_global_after_all_local_results(self, tmp_path):
        text = "Alpha\n\nBeta\n\nGamma\n\nDelta"
        state, plan, _ = plan_run(tmp_path, text, focus="all", chunk_lines=1)
        observation = {"kind": "term", "value": "Alpha"}
        after = accept_initial(tmp_path, state, plan, observations=[observation])
        assert after["ready"]
        assert all(task["stage"] == "global" for task in after["ready"])
        cells = read_state(state)["cells"]
        assert all(cells[task["cell_id"]]["input"]["serialized_chars"] <= 50_000 for task in after["ready"])


class TestReferenceDAG:
    def test_zero_claims_runs_facts_only_then_coverage(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "An introduction only.", reference="Important reference fact.")
        after_initial = accept_initial(tmp_path, state, plan, claims=[])
        assert len(after_initial["ready"]) == 1
        facts_only = after_initial["ready"][0]
        assert facts_only["stage"] == "semantic"
        assert facts_only["dimension"] == "facts-only"
        result = ingest(tmp_path, state, [envelope(facts_only, semantic_payload(state, facts_only))])
        assert result.returncode == 0
        final = json.loads(result.stdout)
        assert [task["stage"] for task in final["ready"]] == ["coverage"]
        assert read_state(state)["coverage"]["expected_matrix"] == []

    def test_adjudication_is_scheduled_only_for_grounding_candidates(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "The launch is in June.", reference="The launch is in July.")
        after_initial = accept_initial(tmp_path, state, plan, claims=[{"text": "Launch month"}])
        semantic = after_initial["ready"][0]
        result = ingest(tmp_path, state, [envelope(semantic, semantic_payload(state, semantic, status="contradicted"))])
        assert result.returncode == 0, result.stderr
        stages = {task["stage"] for task in json.loads(result.stdout)["ready"]}
        assert stages == {"coverage", "adjudication"}

    def test_failed_semantic_batch_marks_claim_grounding_unverified(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "The launch is in June.", reference="The launch is in July.")
        after_initial = accept_initial(tmp_path, state, plan, claims=[{"text": "Launch month"}])
        task = after_initial["ready"][0]
        for attempt in range(3):
            result = ingest(tmp_path, state, [envelope(task, {"assessments": [], "reference_facts": []})])
            assert result.returncode == 0
            if attempt < 2:
                task = json.loads(result.stdout)["ready"][0]
        grounding = read_state(state)["coverage"]["groundings"]
        assert grounding and grounding[0]["status"] == "unverified/incomplete"

    def test_semantic_matrix_is_complete_unique_and_budgeted(self, tmp_path):
        reference = "A" * 19_000 + "\n\n" + "B" * 19_000
        state, plan, _ = plan_run(tmp_path, "Claim one. Claim two.", reference=reference)
        claims = [{"text": "one"}, {"text": "two"}]
        after_initial = accept_initial(tmp_path, state, plan, claims=claims)
        tasks = after_initial["ready"]
        assert tasks and all(task["stage"] == "semantic" for task in tasks)
        state_data = read_state(state)
        matrix = state_data["coverage"]["expected_matrix"]
        assert len(matrix) == len({tuple(pair) for pair in matrix}) == 4
        for task in tasks:
            assert state_data["cells"][task["cell_id"]]["input"]["serialized_chars"] <= 50_000
        result = ingest(tmp_path, state, [envelope(task, semantic_payload(state, task)) for task in tasks])
        assert result.returncode == 0, result.stderr

    def test_claim_overflow_splits_at_structure_boundary_without_retry(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "First paragraph.\n\nSecond paragraph.", reference="Reference.")
        claim_task = next(task for task in plan["ready"] if task["stage"] == "claim")
        huge = {"claims": [{"text": "x" * 10_100}]}
        result = ingest(tmp_path, state, [envelope(claim_task, huge)])
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output["outcomes"][0]["status"] == "split"
        split = read_state(state)["cells"][claim_task["cell_id"]]
        assert split["status"] == "split" and split["attempts"] == []
        assert len([task for task in output["ready"] if task["stage"] == "claim"]) == 2

    def test_unsplittable_claim_block_is_capacity_error(self, tmp_path):
        source = tmp_path / "source.md"
        source.write_text("```\n" + "x" * 20_001 + "\n```", encoding="utf-8")
        reference = tmp_path / "ref.md"
        reference.write_text("reference", encoding="utf-8")
        result = run_cli("plan", "--input", str(source), "--references", str(reference), "--workspace", str(tmp_path / "workspace"))
        assert result.returncode == 2
        assert "cannot be split" in result.stderr

    def test_oversized_reference_line_is_rejected_during_plan(self, tmp_path):
        source = tmp_path / "source.md"
        reference = tmp_path / "reference.md"
        workspace = tmp_path / "workspace"
        source.write_text("A short source claim.", encoding="utf-8")
        reference.write_text("x" * 50_001, encoding="utf-8")
        result = run_cli(
            "plan", "--input", str(source), "--references", str(reference),
            "--workspace", str(workspace),
        )
        assert result.returncode == 2
        assert "Reference line 1 exceeds" in result.stderr
        assert not (workspace / "run-state.json").exists()

    def test_dynamic_cell_cap_stops_split_growth(self, tmp_path, monkeypatch):
        monkeypatch.setattr(review_plan, "HARD_MAX_CELLS_PER_STAGE", 2)
        source = tmp_path / "source.md"
        reference = tmp_path / "reference.md"
        source.write_text("one\n\ntwo", encoding="utf-8")
        reference.write_text("reference", encoding="utf-8")
        args = type("Args", (), {
            "input": str(source), "workspace": str(tmp_path / "workspace"), "references": [str(reference)],
            "focus": "grammar", "language": "auto", "chunk_lines": 400, "keep_workspace": True,
        })()
        run, _ = review_plan._v4_new_run(args)
        claim = next(cell for cell in run["cells"].values() if cell["stage"] == "claim")
        with pytest.raises(review_plan._V4CapacityError):
            review_plan._v4_split_claim_cell(run, claim)


class TestIngestStateContract:
    def test_identical_duplicate_is_idempotent_and_different_payload_conflicts(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "Simple text.", focus="grammar")
        task = plan["ready"][0]
        item = envelope(task, local_payload(task))
        assert ingest(tmp_path, state, [item]).returncode == 0
        duplicate = ingest(tmp_path, state, [item])
        assert duplicate.returncode == 0
        assert json.loads(duplicate.stdout)["outcomes"][0]["status"] == "idempotent"
        conflict_item = envelope(task, local_payload(task, observations=[{"kind": "term", "value": "x"}]))
        conflict = ingest(tmp_path, state, [conflict_item])
        assert conflict.returncode == 0
        assert json.loads(conflict.stdout)["outcomes"][0]["status"] == "conflict"

    def test_stale_generation_and_dependency_do_not_consume_retry(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "Simple text.", focus="grammar")
        task = plan["ready"][0]
        stale = envelope(task, local_payload(task), generation=0, dependency_hash="stale")
        result = ingest(tmp_path, state, [stale])
        assert result.returncode == 0
        assert json.loads(result.stdout)["outcomes"][0]["status"] == "stale"
        cell = read_state(state)["cells"][task["cell_id"]]
        assert cell["attempts"] == [] and cell["status"] == "dispatched"

    def test_partial_batch_accepts_valid_result_and_retries_only_invalid_one(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "Simple text.", focus="all")
        first, second = plan["ready"]
        invalid = envelope(second, {"checks_completed": [], "findings": [], "observations": []})
        result = ingest(tmp_path, state, [envelope(first, local_payload(first)), invalid])
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        state_data = read_state(state)
        assert state_data["cells"][first["cell_id"]]["status"] == "accepted"
        assert state_data["cells"][second["cell_id"]]["attempts"]
        retry = next(task for task in output["ready"] if task["cell_id"] == second["cell_id"])
        assert retry["dispatch_id"] != second["dispatch_id"]

    def test_true_invalid_payload_fails_after_initial_plus_two_retries(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "Simple text.", focus="grammar")
        task = plan["ready"][0]
        for attempt in range(3):
            result = ingest(tmp_path, state, [envelope(task, {"checks_completed": [], "findings": [], "observations": []})])
            assert result.returncode == 0
            if attempt < 2:
                task = json.loads(result.stdout)["ready"][0]
        cell = read_state(state)["cells"][task["cell_id"]]
        assert cell["status"] == "failed" and len(cell["attempts"]) == 3

    def test_primary_corruption_recovers_backup_and_double_corruption_stops(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "Simple text.", focus="grammar")
        assert ingest(tmp_path, state, [envelope(plan["ready"][0], local_payload(plan["ready"][0]))]).returncode == 0
        backup = Path(str(state) + ".bak")
        assert backup.exists()
        state.write_text("not json", encoding="utf-8")
        recovered = run_cli("status", "--state", str(state))
        assert recovered.returncode == 0
        assert json.loads(recovered.stdout)["recovered_from_backup"] is True
        backup.write_text("also not json", encoding="utf-8")
        broken = run_cli("status", "--state", str(state))
        assert broken.returncode == 1
        assert "state_corrupt" in broken.stderr

    def test_v3_workspace_is_explicitly_rejected(self, tmp_path):
        old = tmp_path / "old.json"
        old.write_text(json.dumps({"schema": "ReviewPlan/v3"}), encoding="utf-8")
        result = run_cli("status", "--state", str(old))
        assert result.returncode == 1
        assert "re-run" in result.stderr


class TestAssemblyIntegrityAndCleanup:
    def test_status_does_not_revalidate_but_assemble_detects_tampered_source(self, tmp_path):
        state, plan, source = plan_run(tmp_path, "Original text.", focus="grammar")
        assert ingest(tmp_path, state, [envelope(plan["ready"][0], local_payload(plan["ready"][0]))]).returncode == 0
        source.write_text("Changed text.", encoding="utf-8")
        assert run_cli("status", "--state", str(state)).returncode == 0
        assembled = run_cli("assemble", "--state", str(state), "--keep-workspace")
        assert assembled.returncode == 1
        assert "hash changed" in assembled.stderr

    def test_complete_stdout_assembly_cleans_workspace(self, tmp_path):
        state, plan, _ = plan_run(tmp_path, "Simple text.", focus="grammar", keep=False)
        assert ingest(tmp_path, state, [envelope(plan["ready"][0], local_payload(plan["ready"][0]))]).returncode == 0
        assembled = run_cli("assemble", "--state", str(state))
        assert assembled.returncode == 0, assembled.stderr
        assert json.loads(assembled.stdout)["complete"] is True
        assert not state.parent.exists()

    def test_partial_failure_output_error_and_keep_workspace_all_retain_state(self, tmp_path):
        partial_state, _, _ = plan_run(tmp_path / "partial", "Simple text.", focus="grammar", keep=False)
        partial = run_cli("assemble", "--state", str(partial_state), "--accept-partial")
        assert partial.returncode == 0 and partial_state.parent.exists()

        failed_state, failed_plan, _ = plan_run(tmp_path / "failed", "Simple text.", focus="grammar", keep=False)
        assert ingest(tmp_path / "failed", failed_state, [envelope(failed_plan["ready"][0], local_payload(failed_plan["ready"][0]))]).returncode == 0
        output_dir = tmp_path / "existing-directory"
        output_dir.mkdir()
        output_failure = run_cli("assemble", "--state", str(failed_state), "--output", str(output_dir))
        assert output_failure.returncode == 1 and failed_state.parent.exists()

        kept_state, kept_plan, _ = plan_run(tmp_path / "kept", "Simple text.", focus="grammar", keep=True)
        assert ingest(tmp_path / "kept", kept_state, [envelope(kept_plan["ready"][0], local_payload(kept_plan["ready"][0]))]).returncode == 0
        assert run_cli("assemble", "--state", str(kept_state)).returncode == 0
        assert kept_state.parent.exists()

    def test_report_fields_diff_zero_findings_and_partial_warning(self, tmp_path):
        state, plan, _ = plan_run(tmp_path / "finding", "teh wording", focus="grammar", keep=True)
        task = plan["ready"][0]
        finding = {
            "category": "spelling", "locations": [{"start_line": 1, "end_line": 1}],
            "original_text": "teh", "revised_text": "the", "change": "Replace teh with the", "reason": "Spelling error",
        }
        assert ingest(tmp_path / "finding", state, [envelope(task, local_payload(task, findings=[finding]))]).returncode == 0
        report = run_cli("assemble", "--state", str(state), "--diff", "--keep-workspace")
        assert report.returncode == 0
        data = json.loads(report.stdout)
        assert data["diff_status"] == "generated"
        assert all(label in data["report"] for label in ("Original location", "Original text", "Revised text", "Exact change", "Reason for change"))

        zero_state, zero_plan, _ = plan_run(tmp_path / "zero", "Correct wording", focus="grammar", keep=True)
        assert ingest(tmp_path / "zero", zero_state, [envelope(zero_plan["ready"][0], local_payload(zero_plan["ready"][0]))]).returncode == 0
        zero = run_cli("assemble", "--state", str(zero_state), "--keep-workspace")
        assert "No issues requiring changes were found." in json.loads(zero.stdout)["report"]

        partial_state, _, _ = plan_run(tmp_path / "warning", "Incomplete", focus="grammar", keep=True)
        partial = run_cli("assemble", "--state", str(partial_state), "--accept-partial", "--keep-workspace")
        assert "Review incomplete." in json.loads(partial.stdout)["report"]
