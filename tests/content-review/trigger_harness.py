#!/usr/bin/env python3
"""Isolated real-host trigger harness for the repository candidate description.

This is intentionally separate from pytest. It temporarily exposes the repo
description as a uniquely named slash command, substitutes that command into
explicit-invocation fixtures, and asks the real host to route each query. The
temporary command is removed after every run. No installed skill copy is
modified and the result must not be described as deployment/live-plugin proof.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


FIXTURE_SCHEMA = "1.0.0"
PLACEHOLDER = "{{candidate_command}}"
DEFAULT_WORKERS = 1  # multiple visible candidates can confound host routing


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def load_fixture(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != FIXTURE_SCHEMA:
        raise ValueError(f"fixture must use schema_version {FIXTURE_SCHEMA}")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("fixture.cases must be a non-empty list")
    ids: set[str] = set()
    for index, case in enumerate(cases):
        required = {"id", "query", "should_trigger", "category", "critical"}
        if not isinstance(case, dict) or set(case) != required:
            raise ValueError(f"cases[{index}] must contain exactly {sorted(required)}")
        if not all(isinstance(case[field], str) and case[field] for field in ("id", "query", "category")):
            raise ValueError(f"cases[{index}] has an empty/non-string id, query, or category")
        if not isinstance(case["should_trigger"], bool) or not isinstance(case["critical"], bool):
            raise ValueError(f"cases[{index}] boolean fields are invalid")
        if case["id"] in ids:
            raise ValueError(f"duplicate case id: {case['id']}")
        ids.add(case["id"])
        if case["category"] == "explicit_invocation" and PLACEHOLDER not in case["query"]:
            raise ValueError(f"explicit case {case['id']} must use {PLACEHOLDER}")
    return cases


def parse_skill_frontmatter(skill_md: Path) -> tuple[str, str]:
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"missing frontmatter in {skill_md}")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise ValueError(f"unterminated frontmatter in {skill_md}") from exc
    frontmatter = lines[1:end]
    name = ""
    description = ""
    for index, line in enumerate(frontmatter):
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"\'')
        if not line.startswith("description:"):
            continue
        marker = line.split(":", 1)[1].strip()
        if marker in {"|", "|-", ">", ">-"}:
            block: list[str] = []
            for following in frontmatter[index + 1:]:
                if following and not following[0].isspace():
                    break
                block.append(following[2:] if following.startswith("  ") else following.lstrip())
            if marker.startswith(">"):
                # Match YAML folded-scalar semantics used by SKILL.md: ordinary
                # wrapped lines become spaces while blank lines retain a break.
                paragraphs: list[str] = []
                current: list[str] = []
                for folded_line in block:
                    if folded_line:
                        current.append(folded_line)
                    elif current:
                        paragraphs.append(" ".join(current))
                        current = []
                if current:
                    paragraphs.append(" ".join(current))
                description = "\n".join(paragraphs).strip()
            else:
                description = "\n".join(block).strip()
        else:
            description = marker.strip('"\'')
        break
    if not name or not description:
        raise ValueError(f"frontmatter must contain non-empty name and description: {skill_md}")
    return name, description


def find_project_root(start: Path) -> Path:
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".claude").is_dir():
            return candidate
    raise ValueError("cannot find a project root containing .claude")


def substitute_query(case: dict[str, Any], candidate_command: str) -> str:
    query = case["query"].replace(PLACEHOLDER, candidate_command)
    if PLACEHOLDER in query:
        raise ValueError(f"placeholder substitution failed for {case['id']}")
    return query


def _reader(stream, output: queue.Queue[tuple[str, str | None]], channel: str) -> None:
    try:
        for line in iter(stream.readline, ""):
            output.put((channel, line))
    finally:
        output.put((channel, None))


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop the host and its children so Windows releases the scratch cwd."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _cleanup_scratch(path: Path) -> None:
    """Best-effort bounded cleanup; a locked OS temp path is not routing evidence."""
    for delay in (0.0, 0.2, 0.5, 1.0):
        if delay:
            time.sleep(delay)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return


def _event_decision(
    event: dict[str, Any], target: str, marker: str | None,
    state: dict[str, str | None],
):
    """Return True/False for a routing decision, None while more stream is needed."""
    def decide(value: bool, reason: str) -> bool:
        state["decision_reason"] = reason
        return value

    event_type = event.get("type")
    if event_type == "stream_event":
        stream_event = event.get("event", {})
        stream_type = stream_event.get("type")
        if stream_type == "content_block_start":
            block = stream_event.get("content_block", {})
            if block.get("type") == "tool_use":
                tool = block.get("name", "")
                if tool not in {"Skill", "Read"}:
                    return decide(False, f"first_tool:{tool or 'unknown'}")
                state["pending_tool"] = tool
                state["partial_json"] = ""
        elif stream_type == "content_block_delta":
            delta = stream_event.get("delta", {})
            if delta.get("type") == "text_delta":
                state["partial_text"] = (
                    (state.get("partial_text") or "") + delta.get("text", "")
                )[-4096:]
                if marker and marker in (state.get("partial_text") or ""):
                    return decide(True, "explicit_command_marker")
            elif delta.get("type") == "input_json_delta" and state.get("pending_tool"):
                state["partial_json"] = (state.get("partial_json") or "") + delta.get("partial_json", "")
                if target in (state.get("partial_json") or ""):
                    return decide(True, f"{state.get('pending_tool')}:target_detected")
        elif stream_type in {"content_block_stop", "message_stop"}:
            if state.get("pending_tool"):
                matched = target in (state.get("partial_json") or "")
                tool = state.get("pending_tool")
                state["pending_tool"] = None
                if matched:
                    return decide(True, f"{tool}:target_detected")
                return decide(False, f"{tool}:different_target")
            if stream_type == "message_stop":
                state["message_stopped"] = "true"
                if not state.get("negative_reason"):
                    state["negative_reason"] = "message_stop_without_target_tool"
                return None
    elif event_type == "assistant":
        text_items = [
            item.get("text", "") for item in event.get("message", {}).get("content", [])
            if item.get("type") == "text"
        ]
        if marker and marker in "".join(text_items):
            return decide(True, "explicit_command_marker")
        tool_items = [
            item for item in event.get("message", {}).get("content", [])
            if item.get("type") == "tool_use"
        ]
        if tool_items:
            item = tool_items[0]
            if item.get("name") not in {"Skill", "Read"}:
                return decide(False, f"first_tool:{item.get('name') or 'unknown'}")
            matched = target in json.dumps(item.get("input", {}), ensure_ascii=False)
            if matched:
                return decide(True, f"{item.get('name')}:target_detected")
            return decide(False, f"{item.get('name')}:different_target")
    elif event_type == "result":
        if event.get("is_error"):
            raise RuntimeError(str(event.get("result") or "host returned an error result"))
        if marker and marker in str(event.get("result") or ""):
            return decide(True, "explicit_command_marker")
        return decide(
            False, state.get("negative_reason") or "result_without_target_tool"
        )
    return None


def run_once(
    *, case: dict[str, Any], run_number: int, skill_name: str, description: str,
    project_root: Path, host: str, timeout: int, model: str | None,
) -> dict[str, Any]:
    token = uuid.uuid4().hex[:10]
    command_name = f"{skill_name}-candidate-{token}"
    candidate_command = f"/{command_name}"
    is_explicit = case["category"] == "explicit_invocation"
    explicit_marker = f"CR_EXPLICIT_{token.upper()}" if is_explicit else None
    query_text = substitute_query(case, candidate_command)
    command_dir = project_root / ".claude" / "commands"
    command_path = command_dir / f"{command_name}.md"
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    stderr_lines: list[str] = []
    try:
        command_dir.mkdir(parents=True, exist_ok=True)
        indented = "\n  ".join(description.splitlines())
        body = f"This skill handles: {description}\n"
        if is_explicit:
            body = (
                "Candidate trigger-description evaluation only. If this command was "
                "expanded by an explicit slash invocation, respond with exactly "
                f"`{explicit_marker}` and do not use tools.\n"
            )
        command_path.write_text(
            f"---\ndescription: |\n  {indented}\n---\n\n"
            f"# {skill_name}\n\n{body}", encoding="utf-8",
        )
        executable = shutil.which(host) or (host if Path(host).exists() else None)
        if not executable:
            raise RuntimeError(f"host executable not found: {host}")
        command = [
            executable, "-p", query_text, "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--setting-sources", "project", "--no-session-persistence",
            "--no-chrome",
        ]
        if model:
            command += ["--model", model]
        env = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
        process = subprocess.Popen(
            command, cwd=project_root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None and process.stderr is not None
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        threading.Thread(target=_reader, args=(process.stdout, events, "stdout"), daemon=True).start()
        threading.Thread(target=_reader, args=(process.stderr, events, "stderr"), daemon=True).start()
        state: dict[str, str | None] = {
            "pending_tool": None,
            "partial_json": "",
            "partial_text": "",
            "decision_reason": None,
            "negative_reason": None,
            "message_stopped": None,
            "actual_model": None,
        }
        parsed_events = 0
        decision: bool | None = None
        stdout_closed = stderr_closed = False
        while time.monotonic() - started < timeout:
            try:
                channel, line = events.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None and stdout_closed and stderr_closed:
                    break
                continue
            if line is None:
                stdout_closed = stdout_closed or channel == "stdout"
                stderr_closed = stderr_closed or channel == "stderr"
                if process.poll() is not None and stdout_closed and stderr_closed:
                    break
                continue
            if channel == "stderr":
                stderr_lines.append(line.rstrip())
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed_events += 1
            if event.get("type") == "system" and event.get("subtype") == "init":
                state["actual_model"] = str(event.get("model") or "") or None
            decision = _event_decision(event, command_name, explicit_marker, state)
            if decision is not None:
                break
        if decision is None and process.poll() is None:
            detail = " | ".join(stderr_lines[-5:]).strip()
            diagnostic = (
                f"; parsed_events={parsed_events}"
                f"; negative_reason={state.get('negative_reason') or 'none'}"
            )
            if detail:
                diagnostic += f"; stderr={detail}"
            raise TimeoutError(
                f"host did not produce a routing decision within {timeout}s{diagnostic}"
            )
        if decision is None:
            return_code = process.wait(timeout=5)
            if return_code != 0 or parsed_events == 0:
                detail = "\n".join(stderr_lines[-10:]).strip()
                raise RuntimeError(
                    f"host exited {return_code} without a routing decision"
                    + (f": {detail}" if detail else "")
                )
            decision = False
            state["decision_reason"] = (
                state.get("negative_reason") or "host_exited_without_target_tool"
            )
        outcome = "triggered" if decision else "not_triggered"
        return {
            "case_id": case["id"], "run": run_number, "outcome": outcome,
            "triggered": decision, "candidate_command": candidate_command,
            "query": query_text, "duration_seconds": round(time.monotonic() - started, 3),
            "decision_reason": state.get("decision_reason"),
            "actual_model": state.get("actual_model"), "error": None,
        }
    except Exception as exc:  # infrastructure failures must never become routing false
        return {
            "case_id": case["id"], "run": run_number,
            "outcome": "infrastructure_error", "triggered": None,
            "candidate_command": candidate_command, "query": query_text,
            "duration_seconds": round(time.monotonic() - started, 3),
            "decision_reason": None,
            "actual_model": state.get("actual_model") if 'state' in locals() else None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if process is not None:
            _terminate_process_tree(process)
        command_path.unlink(missing_ok=True)


def summarize(cases: list[dict[str, Any]], runs: list[dict[str, Any]], runs_per_query: int,
              threshold: float) -> dict[str, Any]:
    by_case = {case["id"]: [] for case in cases}
    for run in runs:
        by_case[run["case_id"]].append(run)
    results = []
    for case in cases:
        case_runs = sorted(by_case[case["id"]], key=lambda item: item["run"])
        infra = [item for item in case_runs if item["outcome"] == "infrastructure_error"]
        triggered = sum(item["triggered"] is True for item in case_runs)
        decisive = len(case_runs) - len(infra)
        if infra or decisive != runs_per_query:
            passed = False
            status = "infrastructure_error"
        elif case["critical"]:
            passed = triggered == (runs_per_query if case["should_trigger"] else 0)
            status = "pass" if passed else "routing_failure"
        else:
            rate = triggered / runs_per_query
            passed = rate >= threshold if case["should_trigger"] else rate < threshold
            status = "pass" if passed else "routing_failure"
        results.append({
            **case, "status": status, "pass": passed, "triggers": triggered,
            "runs": runs_per_query, "infrastructure_errors": len(infra),
            "run_results": case_runs,
        })
    return {
        "evidence_scope": "candidate description in isolated real-host harness; not installed-plugin live routing",
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(item["pass"] for item in results),
            "routing_failures": sum(item["status"] == "routing_failure" for item in results),
            "infrastructure_errors": sum(item["status"] == "infrastructure_error" for item in results),
        },
    }


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=here / "fixtures" / "trigger_eval.json")
    parser.add_argument("--skill-path", type=Path,
                        default=here.parent.parent / "skills" / "content-review")
    parser.add_argument(
        "--project-root", type=Path,
        help=("Diagnostic override. By default the harness creates an empty "
              "temporary project so installed/project skills cannot compete."),
    )
    parser.add_argument("--host", default="claude")
    parser.add_argument("--model")
    parser.add_argument("--runs-per-query", type=positive_int, default=3)
    parser.add_argument(
        "--diagnostic", action="store_true",
        help="Allow a non-gating run count other than the required three",
    )
    parser.add_argument(
        "--workers", type=int, choices=(1,), default=DEFAULT_WORKERS,
        help="Must remain 1 so each candidate command is evaluated in isolation",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--critical-only", action="store_true")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs_per_query != 3 and not args.diagnostic:
        parser.error(
            "gating verification requires --runs-per-query 3; "
            "use --diagnostic for a non-gating smoke run"
        )

    cases = load_fixture(args.fixture)
    if args.critical_only:
        cases = [case for case in cases if case["critical"]]
    if args.case_ids:
        selected = set(args.case_ids)
        cases = [case for case in cases if case["id"] in selected]
        missing = selected - {case["id"] for case in cases}
        if missing:
            raise ValueError(f"unknown case id(s): {', '.join(sorted(missing))}")
    skill_name, description = parse_skill_frontmatter(args.skill_path / "SKILL.md")
    if args.prepare_only:
        output = {
            "schema": "TriggerHarnessPreparation/v1", "skill_name": skill_name,
            "description": description, "cases": cases,
        }
    else:
        scratch: Path | None = None
        if args.project_root:
            project_root = args.project_root.resolve()
        else:
            scratch = Path(tempfile.mkdtemp(prefix="content-review-trigger-"))
            project_root = scratch
            claude_dir = project_root / ".claude"
            claude_dir.mkdir(parents=True)
            (claude_dir / "settings.json").write_text("{}\n", encoding="utf-8")
        try:
            futures = []
            runs: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                for case in cases:
                    for run_number in range(1, args.runs_per_query + 1):
                        futures.append(executor.submit(
                            run_once, case=case, run_number=run_number,
                            skill_name=skill_name, description=description,
                            project_root=project_root, host=args.host,
                            timeout=args.timeout, model=args.model,
                        ))
                for future in as_completed(futures):
                    runs.append(future.result())
        finally:
            if scratch is not None:
                _cleanup_scratch(scratch)
        output = {
            "schema": "TriggerHarnessResult/v1", "skill_name": skill_name,
            "description": description, "host": args.host,
            "model": args.model or "host-default",
            "setting_sources": ["project"],
            "verification_mode": "diagnostic" if args.diagnostic else "gating",
            "gating_eligible": not args.diagnostic and args.runs_per_query == 3,
            **summarize(cases, runs, args.runs_per_query, args.threshold),
        }
        output["observed_models"] = sorted({
            run["actual_model"] for run in runs if run.get("actual_model")
        })
        output["evidence_scope"] = (
            "candidate description in an empty temporary real-host project with "
            "project-only settings; not installed-plugin live routing"
            if not args.project_root else
            "candidate description in a caller-selected real-host project with "
            "project-only settings; diagnostic only, not isolated deployment proof"
        )
        if args.diagnostic:
            output["evidence_scope"] += "; non-gating diagnostic run"
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if args.prepare_only:
        return 0
    if output["summary"]["infrastructure_errors"]:
        return 2
    return 0 if output["summary"]["routing_failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
