#!/usr/bin/env python3
"""Deterministic planner, validator, status reporter, and assembler.

The v3 protocol makes orchestration inspectable: every run has immutable input
hashes, every required local/global/reference task is an explicit DAG cell, and
only strictly validated cell results are assembled.  The script deliberately
does not dispatch agents; it supplies the deterministic contract around them.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from typing import Any, NoReturn
from urllib.parse import urlsplit


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


# Package/release version remains unchanged; the wire protocols evolve
# independently and are identified by their schema constants below.
VERSION = "2.0.0"
PROTOCOL_VERSION = "3.0.0"
PLAN_SCHEMA = "ReviewPlan/v3"
CELL_SCHEMA = "CellResult/v3"
CELL_RESULT_SCHEMA = CELL_SCHEMA
OBSERVATION_SCHEMA = "ReducerObservation/v1"
EVIDENCE_SCHEMA = "ReferenceEvidence/v1"

MAX_REDUCER_INPUT_CHARS = 50_000
MAX_REDUCER_DEPTH = 3
MAX_REDUCER_OUTPUT_CHARS = 10_000
MAX_CHUNK_CHARS = 50_000
HARD_MAX_CHUNKS = 20
HARD_MAX_CELLS_PER_STAGE = 60
RESULT_MAX_ATTEMPTS = 3  # initial attempt + two retries
ATTEMPT_LOCK_TIMEOUT_SECONDS = 10.0
ATTEMPT_LOCK_STALE_SECONDS = 60.0

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
TEMPLATE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "report-template.md")
)

DEFAULT_CONFIG = {"chunk_lines": 400, "max_chunks": 20, "max_cells": 60}
FENCE_RE = re.compile(r"^\s*(```|~~~)")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

FOCUS_CHECKS = {
    "grammar": ["grammar"],
    "style": ["style"],
    "logic": ["logic"],
    "consistency": ["consistency"],
    "all": ["grammar", "style", "logic", "consistency"],
}
CHECK_DIMENSION = {
    "grammar": "grammar-style",
    "style": "grammar-style",
    "logic": "logic-consistency",
    "consistency": "logic-consistency",
}
REQUIRED_DIMENSIONS = {
    "grammar-style",
    "logic-consistency",
    "document-global",
    "claim-extraction",
    "passage-index",
    "claim-grounding",
    "reference-coverage",
    "reference-adjudication",
    # v2 compatibility
    "fact-check",
}
ALLOWED_STAGES = {"local", "global", "reference"}
ALLOWED_OBSERVATION_KINDS = {"term", "entity", "number", "date", "format", "claim"}
ALLOWED_SEVERITIES = {"high", "medium", "low"}
ALLOWED_CATEGORIES = {
    "spelling", "grammar", "style", "logic", "consistency", "fact",
    "contradiction", "unsupported", "omission",
}
ASSESSMENT_STATUSES = {
    "supported", "contradicted", "no-basis", "not-established", "unverified/incomplete"
}
SUPPORTED_LOCAL_EXTENSIONS = {".md", ".markdown", ".txt", ".html", ".htm", ".docx", ".pdf"}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

CHECK_FINDING_CATEGORIES = {
    "grammar": {"spelling", "grammar"},
    "style": {"style"},
    "logic": {"logic"},
    "consistency": {"consistency"},
}


def die(message: str, code: int = 1) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _is_url(value: str) -> bool:
    return bool(URL_RE.match(value or ""))


def _extension(value: str) -> str:
    target = urlsplit(value).path if _is_url(value) else value
    return os.path.splitext(target)[1].lower()


def _display_name(value: str) -> str:
    target = urlsplit(value).path if _is_url(value) else value
    return os.path.basename(target.rstrip("/")) or "source"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _json_hash(value: Any) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(blob)


def load_config() -> dict[str, Any]:
    """Load the configured caps, creating the conventional local config if absent."""
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(DEFAULT_CONFIG, handle, indent=2, ensure_ascii=False)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: config.json parse error ({exc}); using defaults", file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    if isinstance(loaded, dict):
        merged.update(loaded)
    return merged


def _ensure_markitdown():
    try:
        from markitdown import MarkItDown
        return MarkItDown
    except ImportError:
        import subprocess
        print("Installing markitdown...", file=sys.stderr)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "markitdown"])
        from markitdown import MarkItDown
        return MarkItDown


def convert_to_markdown(source: str) -> str:
    result = _ensure_markitdown()().convert(source)
    return result.text_content


def read_source(source: str) -> str:
    """Read/convert one input. URL recognition intentionally precedes abspath."""
    ext = _extension(source)
    if ext == ".rtf":
        die("RTF is unsupported: the installed MarkItDown does not reliably convert RTF control codes.")
    if _is_url(source):
        return convert_to_markdown(source)
    path = os.path.abspath(source)
    if not os.path.isfile(path):
        die(f"File not found: {path}")
    if ext not in SUPPORTED_LOCAL_EXTENSIONS:
        die(
            f"Unsupported input type {ext or '[no extension]'}. Supported types: "
            + ", ".join(sorted(SUPPORTED_LOCAL_EXTENSIONS))
        )
    if ext in {".md", ".markdown", ".txt"}:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except UnicodeDecodeError:
            die(f"Text input is not valid UTF-8: {path}")
    return convert_to_markdown(path)


def _resolve_input(value: str) -> tuple[str, str]:
    if _is_url(value):
        return value, "url"
    return os.path.abspath(value), "file"


def classify_input(value: str) -> dict[str, str]:
    """Classify an input without accidentally normalizing URLs as file paths."""
    resolved, kind = _resolve_input(value)
    output = {"kind": kind, "original": value}
    if kind == "file":
        output["local_path"] = resolved
    return output


def _original_input_hash(value: str, canonical_text: str) -> str:
    if _is_url(value):
        return _sha256_text(canonical_text)
    try:
        with open(value, "rb") as handle:
            return _sha256_bytes(handle.read())
    except OSError:
        return _sha256_text(canonical_text)


def _expand_references(values: list[str] | None) -> list[str]:
    expanded: list[str] = []
    for raw in values or []:
        if _is_url(raw):
            expanded.append(raw)
            continue
        path = os.path.abspath(raw)
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    expanded.append(os.path.join(root, name))
        else:
            expanded.append(path)
    # Deterministic de-duplication.
    return list(dict.fromkeys(expanded))


def _is_fence_open(line: str) -> bool:
    return bool(FENCE_RE.match(line))


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def parse_blocks(text: str) -> list[tuple[str, list[str]]]:
    lines = text.split("\n")
    blocks: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_fence_open(line):
            fence = FENCE_RE.match(line).group(1)
            buf = [line]
            index += 1
            while index < len(lines):
                buf.append(lines[index])
                if re.match(r"^\s*" + re.escape(fence) + r"\s*$", lines[index].strip()):
                    index += 1
                    break
                index += 1
            blocks.append(("code", buf))
        elif _is_table_row(line):
            buf = []
            while index < len(lines) and _is_table_row(lines[index]):
                buf.append(lines[index])
                index += 1
            blocks.append(("table", buf))
        elif not line.strip():
            blocks.append(("prose", [line]))
            index += 1
        else:
            buf = []
            while (
                index < len(lines)
                and lines[index].strip()
                and not _is_fence_open(lines[index])
                and not _is_table_row(lines[index])
            ):
                buf.append(lines[index])
                index += 1
            blocks.append(("prose", buf))
    return blocks


def pack_blocks(
    blocks: list[tuple[str, list[str]]], chunk_lines: int
) -> list[list[tuple[str, list[str]]]]:
    chunks: list[list[tuple[str, list[str]]]] = []
    current: list[tuple[str, list[str]]] = []
    count = 0
    for block_type, lines in blocks:
        if current and count + len(lines) > chunk_lines:
            chunks.append(current)
            current, count = [], 0
        current.append((block_type, lines))
        count += len(lines)
    if current:
        chunks.append(current)
    return chunks


def chunk_text(text: str, chunk_lines: int) -> list[dict[str, Any]]:
    if chunk_lines < 1:
        die("--chunk-lines must be a positive integer")
    # A terminal newline terminates the last physical line; it is not an extra
    # reviewable blank line. The canonical artifact itself remains byte-exact.
    chunkable = text[:-1] if text.endswith("\n") else text
    if not chunkable:
        chunkable = ""
    packed = pack_blocks(parse_blocks(chunkable), chunk_lines)
    output: list[dict[str, Any]] = []
    next_line = 1
    for index, chunk in enumerate(packed, 1):
        count = sum(len(lines) for _, lines in chunk)
        body = "\n".join(line for _, lines in chunk for line in lines)
        output.append({
            "index": index,
            "start": next_line,
            "end": next_line + count - 1,
            "lines": count,
            "text": body,
            "sha256": _sha256_text(body),
            "oversized": count > chunk_lines,
            "fence_balance": "balanced",
        })
        next_line += count
    return output


def compute_checks(focus: str | None) -> list[str]:
    normalized = (focus or "all").lower()
    if normalized not in FOCUS_CHECKS:
        die(f"Unknown --focus: {focus}. Use grammar|style|logic|consistency|all.")
    return list(FOCUS_CHECKS[normalized])


def compute_local_dimensions(focus: str | None) -> list[tuple[str, list[str]]]:
    checks = compute_checks(focus)
    grouped: dict[str, list[str]] = {}
    for check in checks:
        grouped.setdefault(CHECK_DIMENSION[check], []).append(check)
    return [(dimension, grouped[dimension]) for dimension in ("grammar-style", "logic-consistency") if dimension in grouped]


def compute_dimensions(focus: str, has_references: bool) -> list[str]:
    """Compatibility helper; v3 plan cells use explicit stages/checks instead."""
    dimensions = [dimension for dimension, _ in compute_local_dimensions(focus)]
    if has_references:
        dimensions.append("fact-check")
    return dimensions


def detect_language(text: str) -> str:
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)
    body = re.sub(r"```.*?```|~~~.*?~~~", "", body, flags=re.DOTALL)
    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", body))
    latin_words = len(re.findall(r"\b[A-Za-z]+(?:[-'][A-Za-z]+)*\b", body))
    return "zh" if cjk_chars > latin_words else "en"


def resolve_language(requested: str, text: str) -> str:
    if requested not in {"auto", "en", "zh"}:
        die("--language must be auto, en, or zh")
    return detect_language(text) if requested == "auto" else requested


def _safe_stem(value: str) -> str:
    stem = os.path.splitext(_display_name(value))[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "reference"


def _source_descriptor(source: str, kind: str, text: str, canonical_path: str) -> dict[str, Any]:
    ext = _extension(source)
    return {
        "original": _norm(source),
        "kind": kind,
        "extension": ext,
        "canonical_path": _norm(canonical_path),
        "sha256": _sha256_text(text),
        "diff_applicable": kind == "file" and ext in {".md", ".markdown", ".txt"},
    }


def _reference_passages(text: str, reference_id: str, max_chars: int = 20_000) -> list[dict[str, Any]]:
    lines = text.splitlines() or [""]
    passages: list[dict[str, Any]] = []
    start = 1
    current: list[str] = []
    chars = 0
    for line_number, line in enumerate(lines, 1):
        # Passage identifiers are line-ranged.  A single oversized line cannot
        # be split without inventing a finer-grained location contract, and it
        # would otherwise fail only after local/claim work has been dispatched.
        if len(line) > max_chars:
            raise _V4CapacityError(
                f"Reference line {line_number} exceeds the {max_chars}-character "
                "passage budget and cannot be split while preserving line-range coverage."
            )
        line_chars = len(line) + 1
        if current and chars + line_chars > max_chars:
            body = "\n".join(current)
            passages.append({
                "id": f"{reference_id}-p{len(passages) + 1:03d}",
                "reference_id": reference_id,
                "start_line": start,
                "end_line": line_number - 1,
                "sha256": _sha256_text(body),
                "chars": len(body),
            })
            current, chars, start = [], 0, line_number
        current.append(line)
        chars += line_chars
    body = "\n".join(current)
    passages.append({
        "id": f"{reference_id}-p{len(passages) + 1:03d}",
        "reference_id": reference_id,
        "start_line": start,
        "end_line": len(lines),
        "sha256": _sha256_text(body),
        "chars": len(body),
    })
    return passages


def _cell_result_filename(cell_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "__", cell_id) + ".json"


def _new_cell(
    *, cell_id: str, stage: str, dimension: str, checks: list[str], input_hash: str,
    dependencies: list[str], chunk: int | None = None, lines: str | None = None,
    chunk_path: str | None = None, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "id": cell_id,
        "stage": stage,
        "dimension": dimension,
        "checks": checks,
        "input_hash": input_hash,
        "dependencies": dependencies,
        "required": True,
        "result_file": _cell_result_filename(cell_id),
    }
    if chunk is not None:
        cell["chunk"] = chunk
    if lines is not None:
        cell["lines"] = lines
    if chunk_path is not None:
        cell["chunk_path"] = _norm(chunk_path)
    if extra:
        cell.update(extra)
    return cell


def plan_reducer_batches(
    dependencies: list[dict[str, Any]], max_chars: int = MAX_REDUCER_INPUT_CHARS,
    max_depth: int = MAX_REDUCER_DEPTH,
) -> list[dict[str, Any]]:
    """Plan a hierarchy whose *declared output budgets* fit every next input.

    Each dependency is gated to ``max_output_chars`` (10k by default).  Thus a
    planned batch is an honest worst-case runtime bound, not an estimate based
    on tiny plan descriptors.  Every reducer result is validated against the
    same output bound before a dependent cell may be accepted.
    """
    if max_chars < 1 or max_depth < 1:
        raise ValueError("reducer bounds must be positive")
    current = [
        {**item, "max_output_chars": int(item.get("max_output_chars", MAX_REDUCER_OUTPUT_CHARS))}
        for item in dependencies
    ]
    planned: list[dict[str, Any]] = []
    depth = 1
    while current:
        batches: list[list[dict[str, Any]]] = []
        active: list[dict[str, Any]] = []
        active_chars = 2
        for item in current:
            budget = int(item["max_output_chars"])
            if budget + 2 > max_chars:
                raise ValueError("one reducer output budget exceeds max serialized input chars")
            if active and active_chars + budget + 1 > max_chars:
                batches.append(active)
                active, active_chars = [], 2
            active.append(item)
            active_chars += budget + 1
        if active:
            batches.append(active)
        for batch_index, batch in enumerate(batches, 1):
            output_id = f"global:reduce:d{depth}:b{batch_index:03d}"
            planned.append({
                "depth": depth,
                "batch": batch_index,
                "output_id": output_id,
                "input_ids": [str(item["id"]) for item in batch],
                "input_budget_chars": sum(int(item["max_output_chars"]) for item in batch),
                "max_output_chars": MAX_REDUCER_OUTPUT_CHARS,
            })
        if len(batches) <= 1:
            return planned
        if depth >= max_depth:
            raise ValueError("hierarchical reducer did not converge within max depth")
        current = [
            {"id": f"global:reduce:d{depth}:b{i:03d}", "max_output_chars": MAX_REDUCER_OUTPUT_CHARS}
            for i in range(1, len(batches) + 1)
        ]
        depth += 1
    return planned


def batch_observations(
    observations: list[dict[str, Any]], max_chars: int = MAX_REDUCER_INPUT_CHARS,
) -> list[dict[str, Any]]:
    """Deterministically batch actual observation JSON and hash every batch."""
    batches: list[list[dict[str, Any]]] = []
    active: list[dict[str, Any]] = []
    for observation in observations:
        candidate = active + [observation]
        size = len(json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        if size > max_chars:
            if not active:
                raise ValueError("one observation exceeds the reducer input character budget")
            batches.append(active)
            active = [observation]
        else:
            active = candidate
    if active or not batches:
        batches.append(active)
    output = []
    for index, batch in enumerate(batches, 1):
        serialized = json.dumps(batch, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        output.append({
            "id": f"observations-{index:03d}",
            "sha256": _sha256_text(serialized),
            "serialized_chars": len(serialized),
            "observations": batch,
        })
    return output


def _stage_cap_or_die(stage: str, count: int, cap: int) -> None:
    if count > cap:
        die(
            f"{stage} cell count {count} exceeds per-stage max_cells={cap}. "
            "Raise --chunk-lines when safe, reduce --focus, or split the document. "
            "Separate reports do not preserve cross-document consistency or omission coverage.",
            code=2,
        )


def _plan_hash(plan: dict[str, Any]) -> str:
    material = dict(plan)
    material.pop("plan_hash", None)
    material.pop("dry_run", None)
    return _json_hash(material)


def cmd_plan(args: argparse.Namespace) -> None:
    cfg = load_config()
    chunk_lines = args.chunk_lines or int(cfg["chunk_lines"])
    max_chunks = min(int(cfg["max_chunks"]), HARD_MAX_CHUNKS)
    max_cells = min(int(cfg["max_cells"]), HARD_MAX_CELLS_PER_STAGE)
    if max_chunks < 1 or max_cells < 1:
        die("Configured max_chunks and max_cells must be positive")

    input_value, input_kind = _resolve_input(args.input)  # URL before abspath
    text = read_source(input_value)
    requested_language = args.language
    document_language = detect_language(text)
    resolved_language = resolve_language(requested_language, text)
    chunks = chunk_text(text, chunk_lines)
    if len(chunks) > max_chunks:
        die(
            f"Chunk count {len(chunks)} exceeds max_chunks={max_chunks}. "
            f"Raise --chunk-lines (currently {chunk_lines}) when safe, reduce --focus, or split the document. "
            "Separate reports do not preserve cross-document consistency or omission coverage.",
            code=2,
        )
    oversized_char_chunks = [chunk for chunk in chunks if len(chunk["text"]) > MAX_CHUNK_CHARS]
    if oversized_char_chunks:
        details = ", ".join(
            f"chunk {chunk['index']}={len(chunk['text'])} chars" for chunk in oversized_char_chunks
        )
        die(
            f"Chunk character budget exceeded ({details}; max={MAX_CHUNK_CHARS}). "
            "Reduce --chunk-lines or split the document without breaking its fenced/table structure.",
            code=2,
        )

    run_id = str(uuid.uuid4())
    if args.workspace:
        workspace_base = os.path.abspath(args.workspace)
    elif input_kind == "file":
        workspace_base = os.path.join(os.path.dirname(input_value), ".review-workspace")
    else:
        workspace_base = os.path.join(os.getcwd(), ".review-workspace")
    run_workspace = os.path.join(workspace_base, run_id)
    chunks_dir = os.path.join(run_workspace, "chunks")
    artifacts_dir = os.path.join(run_workspace, "artifacts")
    source_artifact = os.path.join(artifacts_dir, "source.md")
    line_map_path = os.path.join(artifacts_dir, "source-line-map.json")
    source = _source_descriptor(input_value, input_kind, text, source_artifact)
    source["input_sha256"] = _original_input_hash(input_value, text)
    source["line_map_path"] = _norm(line_map_path)
    line_map = [
        {"chunk": chunk["index"], "start_line": chunk["start"], "end_line": chunk["end"]}
        for chunk in chunks
    ]
    line_map_serialized = json.dumps(line_map, ensure_ascii=False, indent=2)
    source["line_map_sha256"] = _sha256_text(line_map_serialized)

    local_cells: list[dict[str, Any]] = []
    for dimension, dimension_checks in compute_local_dimensions(args.focus):
        for chunk in chunks:
            local_cells.append(_new_cell(
                cell_id=f"local:{dimension}:{chunk['index']:03d}",
                stage="local",
                dimension=dimension,
                checks=dimension_checks,
                input_hash=chunk["sha256"],
                dependencies=[],
                chunk=chunk["index"],
                lines=f"{chunk['start']}-{chunk['end']}",
                chunk_path=os.path.join(chunks_dir, f"chunk_{chunk['index']:03d}.md"),
                extra={"core_start": chunk["start"], "core_end": chunk["end"]},
            ))
    _stage_cap_or_die("local", len(local_cells), max_cells)

    try:
        reducer_batches = plan_reducer_batches([
            {"id": cell["id"], "input_hash": cell["input_hash"],
             "max_output_chars": MAX_REDUCER_OUTPUT_CHARS}
            for cell in local_cells
        ])
    except ValueError as exc:
        die(f"Global reducer cannot be planned completely: {exc}", code=2)
    global_cells: list[dict[str, Any]] = []
    for position, batch in enumerate(reducer_batches):
        is_final = position == len(reducer_batches) - 1
        cell_id = batch["output_id"]
        global_cells.append(_new_cell(
            cell_id=cell_id,
            stage="global",
            dimension="document-global" if is_final else "observation-reduce",
            checks=(
                [check for check in compute_checks(args.focus) if check in {"logic", "consistency", "style"}]
                if is_final else ["observation-reduction"]
            ),
            input_hash=_json_hash({"source": source["sha256"], "dependencies": batch["input_ids"]}),
            dependencies=batch["input_ids"],
            extra={
                "reducer_depth": batch["depth"],
                "reducer_batch": batch["batch"],
                "max_serialized_input_chars": MAX_REDUCER_INPUT_CHARS,
                "input_budget_chars": batch["input_budget_chars"],
                "max_output_chars": batch["max_output_chars"],
                "requires_actual_observation_hashes": True,
                "observation_inputs_required": batch["input_ids"],
            },
        ))
    _stage_cap_or_die("global", len(global_cells), max_cells)

    reference_values = _expand_references(args.references)
    reference_artifacts: list[dict[str, Any]] = []
    all_passages: list[dict[str, Any]] = []
    reference_texts: list[str] = []
    for index, reference in enumerate(reference_values, 1):
        ref_text = read_source(reference)
        reference_texts.append(ref_text)
        reference_id = f"ref-{index:03d}"
        canonical_path = os.path.join(artifacts_dir, "references", f"{index:03d}-{_safe_stem(reference)}.md")
        descriptor = _source_descriptor(reference, "url" if _is_url(reference) else "file", ref_text, canonical_path)
        descriptor["id"] = reference_id
        descriptor["passages"] = _reference_passages(ref_text, reference_id)
        for passage in descriptor["passages"]:
            passage["canonical_path"] = _norm(canonical_path)
        reference_artifacts.append(descriptor)
        all_passages.extend(descriptor["passages"])

    reference_cells: list[dict[str, Any]] = []
    if reference_artifacts:
        claim_cells: list[dict[str, Any]] = []
        for chunk in chunks:
            claim_cells.append(_new_cell(
                cell_id=f"reference:claim-extraction:{chunk['index']:03d}",
                stage="reference", dimension="claim-extraction", checks=["claim-extraction"],
                input_hash=chunk["sha256"], dependencies=[], chunk=chunk["index"],
                lines=f"{chunk['start']}-{chunk['end']}",
                chunk_path=os.path.join(chunks_dir, f"chunk_{chunk['index']:03d}.md"),
                extra={"core_start": chunk["start"], "core_end": chunk["end"]},
            ))
        index_cells: list[dict[str, Any]] = []
        for artifact in reference_artifacts:
            for passage in artifact["passages"]:
                index_cells.append(_new_cell(
                    cell_id=f"reference:passage-index:{passage['id']}",
                    stage="reference", dimension="passage-index", checks=["passage-index"],
                    input_hash=passage["sha256"], dependencies=[],
                    extra={
                        "reference_id": artifact["id"], "passage_ids": [passage["id"]],
                        "reference_path": artifact["canonical_path"],
                        "reference_lines": f"{passage['start_line']}-{passage['end_line']}",
                    },
                ))
        routing_cells: list[dict[str, Any]] = []
        grounding_cells: list[dict[str, Any]] = []
        batch_ids = [passage["id"] for passage in all_passages]
        for claim_cell in claim_cells:
            chunk_routing: list[dict[str, Any]] = []
            for passage, index_cell in zip(all_passages, index_cells):
                routing = _new_cell(
                    cell_id=(
                        f"reference:semantic-routing:{claim_cell['chunk']:03d}:"
                        f"{passage['id']}"
                    ),
                    stage="reference", dimension="semantic-routing", checks=["semantic-routing"],
                    input_hash=_json_hash({
                        "claim": claim_cell["input_hash"], "passage": passage["sha256"]
                    }),
                    dependencies=[claim_cell["id"], index_cell["id"]], chunk=claim_cell["chunk"],
                    lines=claim_cell["lines"], chunk_path=claim_cell["chunk_path"],
                    extra={
                        "core_start": claim_cell["core_start"], "core_end": claim_cell["core_end"],
                        "required_batch_ids": [passage["id"]],
                        "passage_ids": [passage["id"]],
                        "retrieval_policy": "semantic-required; lexical-ranking-only; candidate-union",
                    },
                )
                routing_cells.append(routing)
                chunk_routing.append(routing)
            grounding_cells.append(_new_cell(
                cell_id=f"reference:grounding:{claim_cell['chunk']:03d}",
                stage="reference", dimension="grounding", checks=["claim-grounding"],
                input_hash=_json_hash({"routing": [cell["input_hash"] for cell in chunk_routing]}),
                dependencies=[cell["id"] for cell in chunk_routing], chunk=claim_cell["chunk"],
                lines=claim_cell["lines"], chunk_path=claim_cell["chunk_path"],
                extra={
                    "core_start": claim_cell["core_start"], "core_end": claim_cell["core_end"],
                    "required_batch_ids": batch_ids,
                    "grounding_policy": "all-semantic-batches-required-before-not-established",
                },
            ))
        coverage = _new_cell(
            cell_id="reference:reference-coverage:global", stage="reference",
            dimension="reference-coverage", checks=["reference-coverage", "omission"],
            input_hash=_json_hash({"source": source["sha256"], "references": [a["sha256"] for a in reference_artifacts]}),
            dependencies=[cell["id"] for cell in grounding_cells + index_cells],
            extra={"required_batch_ids": batch_ids},
        )
        adjudication = _new_cell(
            cell_id="reference:adjudication:global", stage="reference",
            dimension="adjudication", checks=["contradiction-adjudication", "unsupported-adjudication"],
            input_hash=_json_hash({"coverage": coverage["input_hash"], "grounding": [c["input_hash"] for c in grounding_cells]}),
            dependencies=[coverage["id"], *[cell["id"] for cell in grounding_cells]],
            extra={"required_batch_ids": batch_ids},
        )
        reference_cells = claim_cells + index_cells + routing_cells + grounding_cells + [coverage, adjudication]
    _stage_cap_or_die("reference", len(reference_cells), max_cells)

    cells = local_cells + global_cells + reference_cells
    for cell in cells:
        cell["document_language"] = document_language
        cell["report_language"] = resolved_language
        cell["text_contract"] = {
            "original_text": "verbatim-source",
            "revised_text": f"document-language:{document_language}",
            "change": f"report-language:{resolved_language}",
            "reason": f"report-language:{resolved_language}",
            "omission_sentinel": "[原文缺失]" if document_language == "zh" else "[Missing from source]",
            "deletion_marker": "[删除该文本]" if document_language == "zh" else "[Delete this text]",
            "confirmation_marker": (
                "需作者确认；建议方向：" if document_language == "zh"
                else "Author confirmation required; suggested direction:"
            ),
        }
    chunk_records = [{
        "index": chunk["index"], "start": chunk["start"], "end": chunk["end"],
        "lines": chunk["lines"], "sha256": chunk["sha256"], "oversized": chunk["oversized"],
        "fence_balance": chunk["fence_balance"],
        "path": _norm(os.path.join(chunks_dir, f"chunk_{chunk['index']:03d}.md")),
    } for chunk in chunks]
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "version": VERSION,
        "package_version": VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "input": _norm(input_value),
        "input_kind": input_kind,
        "input_hash": source["input_sha256"],
        "source_hash": source["input_sha256"],
        "source": source,
        # compatibility aliases used by older orchestrators
        "file": _norm(input_value),
        "workspace": _norm(chunks_dir),
        "run_workspace": _norm(run_workspace),
        "canonical_source": _norm(source_artifact),
        "total_lines": len(text.splitlines()),
        "line_map": line_map,
        "chunk_lines": chunk_lines,
        "focus": args.focus,
        "checks": compute_checks(args.focus),
        "document_language": document_language,
        "language": {"requested": requested_language, "resolved": resolved_language},
        "report_language": resolved_language,
        "resolved_language": resolved_language,
        "has_references": bool(reference_artifacts),
        "references": [_norm(value) for value in reference_values],
        "reference_artifacts": reference_artifacts,
        "reference_passages": all_passages,
        "passages": all_passages,
        "chunks": chunk_records,
        "n_chunks": len(chunks),
        "dimensions": [dimension for dimension, _ in compute_local_dimensions(args.focus)],
        "n_dimensions": len(compute_local_dimensions(args.focus)),
        "cells": cells,
        "n_cells": len(cells),
        "n_total_cells": len(cells),
        "stages": {
            "local": {"count": len(local_cells), "max_cells": max_cells},
            "global": {"count": len(global_cells), "max_cells": max_cells},
            "reference": {"count": len(reference_cells), "max_cells": max_cells},
        },
        "dag": {"cells": [cell["id"] for cell in cells], "dependencies": {cell["id"]: cell["dependencies"] for cell in cells}},
        "reducer": {
            "schema": OBSERVATION_SCHEMA,
            "max_serialized_input_chars": MAX_REDUCER_INPUT_CHARS,
            "max_depth": MAX_REDUCER_DEPTH,
            "max_output_chars": MAX_REDUCER_OUTPUT_CHARS,
            "batches": reducer_batches,
        },
        "max_chunk_chars": MAX_CHUNK_CHARS,
        "max_chunks": max_chunks,
        "max_cells_per_stage": max_cells,
        "attempts_path": _norm(os.path.join(run_workspace, "attempts.json")),
        "diff": {
            "applicable": source["diff_applicable"],
            "status": "not_requested",
            "reason": "available only for direct .md/.markdown/.txt sources" if not source["diff_applicable"] else "",
        },
    }
    plan["plan_hash"] = _plan_hash(plan)

    if args.dry_run:
        plan["dry_run"] = True
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return

    os.makedirs(chunks_dir, exist_ok=False)
    os.makedirs(os.path.dirname(source_artifact), exist_ok=True)
    with open(source_artifact, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    with open(line_map_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(line_map_serialized)
    for chunk, record in zip(chunks, chunk_records):
        with open(record["path"], "w", encoding="utf-8", newline="") as handle:
            handle.write(chunk["text"])
        if chunk["oversized"]:
            print(f"Warning: kept unsplittable {chunk['lines']}-line block in chunk {chunk['index']}", file=sys.stderr)
    for artifact, ref_text in zip(reference_artifacts, reference_texts):
        os.makedirs(os.path.dirname(artifact["canonical_path"]), exist_ok=True)
        with open(artifact["canonical_path"], "w", encoding="utf-8", newline="") as handle:
            handle.write(ref_text)
    with open(plan["attempts_path"], "w", encoding="utf-8") as handle:
        json.dump({"schema": "CellAttempts/v1", "run_id": run_id, "cells": {}}, handle, ensure_ascii=False, indent=2)
    if args.plan_output:
        output = os.path.abspath(args.plan_output)
        os.makedirs(os.path.dirname(output), exist_ok=True) if os.path.dirname(output) else None
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False, indent=2)
    print(json.dumps(plan, ensure_ascii=False, indent=2))


def _cell_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(cell.get("id")): cell for cell in plan.get("cells", []) if cell.get("id")}


def _first_line(locations: list[Any]) -> int | None:
    if not locations:
        return None
    location = locations[0]
    if isinstance(location, int):
        return location
    if isinstance(location, dict):
        return location.get("start_line", location.get("line"))
    return None


def _validate_location(location: Any, errors: list[str], prefix: str) -> tuple[int | None, int | None]:
    if isinstance(location, int) and not isinstance(location, bool):
        start = end = location
    elif isinstance(location, dict):
        start = location.get("start_line", location.get("line"))
        end = location.get("end_line", start)
    else:
        errors.append(f"{prefix} must be an integer or {{start_line,end_line}}")
        return None, None
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        errors.append(f"{prefix} line bounds must be integers")
        return None, None
    if start < 1 or end < start:
        errors.append(f"{prefix} has invalid line bounds")
    return start, end


def _canonical_text(plan: dict[str, Any]) -> str | None:
    path = plan.get("source", {}).get("canonical_path") or plan.get("canonical_source")
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    source = plan.get("file")
    if source and not _is_url(source) and os.path.isfile(source) and _extension(source) in {".md", ".markdown", ".txt"}:
        with open(source, "r", encoding="utf-8") as handle:
            return handle.read()
    return None


def _validate_evidence(
    evidence: Any, passage_map: dict[str, dict[str, Any]], reference_ids: set[str],
    errors: list[str], prefix: str,
) -> None:
    if not isinstance(evidence, dict):
        errors.append(f"{prefix} must be an object")
        return
    required = {"schema", "reference_id", "passage_id", "location", "quote"}
    missing = sorted(required - evidence.keys())
    if missing:
        errors.append(f"{prefix} missing fields: {', '.join(missing)}")
    if evidence.get("schema") != EVIDENCE_SCHEMA:
        errors.append(f"{prefix}.schema must be {EVIDENCE_SCHEMA}")
    reference_id = evidence.get("reference_id")
    if not isinstance(reference_id, str) or reference_id not in reference_ids:
        errors.append(f"{prefix}.reference_id is not in the plan reference manifest")
    passage_id = evidence.get("passage_id")
    if not isinstance(passage_id, str) or passage_id not in passage_map:
        errors.append(f"{prefix}.passage_id is not in the plan passage manifest")
    for field in ("location", "quote"):
        if not isinstance(evidence.get(field), str) or not evidence.get(field):
            errors.append(f"{prefix}.{field} must be a non-empty string")
    passage = passage_map.get(str(passage_id))
    if passage and passage.get("reference_id") != reference_id:
        errors.append(f"{prefix}.reference_id does not own the cited passage")
    if passage and isinstance(evidence.get("quote"), str):
        try:
            with open(passage["canonical_path"], "r", encoding="utf-8") as handle:
                reference_lines = handle.read().splitlines()
            excerpt = "\n".join(
                reference_lines[passage["start_line"] - 1:passage["end_line"]]
            )
            if evidence["quote"] not in excerpt:
                errors.append(f"{prefix}.quote is not verbatim in the cited passage")
        except (OSError, KeyError, TypeError):
            errors.append(f"{prefix} cited passage artifact cannot be verified")


def _allowed_categories_for_cell(cell: dict[str, Any]) -> set[str]:
    """Return the exact finding categories authorized by a planned cell."""
    if cell.get("stage") in {"local", "global"}:
        allowed: set[str] = set()
        for check in cell.get("checks", []):
            allowed.update(CHECK_FINDING_CATEGORIES.get(check, set()))
        return allowed
    if cell.get("stage") == "reference":
        if cell.get("dimension") == "reference-coverage":
            return {"omission"}
        if cell.get("dimension") == "adjudication":
            return {"fact", "contradiction", "unsupported"}
        return set()
    return set()


def validate_cell_result(
    plan: dict[str, Any], cell: dict[str, Any], result: Any,
) -> list[str]:
    """Return strict v3 validation errors; an empty list means accepted."""
    errors: list[str] = []
    if plan.get("schema") != PLAN_SCHEMA:
        return [f"plan schema must be {PLAN_SCHEMA}"]
    if not isinstance(result, dict):
        return ["result must be a JSON object"]
    if result.get("schema") != CELL_SCHEMA:
        errors.append(f"schema must be {CELL_SCHEMA}")
    if result.get("run_id") != plan.get("run_id"):
        errors.append("run_id does not match the plan")
    if result.get("input_hash") != cell.get("input_hash"):
        errors.append("input_hash does not match the cell (stale or wrong input)")
    returned_cell = result.get("cell")
    if not isinstance(returned_cell, dict):
        errors.append("cell must be an object")
    else:
        for field in ("id", "stage", "dimension"):
            if returned_cell.get(field) != cell.get(field):
                errors.append(f"cell.{field} does not match the plan")
        if cell.get("chunk") is not None and returned_cell.get("chunk") != cell.get("chunk"):
            errors.append("cell.chunk does not match the plan")
    if result.get("checked_thoroughly") is not True:
        errors.append("checked_thoroughly must be the boolean true")
    completed = result.get("checks_completed")
    if not isinstance(completed, list) or any(not isinstance(item, str) for item in completed):
        errors.append("checks_completed must be a list of strings")
    elif completed != cell.get("checks", []):
        errors.append("checks_completed must exactly cover the cell checks")

    canonical = _canonical_text(plan)
    source_lines = canonical.splitlines() if canonical is not None else []
    passage_map = {
        str(item.get("id")): item for item in plan.get("reference_passages", []) if item.get("id")
    }
    reference_ids = {
        str(item.get("id")) for item in plan.get("reference_artifacts", []) if item.get("id")
    }
    findings = result.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be a list")
        findings = []
    allowed_categories = _allowed_categories_for_cell(cell)
    for index, finding in enumerate(findings):
        prefix = f"findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{prefix} must be an object")
            continue
        required = {"locations", "original_text", "revised_text", "change", "reason", "severity", "category", "fixable", "evidence"}
        missing = sorted(required - finding.keys())
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(missing)}")
        locations = finding.get("locations")
        if not isinstance(locations, list) or not locations:
            errors.append(f"{prefix}.locations must be a non-empty list")
            locations = []
        bounds: list[tuple[int | None, int | None]] = []
        for loc_index, location in enumerate(locations):
            bounds.append(_validate_location(location, errors, f"{prefix}.locations[{loc_index}]"))
        if cell.get("stage") == "local" or cell.get("chunk") is not None:
            core_start, core_end = cell.get("core_start"), cell.get("core_end")
            for start, end in bounds:
                if start is not None and core_start is not None and (start < core_start or end > core_end):
                    errors.append(f"{prefix} location is outside the cell core lines")
        original = finding.get("original_text")
        missing_sentinels = {"[原文缺失]", "[Missing from source]"}
        expected_missing = "[原文缺失]" if plan.get("document_language") == "zh" else "[Missing from source]"
        if not isinstance(original, str) or not original:
            errors.append(f"{prefix}.original_text must be a non-empty string")
        elif original in missing_sentinels:
            if (
                cell.get("dimension") != "reference-coverage"
                or finding.get("category") != "omission"
                or original != expected_missing
            ):
                errors.append(
                    f"{prefix}: the language-matched missing-source sentinel is reserved for reference-coverage omissions"
                )
        elif canonical is not None:
            if original not in canonical:
                errors.append(f"{prefix}.original_text is not verbatim in the canonical source")
            elif bounds and bounds[0][0] and source_lines:
                start, end = bounds[0]
                excerpt = "\n".join(source_lines[start - 1:end]) if end <= len(source_lines) else ""
                if original not in excerpt:
                    errors.append(f"{prefix}.original_text does not occur at its declared location")
        for field in ("revised_text", "change", "reason"):
            if not isinstance(finding.get(field), str) or not finding.get(field):
                errors.append(f"{prefix}.{field} must be a non-empty string")
        if finding.get("severity") not in ALLOWED_SEVERITIES:
            errors.append(f"{prefix}.severity is invalid")
        if finding.get("category") not in ALLOWED_CATEGORIES:
            errors.append(f"{prefix}.category is invalid")
        elif finding.get("category") not in allowed_categories:
            allowed_text = ", ".join(sorted(allowed_categories)) or "none"
            errors.append(
                f"{prefix}.category is outside the planned checks/stage (allowed: {allowed_text})"
            )
        if finding.get("category") == "omission" and cell.get("dimension") != "reference-coverage":
            errors.append(f"{prefix}: omissions may only be emitted by the reference-coverage cell")
        if not isinstance(finding.get("fixable"), bool):
            errors.append(f"{prefix}.fixable must be boolean")
        evidence = finding.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{prefix}.evidence must be a list")
        else:
            for ev_index, item in enumerate(evidence):
                _validate_evidence(
                    item, passage_map, reference_ids, errors, f"{prefix}.evidence[{ev_index}]"
                )
        if cell.get("stage") == "reference" and not evidence:
            errors.append(f"{prefix}: reference-stage findings require evidence")
        if (
            cell.get("stage") == "reference"
            and cell.get("dimension") not in {"reference-coverage", "adjudication"}
        ):
            errors.append(f"{prefix}: this reference stage may emit assessments/observations, not findings")

    observations = result.get("observations")
    if not isinstance(observations, list):
        errors.append("observations must be a list")
        observations = []
    for index, observation in enumerate(observations):
        prefix = f"observations[{index}]"
        if not isinstance(observation, dict):
            errors.append(f"{prefix} must be an object")
            continue
        required = {"schema", "kind", "key", "value", "normalized_value", "locations"}
        missing = sorted(required - observation.keys())
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(missing)}")
        if observation.get("schema") != OBSERVATION_SCHEMA:
            errors.append(f"{prefix}.schema must be {OBSERVATION_SCHEMA}")
        if observation.get("kind") not in ALLOWED_OBSERVATION_KINDS:
            errors.append(f"{prefix}.kind is invalid")
        for field in ("key", "value", "normalized_value"):
            if not isinstance(observation.get(field), str) or not observation.get(field):
                errors.append(f"{prefix}.{field} must be a non-empty string")
        locations = observation.get("locations")
        if not isinstance(locations, list) or not locations:
            errors.append(f"{prefix}.locations must be a non-empty list")
        else:
            for loc_index, location in enumerate(locations):
                start, end = _validate_location(location, errors, f"{prefix}.locations[{loc_index}]")
                if (cell.get("stage") == "local" or cell.get("chunk") is not None) and start is not None:
                    if start < cell.get("core_start", start) or end > cell.get("core_end", end):
                        errors.append(f"{prefix} location is outside the cell core lines")
            value_corpus = canonical
            value_lines = source_lines
            if cell.get("dimension") == "passage-index" and cell.get("reference_path"):
                try:
                    with open(cell["reference_path"], "r", encoding="utf-8") as handle:
                        value_corpus = handle.read()
                    value_lines = value_corpus.splitlines()
                except OSError:
                    value_corpus = None
                    value_lines = []
            if (
                value_corpus is not None
                and isinstance(observation.get("value"), str)
                and observation.get("value") not in value_corpus
            ):
                errors.append(f"{prefix}.value must be verbatim in its canonical input")
            elif value_lines and isinstance(locations, list):
                for loc_index, location in enumerate(locations):
                    start, end = _validate_location(location, [], "location")
                    if start is None or end is None or end > len(value_lines):
                        continue
                    excerpt = "\n".join(value_lines[start - 1:end])
                    if observation.get("value") not in excerpt:
                        errors.append(
                            f"{prefix}.value does not occur at locations[{loc_index}]"
                        )

    observation_chars = len(json.dumps(
        observations, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))
    if cell.get("stage") in {"local", "global"} and observation_chars > int(
        cell.get("max_output_chars", MAX_REDUCER_OUTPUT_CHARS)
    ):
        errors.append("observations exceed the reducer output character gate")
    if cell.get("dimension") == "observation-reduce" and findings:
        errors.append("intermediate observation-reduce cells may not emit user findings")
    if cell.get("stage") == "global":
        observation_order = [
            (
                str(item.get("kind", "")), str(item.get("key", "")),
                _first_line(item.get("locations", [])) or sys.maxsize,
                str(item.get("normalized_value", "")), str(item.get("value", "")),
            )
            for item in observations if isinstance(item, dict)
        ]
        if observation_order != sorted(observation_order):
            errors.append("global observations must use stable kind/key/location/value order")
    if cell.get("stage") == "global":
        observation_inputs = result.get("observation_inputs")
        if not isinstance(observation_inputs, list):
            errors.append("global results require observation_inputs")
        else:
            for index, item in enumerate(observation_inputs):
                if not isinstance(item, dict) or set(item) != {"cell_id", "sha256", "serialized_chars"}:
                    errors.append(
                        f"observation_inputs[{index}] must contain only cell_id, sha256, serialized_chars"
                    )
                elif (
                    not isinstance(item.get("cell_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
                    or not isinstance(item.get("serialized_chars"), int)
                    or isinstance(item.get("serialized_chars"), bool)
                    or item.get("serialized_chars") < 0
                ):
                    errors.append(f"observation_inputs[{index}] has invalid field types")

    assessment_required = cell.get("dimension") in {
        "semantic-routing", "grounding", "reference-coverage", "adjudication"
    }
    if assessment_required and "reference_assessments" not in result:
        errors.append("reference_assessments is required for this reference stage")
    assessments = result.get("reference_assessments", [])
    if not isinstance(assessments, list):
        errors.append("reference_assessments must be a list when present")
        assessments = []
    required_batches = set(cell.get("required_batch_ids", []))
    for index, assessment in enumerate(assessments):
        prefix = f"reference_assessments[{index}]"
        if not isinstance(assessment, dict):
            errors.append(f"{prefix} must be an object")
            continue
        required = {"claim_key", "status", "batch_ids", "completed_batch_ids", "evidence"}
        missing = sorted(required - assessment.keys())
        if missing:
            errors.append(f"{prefix} missing fields: {', '.join(missing)}")
        if not isinstance(assessment.get("claim_key"), str) or not assessment.get("claim_key"):
            errors.append(f"{prefix}.claim_key must be a non-empty string")
        status = assessment.get("status")
        if status not in ASSESSMENT_STATUSES:
            errors.append(f"{prefix}.status is invalid")
        batch_ids = assessment.get("batch_ids")
        completed = assessment.get("completed_batch_ids")
        if not isinstance(batch_ids, list) or any(not isinstance(item, str) for item in batch_ids):
            errors.append(f"{prefix}.batch_ids must be a string list")
            batch_ids = []
        if not isinstance(completed, list) or any(not isinstance(item, str) for item in completed):
            errors.append(f"{prefix}.completed_batch_ids must be a string list")
            completed = []
        if required_batches and set(batch_ids) != required_batches:
            errors.append(f"{prefix}.batch_ids must cover every required semantic batch")
        if status != "unverified/incomplete" and set(completed) != set(batch_ids):
            errors.append(f"{prefix}: a conclusive status requires all listed batches completed")
        if status == "unverified/incomplete" and set(completed) == set(batch_ids):
            errors.append(f"{prefix}: unverified/incomplete must identify incomplete batch coverage")
        if status == "not-established" and cell.get("dimension") == "semantic-routing":
            errors.append(f"{prefix}: not-established is reserved for all-batch aggregation")
        if status == "no-basis" and cell.get("dimension") != "semantic-routing":
            errors.append(f"{prefix}: no-basis is a per-batch routing status; aggregate it before a verdict")
        if set(completed) - set(batch_ids):
            errors.append(f"{prefix}.completed_batch_ids contains an unknown batch")
        evidence = assessment.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{prefix}.evidence must be a list")
        else:
            for ev_index, item in enumerate(evidence):
                _validate_evidence(
                    item, passage_map, reference_ids, errors, f"{prefix}.evidence[{ev_index}]"
                )
        if status in {"supported", "contradicted"} and not evidence:
            errors.append(f"{prefix}: {status} requires reference evidence")
    return errors


def _load_plan(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"Cannot read plan: {exc}")
    if plan.get("schema") != PLAN_SCHEMA:
        die(f"Unsupported plan schema: expected {PLAN_SCHEMA}")
    if plan.get("plan_hash") != _plan_hash(plan):
        die("Plan hash mismatch; the plan is stale or was modified")
    return plan


def _validate_artifacts(plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    source_path = plan.get("source", {}).get("canonical_path")
    if not source_path or not os.path.isfile(source_path):
        errors.append("canonical source artifact is missing")
    else:
        with open(source_path, "rb") as handle:
            if _sha256_bytes(handle.read()) != plan.get("source", {}).get("sha256"):
                errors.append("canonical source hash mismatch")
    original = plan.get("source", {}).get("original")
    if plan.get("input_kind") == "file" and original and os.path.isfile(original):
        with open(original, "rb") as handle:
            if _sha256_bytes(handle.read()) != plan.get("source_hash"):
                errors.append("original source hash mismatch")
    line_map_path = plan.get("source", {}).get("line_map_path")
    if not line_map_path or not os.path.isfile(line_map_path):
        errors.append("canonical source line map is missing")
    else:
        line_map, line_map_error = _read_json(line_map_path)
        if line_map_error or line_map != plan.get("line_map"):
            errors.append("canonical source line map does not match the plan")
        with open(line_map_path, "rb") as handle:
            if _sha256_bytes(handle.read()) != plan.get("source", {}).get("line_map_sha256"):
                errors.append("canonical source line map hash mismatch")
    for chunk in plan.get("chunks", []):
        path = chunk.get("path")
        if not path or not os.path.isfile(path):
            errors.append(f"chunk {chunk.get('index')} artifact is missing")
        else:
            with open(path, "rb") as handle:
                if _sha256_bytes(handle.read()) != chunk.get("sha256"):
                    errors.append(f"chunk {chunk.get('index')} hash mismatch")
    for reference in plan.get("reference_artifacts", []):
        path = reference.get("canonical_path")
        if not path or not os.path.isfile(path):
            errors.append(f"reference artifact {reference.get('id')} is missing")
        else:
            with open(path, "rb") as handle:
                if _sha256_bytes(handle.read()) != reference.get("sha256"):
                    errors.append(f"reference artifact {reference.get('id')} hash mismatch")
    for passage in plan.get("reference_passages", []):
        path = passage.get("canonical_path")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines() or [""]
            body = "\n".join(lines[passage["start_line"] - 1:passage["end_line"]])
            if _sha256_text(body) != passage.get("sha256"):
                errors.append(f"reference passage {passage.get('id')} hash mismatch")
        except (OSError, KeyError, TypeError):
            errors.append(f"reference passage {passage.get('id')} cannot be verified")
    return errors


def _read_json(path: str) -> tuple[Any, str | None]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)


def _attempts_path(plan: dict[str, Any]) -> str:
    return plan.get("attempts_path") or os.path.join(plan["run_workspace"], "attempts.json")


def _read_attempts(plan: dict[str, Any]) -> dict[str, Any]:
    path = _attempts_path(plan)
    data, error = _read_json(path)
    if error or not isinstance(data, dict) or data.get("run_id") != plan.get("run_id"):
        return {"schema": "CellAttempts/v1", "run_id": plan.get("run_id"), "cells": {}}
    data.setdefault("cells", {})
    return data


def _acquire_attempts_lock(path: str) -> str:
    """Acquire an atomic cross-process directory lock for the shared ledger."""
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + ATTEMPT_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            os.mkdir(lock_path)
            return lock_path
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_path)
                if age > ATTEMPT_LOCK_STALE_SECONDS:
                    os.rmdir(lock_path)
                    continue
            except (FileNotFoundError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out acquiring the attempts ledger lock")
            time.sleep(0.01)


def _replace_with_retry(source: str, target: str) -> None:
    """Atomically replace a Windows-visible ledger, retrying transient readers."""
    deadline = time.monotonic() + ATTEMPT_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _write_attempt(
    plan: dict[str, Any], cell_id: str, valid: bool, errors: list[str], result_hash: str | None,
) -> dict[str, Any]:
    path = _attempts_path(plan)
    lock_path = _acquire_attempts_lock(path)
    temp: str | None = None
    try:
        # The read happens only after the writer lock is held, so two validators
        # can never derive updates from the same stale ledger snapshot.
        ledger = _read_attempts(plan)
        record = ledger["cells"].setdefault(cell_id, {"attempts": []})
        attempts = record.setdefault("attempts", [])
        if record.get("status") in {"accepted", "FAILED"}:
            return record
        if len(attempts) < RESULT_MAX_ATTEMPTS:
            attempts.append({
                "number": len(attempts) + 1,
                "valid": valid,
                "errors": list(errors),
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
        record["status"] = (
            "accepted" if valid
            else ("FAILED" if len(attempts) >= RESULT_MAX_ATTEMPTS else "retry")
        )
        if valid:
            record["accepted_result_hash"] = result_hash
        record["retry_remaining"] = 0 if valid else max(
            0, RESULT_MAX_ATTEMPTS - len(attempts)
        )
        temp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        with open(temp, "w", encoding="utf-8", newline="") as handle:
            json.dump(ledger, handle, ensure_ascii=False, indent=2)
        _replace_with_retry(temp, path)
        temp = None
        return record
    finally:
        if temp:
            try:
                os.remove(temp)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(lock_path)
        except FileNotFoundError:
            pass


def _validate_dependency_results(
    plan: dict[str, Any], cell: dict[str, Any], result: dict[str, Any], results_dir: str,
) -> list[str]:
    """Validate dependency order, immutable hashes, observation inputs, and claims."""
    errors: list[str] = []
    cells = _cell_map(plan)
    ledger = _read_attempts(plan)
    dependency_payloads: dict[str, dict[str, Any]] = {}
    for dependency_id in cell.get("dependencies", []):
        dependency = cells.get(dependency_id)
        if not dependency:
            errors.append(f"unknown dependency in plan: {dependency_id}")
            continue
        record = ledger.get("cells", {}).get(dependency_id, {})
        if record.get("status") != "accepted":
            errors.append(f"dependency is not accepted: {dependency_id}")
            continue
        payload, parse_error = _read_json(_result_path(results_dir, dependency))
        if parse_error or not isinstance(payload, dict):
            errors.append(f"accepted dependency result is unavailable: {dependency_id}")
            continue
        if record.get("accepted_result_hash") != _json_hash(payload):
            errors.append(f"accepted dependency result hash changed: {dependency_id}")
            continue
        dependency_payloads[dependency_id] = payload

    if cell.get("stage") == "global":
        supplied = result.get("observation_inputs")
        if not isinstance(supplied, list):
            errors.append("global result requires observation_inputs with actual dependency hashes")
            supplied = []
        supplied_map = {
            item.get("cell_id"): item for item in supplied if isinstance(item, dict) and item.get("cell_id")
        }
        if set(supplied_map) != set(cell.get("dependencies", [])):
            errors.append("observation_inputs must exactly cover the global cell dependencies")
        total_chars = 0
        for dependency_id, payload in dependency_payloads.items():
            serialized = json.dumps(
                payload.get("observations", []), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
            total_chars += len(serialized)
            expected = {
                "cell_id": dependency_id,
                "sha256": _sha256_text(serialized),
                "serialized_chars": len(serialized),
            }
            supplied_item = supplied_map.get(dependency_id, {})
            for field, expected_value in expected.items():
                if supplied_item.get(field) != expected_value:
                    errors.append(f"observation_inputs for {dependency_id} has wrong {field}")
        if total_chars > int(cell.get("max_serialized_input_chars", MAX_REDUCER_INPUT_CHARS)):
            errors.append("actual dependency observations exceed the global reducer input gate")

    dimension = cell.get("dimension")
    expected_claims: set[str] | None = None
    if dimension == "semantic-routing":
        expected_claims = {
            observation.get("key")
            for payload in dependency_payloads.values()
            for observation in payload.get("observations", [])
            if observation.get("kind") == "claim" and observation.get("key")
        }
    elif dimension in {"grounding", "reference-coverage", "adjudication"}:
        expected_claims = {
            assessment.get("claim_key")
            for payload in dependency_payloads.values()
            for assessment in payload.get("reference_assessments", [])
            if assessment.get("claim_key")
        }
    if expected_claims is not None:
        actual_claims = {
            assessment.get("claim_key")
            for assessment in result.get("reference_assessments", [])
            if isinstance(assessment, dict) and assessment.get("claim_key")
        }
        if actual_claims != expected_claims:
            errors.append("reference_assessments must exactly preserve dependency claim-key coverage")
    return errors


def cmd_validate_cell(args: argparse.Namespace) -> None:
    plan = _load_plan(args.plan)
    cells = _cell_map(plan)
    data, parse_error = _read_json(args.result)
    result_cell_id = data.get("cell", {}).get("id") if isinstance(data, dict) else None
    cell_id = args.cell_id or result_cell_id
    if not cell_id or cell_id not in cells:
        errors = ["result cell id is missing or not present in the plan"]
        print(json.dumps({"valid": False, "status": "rejected", "errors": errors}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    errors = _validate_artifacts(plan)
    if parse_error:
        errors.append(f"result is not parseable JSON: {parse_error}")
    else:
        errors.extend(validate_cell_result(plan, cells[cell_id], data))
        errors.extend(_validate_dependency_results(
            plan, cells[cell_id], data, os.path.dirname(os.path.abspath(args.result))
        ))
    result_hash = _json_hash(data) if isinstance(data, dict) else None
    record = _write_attempt(plan, cell_id, not errors, errors, result_hash)
    terminal_mismatch = (
        record.get("status") == "accepted"
        and record.get("accepted_result_hash") != result_hash
    )
    terminal_failed = record.get("status") == "FAILED"
    if terminal_mismatch:
        errors.append("cell already accepted with a different immutable result hash")
    if terminal_failed and not errors:
        errors.append("cell exhausted initial + 2 attempts and is terminally FAILED")
    output = {"cell_id": cell_id, "valid": not errors, "status": record["status"], "errors": errors,
              "attempt_count": len(record["attempts"]), "retry_remaining": record["retry_remaining"]}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


def _result_path(cells_dir: str, cell: dict[str, Any]) -> str:
    return os.path.join(cells_dir, cell.get("result_file") or _cell_result_filename(cell["id"]))


def _collect_results(
    plan: dict[str, Any], cells_dir: str, *, require_accepted: bool = False,
) -> list[dict[str, Any]]:
    artifact_errors = _validate_artifacts(plan)
    ledger = _read_attempts(plan)
    output: list[dict[str, Any]] = []
    for cell in plan.get("cells", []):
        path = _result_path(cells_dir, cell)
        entry = {"cell": cell, "path": _norm(path), "status": "accepted", "errors": [],
                 "findings": [], "observations": [], "reference_assessments": [],
                 "dispatched": os.path.isfile(path), "valid": False}
        data, parse_error = _read_json(path)
        if parse_error:
            entry["errors"].append(f"missing or unparseable result: {parse_error}")
        else:
            entry["errors"].extend(validate_cell_result(plan, cell, data))
        entry["errors"].extend(artifact_errors)
        attempt_record = ledger.get("cells", {}).get(cell["id"], {})
        if not entry["errors"]:
            entry["valid"] = True
            accepted_hash = attempt_record.get("accepted_result_hash")
            if attempt_record.get("status") == "FAILED":
                entry["errors"].append("attempt ledger is terminally FAILED")
            elif attempt_record.get("status") == "accepted" and accepted_hash != _json_hash(data):
                entry["errors"].append("result differs from the immutable accepted result hash")
            elif require_accepted and attempt_record.get("status") != "accepted":
                entry["errors"].append("result has not been accepted by validate-cell")
        if entry["errors"]:
            entry["valid"] = False
            entry["status"] = "FAILED"
        else:
            entry["findings"] = data.get("findings", [])
            entry["observations"] = data.get("observations", [])
            entry["reference_assessments"] = data.get("reference_assessments", [])
            if attempt_record.get("status") != "accepted":
                entry["status"] = "valid_unaccepted"
            elif not entry["findings"]:
                entry["status"] = "empty"
        output.append(entry)
    return output


def cmd_status(args: argparse.Namespace) -> None:
    plan = _load_plan(args.plan)
    results = _collect_results(plan, args.cells_dir)
    ledger = _read_attempts(plan)
    rows = []
    for result in results:
        cell_id = result["cell"]["id"]
        attempt = ledger.get("cells", {}).get(cell_id, {})
        status = result["status"]
        errors = result["errors"]
        if status == "FAILED" and attempt.get("status") != "FAILED":
            if not result["dispatched"] and not attempt.get("attempts"):
                status = "pending"
                errors = []
            else:
                status = "retryable"
        rows.append({
            "cell_id": cell_id, "stage": result["cell"]["stage"], "status": status,
            "dispatched": result["dispatched"], "valid": result["valid"],
            "attempt_count": len(attempt.get("attempts", [])),
            "retry_remaining": attempt.get("retry_remaining", RESULT_MAX_ATTEMPTS),
            "errors": errors,
        })
    planned = len(results)
    dispatched = sum(1 for result in results if result["dispatched"])
    valid = sum(1 for result in results if result["valid"])
    retried = sum(
        1 for result in results
        if len(ledger.get("cells", {}).get(result["cell"]["id"], {}).get("attempts", [])) > 1
    )
    completed = sum(1 for row in rows if row["status"] in {"accepted", "empty"})
    failed = sum(1 for row in rows if row["status"] == "FAILED")
    unverified = sum(
        1 for result in results
        if any(
            assessment.get("status") == "unverified/incomplete"
            for assessment in result.get("reference_assessments", [])
            if isinstance(assessment, dict)
        )
    )
    counts = {
        "planned": planned, "dispatched": dispatched, "valid": valid,
        "retried": retried, "completed": completed, "failed": failed,
        "unverified": unverified,
    }
    print(json.dumps({
        "schema": "ReviewStatus/v1", "run_id": plan["run_id"],
        "complete": completed == planned and failed == 0 and unverified == 0,
        "counts": counts, "cells": rows,
    }, ensure_ascii=False, indent=2))


def _legacy_normalize_finding(finding: dict[str, Any], language: str) -> dict[str, Any]:
    """Normalize accepted v3 findings; aliases only support old caller helpers."""
    if "locations" in finding:
        return dict(finding)
    line = finding.get("line")
    quote = finding.get("quote") or ("[原文缺失]" if language == "zh" else "[Missing from source]")
    suggestion = finding.get("suggestion") or (
        "需作者确认；建议方向：根据审阅意见修订" if language == "zh"
        else "Author confirmation required; suggested direction: revise according to the review note"
    )
    return {
        **finding,
        "locations": [{"start_line": line, "end_line": line}] if isinstance(line, int) else [],
        "original_text": quote,
        "revised_text": suggestion,
        "change": finding.get("change") or suggestion,
        "reason": finding.get("reason") or finding.get("issue") or "Review finding",
        "evidence": finding.get("evidence", []),
    }


def _dedupe(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, str, str], dict[str, Any]] = {}
    for finding in findings:
        key = (_first_line(finding.get("locations", [])) or finding.get("line"),
               (finding.get("original_text") or finding.get("quote") or "").strip(),
               finding.get("category", "style"))
        if key not in merged:
            merged[key] = dict(finding)
            merged[key]["evidence"] = list(finding.get("evidence", []))
            continue
        current = merged[key]
        if SEVERITY_RANK.get(finding.get("severity", "medium"), 1) < SEVERITY_RANK.get(current.get("severity", "medium"), 1):
            current["severity"] = finding.get("severity")
        for field in ("revised_text", "change", "reason"):
            if len(str(finding.get(field, ""))) > len(str(current.get(field, ""))):
                current[field] = finding[field]
        seen = {_json_hash(item) for item in current.get("evidence", [])}
        current["evidence"].extend(item for item in finding.get("evidence", []) if _json_hash(item) not in seen)
    return list(merged.values())


def _template_blocks() -> dict[tuple[str, str], str]:
    try:
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        die(f"Authoritative report template cannot be read: {exc}")
    blocks: dict[tuple[str, str], str] = {}
    for name in ("REPORT", "SUMMARY", "FINDING", "PARTIAL", "NO_FINDINGS", "DIFF"):
        for language in ("ZH", "EN"):
            pattern = re.compile(
                rf"<!--\s*{name}:{language}\s*-->(.*?)<!--\s*/{name}:{language}\s*-->", re.DOTALL
            )
            match = pattern.search(text)
            if not match:
                die(f"Authoritative report template is missing block {name}:{language}")
            blocks[(name, language)] = match.group(1).strip("\r\n")
    return blocks


def _fill(template: str, values: dict[str, Any]) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


def _location_text(finding: dict[str, Any], language: str) -> str:
    rendered = []
    for location in finding.get("locations", []):
        start, end = _validate_location(location, [], "location")
        if start is None:
            continue
        prefix = "第" if language == "zh" else "Line "
        suffix = "行" if language == "zh" else ""
        rendered.append(f"{prefix}{start}{suffix}" if end == start else f"{prefix}{start}–{end}{suffix}")
    if finding.get("original_text") in {"[原文缺失]", "[Missing from source]"}:
        prefix = "建议插入位置" if language == "zh" else "Suggested insertion point"
        return prefix + (("：" if language == "zh" else ": ") + ", ".join(rendered) if rendered else "")
    if rendered:
        return ", ".join(rendered)
    return "建议插入位置" if language == "zh" else "Suggested insertion point"


def _reason_with_evidence(finding: dict[str, Any], plan: dict[str, Any], language: str) -> str:
    reason = finding.get("reason", "")
    reference_names = {
        item.get("id"): _display_name(item.get("original", "reference"))
        for item in plan.get("reference_artifacts", [])
    }
    citations = []
    for evidence in finding.get("evidence", []):
        reference = reference_names.get(evidence.get("reference_id"), evidence.get("reference_id"))
        citations.append(
            f"{reference}, {evidence.get('passage_id')}, {evidence.get('location')}: "
            f"{evidence.get('quote')}"
        )
    if not citations:
        return reason
    if language == "zh":
        return reason + "（" + "；".join(citations) + "）"
    return reason + " (" + "; ".join(citations) + ")"


def _uncovered_scope(results: list[dict[str, Any]], language: str) -> str:
    labels_zh = {
        "grammar-style": "语法与风格",
        "logic-consistency": "逻辑与一致性",
        "document-global": "全文一致性",
        "observation-reduce": "跨段归并",
        "claim-extraction": "主张提取",
        "passage-index": "参考材料索引",
        "semantic-routing": "语义检索",
        "grounding": "主张核验",
        "reference-coverage": "参考材料覆盖与遗漏",
        "adjudication": "矛盾与无依据内容复核",
    }
    labels_en = {
        "grammar-style": "grammar and style review",
        "logic-consistency": "logic and consistency review",
        "document-global": "document-wide consistency review",
        "observation-reduce": "cross-chunk observation reduction",
        "claim-extraction": "source claim extraction",
        "passage-index": "reference passage indexing",
        "semantic-routing": "semantic reference routing",
        "grounding": "claim grounding",
        "reference-coverage": "reference coverage and omissions",
        "adjudication": "contradiction and unsupported-claim adjudication",
    }
    scopes: list[str] = []
    for result in results:
        cell = result["cell"]
        label = (
            labels_zh.get(cell.get("dimension"), cell.get("dimension"))
            if language == "zh"
            else labels_en.get(cell.get("dimension"), cell.get("dimension", "review"))
        )
        if cell.get("lines"):
            label += (f"（第 {cell['lines']} 行）" if language == "zh" else f" (lines {cell['lines']})")
        if label not in scopes:
            scopes.append(label)
    return "、".join(scopes) if language == "zh" else "; ".join(scopes)


def _build_report(
    plan: dict[str, Any], results: list[dict[str, Any]], findings: list[dict[str, Any]], incomplete: bool,
    diff: str = "", uncovered_results: list[dict[str, Any]] | None = None,
) -> str:
    language = plan.get("resolved_language") or plan.get("language", {}).get("resolved", "en")
    code = language.upper()
    blocks = _template_blocks()
    failed = uncovered_results if uncovered_results is not None else [
        result for result in results if result["status"] == "FAILED"
    ]
    status_notice = _fill(
        blocks[("PARTIAL", code)], {"uncovered_scope": _uncovered_scope(failed, language)}
    ) if incomplete else ""
    if status_notice:
        status_notice += "\n\n"
    cards = []
    for number, finding in enumerate(findings, 1):
        cards.append(_fill(blocks[("FINDING", code)], {
            "index": number,
            "location": _location_text(finding, language),
            "original_text": finding.get("original_text", ""),
            "revised_text": finding.get("revised_text", ""),
            "change": finding.get("change", ""),
            "reason": _reason_with_evidence(finding, plan, language),
        }))
    if incomplete and not findings:
        findings_text = ""
    elif findings:
        findings_text = "\n\n".join(cards)
    else:
        findings_text = blocks[("NO_FINDINGS", code)]
    summary = "" if incomplete or not findings else _fill(
        blocks[("SUMMARY", code)], {"finding_count": len(findings)}
    )
    if summary:
        summary += "\n\n"
    diff_section = ("\n\n" + _fill(blocks[("DIFF", code)], {"diff": diff})) if diff else ""
    return _fill(blocks[("REPORT", code)], {
        "filename": _display_name(plan.get("input") or plan.get("file", "source")),
        "status_notice": status_notice,
        "summary_line": summary,
        "findings": findings_text,
        "diff_section": diff_section,
    }).strip() + "\n"


def _build_diff(plan: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    if not plan.get("source", {}).get("diff_applicable"):
        return ""
    path = plan.get("source", {}).get("original")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            original = handle.read().splitlines()
    except OSError:
        return ""
    revised = list(original)
    changed = False
    document_language = plan.get("document_language", "en")
    deletion_marker = "[删除该文本]" if document_language == "zh" else "[Delete this text]"
    edits = []
    for finding in findings:
        # v4 payloads deliberately contain only stage-specific review data;
        # a five-field finding is therefore fixable unless it explicitly says
        # otherwise.  This keeps opt-in diffs useful without asking agents to
        # echo a legacy envelope flag.
        if finding.get("fixable", True) is False:
            continue
        locations = finding.get("locations", [])
        if not locations:
            continue
        start, end = _validate_location(locations[0], [], "location")
        if start is None or end is None:
            continue
        old, new = finding.get("original_text"), finding.get("revised_text")
        if old and new:
            edits.append((start, end, old, new))
    # Descending application keeps earlier source locations stable. Overlapping
    # or non-verbatim edits are deliberately skipped.
    occupied_start = len(revised) + 1
    for start, end, old, new in sorted(edits, reverse=True):
        if start < 1 or end > len(revised) or end >= occupied_start:
            continue
        current = "\n".join(revised[start - 1:end])
        if old == current:
            replacement = [] if new == deletion_marker else new.splitlines()
            revised[start - 1:end] = replacement
            occupied_start = start
            changed = True
        elif start == end and old in revised[start - 1]:
            replacement = "" if new == deletion_marker else new
            revised[start - 1] = revised[start - 1].replace(old, replacement, 1)
            occupied_start = start
            changed = True
    if not changed:
        return ""
    name = os.path.basename(path)
    return "\n".join(difflib.unified_diff(original, revised, fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""))


def cmd_assemble(args: argparse.Namespace) -> None:
    plan = _load_plan(args.plan)
    results = _collect_results(plan, args.cells_dir, require_accepted=True)
    failed = [result for result in results if result["status"] == "FAILED" and result["cell"].get("required", True)]
    unverified = [
        result for result in results
        if any(
            assessment.get("status") == "unverified/incomplete"
            for assessment in result.get("reference_assessments", [])
            if isinstance(assessment, dict)
        )
    ]
    uncovered_results = failed + [result for result in unverified if result not in failed]
    incomplete = bool(uncovered_results)
    findings: list[dict[str, Any]] = []
    language = plan.get("resolved_language", "en")
    for result in results:
        if result["status"] in {"accepted", "empty"}:
            findings.extend(_legacy_normalize_finding(item, language) for item in result["findings"])
    deduped = _dedupe(findings)
    deduped.sort(key=lambda item: (_first_line(item.get("locations", [])) or sys.maxsize,
                                   SEVERITY_RANK.get(item.get("severity", "medium"), 1)))

    if incomplete and not args.accept_partial:
        # JSON/report is still emitted so callers receive actionable failed-cell
        # information; --accept-partial authorizes writing it to --output.
        pass
    diff_status = "not_requested"
    diff_reason = ""
    diff = ""
    if args.diff:
        if not plan.get("source", {}).get("diff_applicable"):
            diff_status = "not_applicable"
            diff_reason = "Diffs apply only to direct .md/.markdown/.txt sources, not converted or URL inputs."
        elif incomplete:
            diff_status = "suppressed_incomplete"
            diff_reason = "Diff suppressed because required review cells are incomplete."
        else:
            diff = _build_diff(plan, deduped)
            diff_status = "generated" if diff else "no_applicable_edits"
    report = _build_report(
        plan, results, deduped, incomplete, diff, uncovered_results=uncovered_results
    )
    output: dict[str, Any] = {
        "schema": "ReviewAssembly/v3",
        "run_id": plan["run_id"],
        "status": "partial" if incomplete else "complete",
        "complete": not incomplete,
        "incomplete": incomplete,
        "report": report,
        "deduped_count": len(deduped),
        "diff_status": diff_status,
        "diff_reason": diff_reason,
    }
    if diff:
        output["diff"] = diff
    if failed:
        output["failed_required_cells"] = [{
            "id": result["cell"]["id"], "stage": result["cell"]["stage"], "errors": result["errors"]
        } for result in failed]
    if unverified:
        output["unverified_cells"] = [
            {"id": result["cell"]["id"], "stage": result["cell"]["stage"]}
            for result in unverified
        ]
    if args.output:
        if incomplete and not args.accept_partial:
            die("Required cells FAILED; pass --accept-partial to write a visibly partial artifact")
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(json.dumps({
            "schema": output["schema"],
            "run_id": output["run_id"],
            "status": output["status"],
            "complete": output["complete"],
            "output": _norm(os.path.abspath(args.output)),
            "diff_status": output["diff_status"],
            "diff_reason": output["diff_reason"],
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))


###############################################################################
# ReviewRun/v4
#
# The v3 implementation above is intentionally left in place as a small set of
# compatibility helpers (chunking, conversion, and the report renderer).  The
# command surface below is the active protocol.  Keeping the deterministic
# helpers avoids reimplementing input conversion while deliberately refusing to
# load a v3 workspace.
###############################################################################

RUN_SCHEMA = "ReviewRun/v4"
RUN_STATUS_SCHEMA = "ReviewStatus/v4"
ASSEMBLY_SCHEMA = "ReviewAssembly/v4"
STATE_FILENAME = "run-state.json"
STATE_BACKUP_SUFFIX = ".bak"
CLAIM_INPUT_MAX_CHARS = 20_000
PROMPT_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "references"))


class _V4CapacityError(RuntimeError):
    """A deterministic planning bound was reached, not an agent failure."""


class _V4StateError(RuntimeError):
    """A run state cannot safely be read."""


def _v4_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _v4_hash(value: Any) -> str:
    return _sha256_text(_v4_json(value))


def _v4_state_hash(state: dict[str, Any]) -> str:
    material = dict(state)
    material.pop("state_hash", None)
    material.pop("_recovered_from_backup", None)
    return _v4_hash(material)


def _v4_write_bytes(path: str, payload: bytes) -> None:
    """Durably replace one file using a sibling temporary file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


def _v4_write_text(path: str, text: str) -> None:
    _v4_write_bytes(path, text.encode("utf-8"))


def _v4_save_state(path: str, state: dict[str, Any]) -> None:
    """Persist one generation and retain exactly one last-known-good backup."""
    state.pop("_recovered_from_backup", None)
    state["state_generation"] = int(state.get("state_generation", 0)) + 1
    state["state_hash"] = _v4_state_hash(state)
    if os.path.exists(path):
        with open(path, "rb") as handle:
            previous = handle.read()
        # The previous file was verified on load before this save.  Replacing
        # the backup first means a crash cannot leave an unverified new state as
        # the only recoverable copy.
        _v4_write_bytes(path + STATE_BACKUP_SUFFIX, previous)
    _v4_write_bytes(path, (_v4_json(state) + "\n").encode("utf-8"))


def _v4_read_state(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise _V4StateError(str(exc)) from exc
    if not isinstance(data, dict):
        raise _V4StateError("state root is not an object")
    if data.get("schema") == PLAN_SCHEMA:
        raise _V4StateError("ReviewPlan/v3 workspace detected; re-run the review (v3 workspaces are not migrated).")
    if data.get("schema") != RUN_SCHEMA:
        raise _V4StateError(f"expected {RUN_SCHEMA}, got {data.get('schema')!r}")
    if not isinstance(data.get("state_generation"), int) or not isinstance(data.get("state_hash"), str):
        raise _V4StateError("state generation or state hash is missing")
    if data["state_hash"] != _v4_state_hash(data):
        raise _V4StateError("state hash mismatch")
    return data


def _v4_load_state(path: str, *, restore_backup: bool) -> tuple[dict[str, Any], bool]:
    state_path = os.path.abspath(path)
    try:
        return _v4_read_state(state_path), False
    except _V4StateError as primary_error:
        backup_path = state_path + STATE_BACKUP_SUFFIX
        try:
            recovered = _v4_read_state(backup_path)
        except _V4StateError as backup_error:
            message = str(primary_error)
            if "ReviewPlan/v3 workspace" in message:
                raise _V4StateError(message)
            raise _V4StateError(
                f"state_corrupt: primary={primary_error}; backup={backup_error}"
            ) from backup_error
        if restore_backup:
            with open(backup_path, "rb") as handle:
                _v4_write_bytes(state_path, handle.read())
        recovered["_recovered_from_backup"] = True
        return recovered, True


def _v4_read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _v4_text_descriptor(
    value: str, *, workspace: str, label: str, number: int = 0,
) -> tuple[dict[str, Any], str]:
    """Return a source/reference descriptor without copying direct text files."""
    resolved, kind = _resolve_input(value)
    text = read_source(resolved)
    ext = _extension(resolved)
    direct = kind == "file" and ext in {".md", ".markdown", ".txt"}
    if direct:
        read_path = resolved
        snapshot = None
    else:
        name = "source.md" if label == "source" else f"reference_{number:03d}.md"
        read_path = os.path.join(workspace, "artifacts", name)
        _v4_write_text(read_path, text)
        snapshot = _norm(read_path)
    descriptor = {
        "original": _norm(resolved),
        "input_kind": kind,
        "extension": ext,
        "read_path": _norm(read_path),
        "canonical_snapshot": snapshot,
        "content_sha256": _sha256_text(text),
        "input_sha256": _original_input_hash(resolved, text),
        "diff_applicable": direct,
    }
    return descriptor, text


def _v4_claim_blocks(text: str) -> list[dict[str, Any]]:
    """Pack complete parser blocks into independently reviewable 20k inputs."""
    groups: list[dict[str, Any]] = []
    active: list[list[int]] = []
    active_chars = 0
    line = 1
    for _, block_lines in parse_blocks(text):
        end = line + len(block_lines) - 1
        block_text = "\n".join(block_lines)
        block_chars = len(block_text)
        if block_chars > CLAIM_INPUT_MAX_CHARS:
            raise _V4CapacityError(
                f"A structure-safe claim block at lines {line}-{end} exceeds "
                f"{CLAIM_INPUT_MAX_CHARS} characters and cannot be split."
            )
        separator = 1 if active else 0
        if active and active_chars + separator + block_chars > CLAIM_INPUT_MAX_CHARS:
            groups.append({"units": active})
            active, active_chars = [], 0
        active.append([line, end])
        active_chars += (1 if active_chars else 0) + block_chars
        line = end + 1
    if active:
        groups.append({"units": active})
    return groups or [{"units": [[1, 1]]}]


def _v4_create_cell(
    run: dict[str, Any], *, cell_id: str, stage: str, dimension: str,
    checks: list[str], dependencies: list[str], input_data: dict[str, Any],
    required: bool = True,
) -> dict[str, Any]:
    if cell_id in run["cells"]:
        raise RuntimeError(f"duplicate dynamic cell id: {cell_id}")
    cell = {
        "id": cell_id,
        "stage": stage,
        "dimension": dimension,
        "checks": list(checks),
        "dependencies": list(dependencies),
        "input": input_data,
        "input_hash": _v4_hash({"id": cell_id, "input": input_data, "checks": checks}),
        "required": required,
        "status": "pending",
        "attempts": [],
    }
    run["cells"][cell_id] = cell
    return cell


def _v4_cell_values(run: dict[str, Any], *, stage: str | None = None) -> list[dict[str, Any]]:
    cells = list(run["cells"].values())
    if stage is not None:
        cells = [cell for cell in cells if cell["stage"] == stage]
    return sorted(cells, key=lambda cell: cell["id"])


def _v4_dependencies_accepted(run: dict[str, Any], cell: dict[str, Any]) -> bool:
    return all(run["cells"].get(dep, {}).get("status") == "accepted" for dep in cell["dependencies"])


def _v4_dependency_hash(run: dict[str, Any], cell: dict[str, Any]) -> str:
    values = []
    for dep in sorted(cell["dependencies"]):
        upstream = run["cells"][dep]
        values.append([dep, upstream.get("accepted_payload_hash")])
    return _v4_hash({"input_hash": cell["input_hash"], "dependencies": values})


def _v4_dispatch_ready(run: dict[str, Any]) -> list[str]:
    """Bind ready cells to the generation that will be written next."""
    generation = int(run["state_generation"]) + 1
    dispatched: list[str] = []
    for cell in _v4_cell_values(run):
        if cell["status"] != "pending" or not _v4_dependencies_accepted(run, cell):
            continue
        cell["status"] = "dispatched"
        cell["dispatch"] = {
            "dispatch_id": uuid.uuid4().hex,
            "dependency_hash": _v4_dependency_hash(run, cell),
            "state_generation": generation,
        }
        dispatched.append(cell["id"])
    return dispatched


def _v4_manifest_text(descriptor: dict[str, Any]) -> str:
    return _v4_read_text(descriptor["read_path"])


def _v4_validate_manifest(run: dict[str, Any]) -> list[str]:
    """Recheck only immutable sources, references, and transient chunks."""
    errors: list[str] = []
    descriptors = [run["manifest"]["source"]] + list(run["manifest"].get("references", []))
    for descriptor in descriptors:
        try:
            text = _v4_manifest_text(descriptor)
        except OSError as exc:
            errors.append(f"missing artifact {descriptor.get('read_path')}: {exc}")
            continue
        if _sha256_text(text) != descriptor.get("content_sha256"):
            errors.append(f"artifact hash changed: {descriptor.get('original')}")
    for chunk in run["manifest"].get("chunks", []):
        try:
            text = _v4_read_text(chunk["path"])
        except OSError as exc:
            errors.append(f"missing chunk {chunk.get('path')}: {exc}")
            continue
        if _sha256_text(text) != chunk.get("sha256"):
            errors.append(f"chunk hash changed: {chunk.get('id')}")
    return errors


def _v4_source_lines(run: dict[str, Any]) -> list[str]:
    return _v4_manifest_text(run["manifest"]["source"]).splitlines()


def _v4_passage_map(run: dict[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    output: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for reference in run["manifest"].get("references", []):
        for passage in reference.get("passages", []):
            output[passage["id"]] = (reference, passage)
    return output


def _v4_passage_payload(run: dict[str, Any], passage_id: str) -> dict[str, Any]:
    reference, passage = _v4_passage_map(run)[passage_id]
    lines = _v4_manifest_text(reference).splitlines()
    text = "\n".join(lines[passage["start_line"] - 1:passage["end_line"]])
    if _sha256_text(text) != passage["sha256"]:
        raise _V4StateError(f"passage hash changed: {passage_id}")
    return {
        "id": passage_id,
        "reference": _display_name(reference["original"]),
        "start_line": passage["start_line"],
        "end_line": passage["end_line"],
        "text": text,
    }


def _v4_task_input(run: dict[str, Any], cell: dict[str, Any]) -> dict[str, Any]:
    data = cell["input"]
    if cell["stage"] == "local":
        return {
            "core_lines": data["core_lines"],
            "checks": cell["checks"],
            "text": _v4_read_text(data["chunk_path"]),
        }
    if cell["stage"] == "claim":
        lines = _v4_source_lines(run)
        parts = []
        for start, end in data["units"]:
            parts.append("\n".join(lines[start - 1:end]))
        return {"line_units": data["units"], "text": "\n".join(parts)}
    if cell["stage"] == "global":
        return {"checks": cell["checks"], "observations": data["observations"]}
    if cell["stage"] == "semantic":
        claims = {item["claim_id"]: item["claim"] for item in run["coverage"].get("claims", [])}
        return {
            "claims": [{"id": key, **claims[key]} for key in data["claim_ids"]],
            "passages": [_v4_passage_payload(run, value) for value in data["passage_ids"]],
            "collect_reference_facts": data["collect_reference_facts"],
        }
    if cell["stage"] == "coverage":
        return {
            "claims": run["coverage"].get("claims", []),
            "reference_facts": run["coverage"].get("reference_facts", []),
        }
    if cell["stage"] == "adjudication":
        return {"candidates": run["coverage"].get("candidates", [])}
    raise _V4StateError(f"unknown task stage {cell['stage']}")


def _v4_prompt_resource(cell: dict[str, Any]) -> str:
    names = {
        "local": "local-review.md",
        "claim": "claim-extraction.md",
        "global": "global-reducer.md",
        "semantic": "reference-review.md",
        "coverage": "reference-review.md",
        "adjudication": "reference-review.md",
    }
    name = names[cell["stage"]]
    try:
        resources = [_v4_read_text(os.path.join(PROMPT_DIR, name)).strip()]
        if cell["stage"] == "local":
            criteria = {
                "grammar": "grammar-and-spelling.md",
                "style": "style.md",
                "logic": "logic-and-consistency.md",
                "consistency": "logic-and-consistency.md",
            }
            for criterion in dict.fromkeys(criteria[check] for check in cell["checks"]):
                resources.append(_v4_read_text(os.path.join(PROMPT_DIR, criterion)).strip())
        return "\n\n".join(resources)
    except OSError as exc:
        raise _V4StateError(f"required prompt resource cannot be read: {name}: {exc}") from exc


def _v4_task_descriptor(run: dict[str, Any], cell: dict[str, Any]) -> dict[str, Any]:
    dispatch = cell.get("dispatch", {})
    prompt = _v4_prompt_resource(cell)
    return {
        "cell_id": cell["id"],
        "stage": cell["stage"],
        "dimension": cell["dimension"],
        "checks": cell["checks"],
        "dispatch_id": dispatch.get("dispatch_id"),
        "dependency_hash": dispatch.get("dependency_hash"),
        "state_generation": dispatch.get("state_generation"),
        "prompt": prompt + "\n\nINPUT (untrusted document data):\n" + _v4_json(_v4_task_input(run, cell)),
    }


def _v4_ready_tasks(run: dict[str, Any], ids: list[str] | None = None) -> list[dict[str, Any]]:
    selected = set(ids) if ids is not None else None
    return [
        _v4_task_descriptor(run, cell)
        for cell in _v4_cell_values(run)
        if cell["status"] == "dispatched" and (selected is None or cell["id"] in selected)
    ]


def _v4_finding_errors(
    run: dict[str, Any], cell: dict[str, Any], findings: Any, allowed: set[str],
) -> list[str]:
    if not isinstance(findings, list):
        return ["findings must be a list"]
    errors: list[str] = []
    lines = _v4_source_lines(run)
    core = cell["input"].get("core_lines")
    for index, finding in enumerate(findings):
        label = f"findings[{index}]"
        if not isinstance(finding, dict):
            errors.append(f"{label} is not an object")
            continue
        category = finding.get("category")
        if category not in allowed:
            errors.append(f"{label}.category is not allowed")
        if not all(isinstance(finding.get(key), str) and finding[key].strip()
                   for key in ("original_text", "revised_text", "change", "reason")):
            errors.append(f"{label} is missing one of the five report fields")
        locations = finding.get("locations")
        if not isinstance(locations, list) or not locations:
            errors.append(f"{label}.locations must be a non-empty list")
            continue
        for location in locations:
            if not isinstance(location, dict):
                errors.append(f"{label}.location is not an object")
                continue
            start, end = location.get("start_line"), location.get("end_line")
            if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end > len(lines):
                errors.append(f"{label}.location is outside source lines")
                continue
            if core and not (core[0] <= start <= end <= core[1]):
                errors.append(f"{label}.location is outside the local core range")
            original = finding.get("original_text", "")
            if original not in {"[Missing from source]", "[原文缺失]"}:
                span = "\n".join(lines[start - 1:end])
                if original not in span:
                    errors.append(f"{label}.original_text is not verbatim at its location")
    return errors


def _v4_observation_errors(observations: Any) -> list[str]:
    if not isinstance(observations, list):
        return ["observations must be a list"]
    errors = []
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict) or observation.get("kind") not in ALLOWED_OBSERVATION_KINDS:
            errors.append(f"observations[{index}] is not a supported observation")
    return errors


def _v4_validate_payload(run: dict[str, Any], cell: dict[str, Any], payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    if cell["stage"] == "local":
        expected = cell["checks"]
        errors = [] if payload.get("checks_completed") == expected else ["checks_completed does not match the task"]
        categories = set().union(*(CHECK_FINDING_CATEGORIES[check] for check in expected))
        return errors + _v4_finding_errors(run, cell, payload.get("findings"), categories) + _v4_observation_errors(payload.get("observations"))
    if cell["stage"] == "global":
        errors = []
        if payload.get("checks_completed") != cell["checks"]:
            errors.append("checks_completed does not match the task")
        allowed = {"style", "logic", "consistency"} if cell["checks"] else set()
        return errors + _v4_finding_errors(run, cell, payload.get("findings"), allowed) + _v4_observation_errors(payload.get("observations"))
    if cell["stage"] == "claim":
        claims = payload.get("claims")
        if not isinstance(claims, list):
            return ["claims must be a list"]
        return [f"claims[{index}] is not an object" for index, item in enumerate(claims) if not isinstance(item, dict)]
    if cell["stage"] == "semantic":
        assessments = payload.get("assessments")
        facts = payload.get("reference_facts")
        if not isinstance(assessments, list) or not isinstance(facts, list):
            return ["assessments and reference_facts must be lists"]
        expected = {(claim, passage) for claim in cell["input"]["claim_ids"] for passage in cell["input"]["passage_ids"]}
        actual: list[tuple[Any, Any]] = []
        errors = []
        for index, assessment in enumerate(assessments):
            if not isinstance(assessment, dict):
                errors.append(f"assessments[{index}] is not an object")
                continue
            pair = (assessment.get("claim_id"), assessment.get("passage_id"))
            actual.append(pair)
            if assessment.get("status") not in ASSESSMENT_STATUSES - {"not-established", "unverified/incomplete"}:
                errors.append(f"assessments[{index}] has an invalid status")
        if len(actual) != len(set(actual)) or set(actual) != expected:
            errors.append("semantic coverage does not check every claim-passage pair exactly once")
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict) or fact.get("passage_id") not in cell["input"]["passage_ids"]:
                errors.append(f"reference_facts[{index}] is not tied to this passage batch")
        return errors
    if cell["stage"] == "coverage":
        return _v4_finding_errors(run, cell, payload.get("findings"), {"omission"})
    if cell["stage"] == "adjudication":
        return _v4_finding_errors(run, cell, payload.get("findings"), {"fact", "contradiction", "unsupported"})
    return [f"unknown stage {cell['stage']}"]


def _v4_output_within_bound(cell: dict[str, Any], payload: dict[str, Any]) -> bool:
    if cell["stage"] == "claim":
        return len(_v4_json(payload.get("claims", []))) <= MAX_REDUCER_OUTPUT_CHARS
    return len(_v4_json(payload)) <= MAX_REDUCER_OUTPUT_CHARS


def _v4_stage_count(run: dict[str, Any], stage: str) -> int:
    return sum(1 for cell in run["cells"].values() if cell["stage"] == stage)


def _v4_require_stage_room(run: dict[str, Any], stage: str, adding: int) -> None:
    if _v4_stage_count(run, stage) + adding > HARD_MAX_CELLS_PER_STAGE:
        raise _V4CapacityError(
            f"{stage} dynamic cell count would exceed per-stage max_cells={HARD_MAX_CELLS_PER_STAGE}."
        )


def _v4_split_claim_cell(run: dict[str, Any], cell: dict[str, Any]) -> None:
    units = cell["input"].get("units", [])
    if len(units) < 2:
        raise _V4CapacityError(
            f"Claim output for {cell['id']} exceeds {MAX_REDUCER_OUTPUT_CHARS} characters and its input cannot be split safely."
        )
    _v4_require_stage_room(run, "claim", 2)
    midpoint = len(units) // 2
    cell["status"] = "split"
    cell["required"] = False
    for suffix, part in (("a", units[:midpoint]), ("b", units[midpoint:])):
        _v4_create_cell(
            run,
            cell_id=f"{cell['id']}.{suffix}",
            stage="claim",
            dimension="claim-extraction",
            checks=[],
            dependencies=cell["dependencies"],
            input_data={"units": part, "parent": cell["id"]},
        )


def _v4_dedupe_observations(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        if isinstance(value, dict):
            unique.setdefault(_v4_hash(value), value)
    return [unique[key] for key in sorted(unique)]


def _v4_schedule_global_layer(run: dict[str, Any], observations: list[dict[str, Any]], depth: int) -> None:
    batches = batch_observations(observations, MAX_REDUCER_INPUT_CHARS)
    _v4_require_stage_room(run, "global", len(batches))
    checks = [check for check in run["config"]["checks"] if check in {"style", "logic", "consistency"}]
    ids = []
    for index, batch in enumerate(batches, 1):
        is_final = len(batches) == 1
        cell = _v4_create_cell(
            run,
            cell_id=f"global:d{depth}:b{index:03d}",
            stage="global",
            dimension="document-global" if is_final else "observation-reduce",
            checks=checks if is_final else [],
            dependencies=run["progress"].get("global_dependencies", []),
            input_data={"depth": depth, "observations": batch["observations"], "serialized_chars": batch["serialized_chars"]},
        )
        ids.append(cell["id"])
    run["progress"]["global_active_layer"] = ids
    run["progress"]["global_depth"] = depth


def _v4_advance_global(run: dict[str, Any]) -> bool:
    progress = run["progress"]
    local = _v4_cell_values(run, stage="local")
    if not local or any(cell["status"] != "accepted" for cell in local):
        return False
    checks = [check for check in run["config"]["checks"] if check in {"style", "logic", "consistency"}]
    if progress.get("global_complete"):
        return False
    if len(run["manifest"]["chunks"]) <= 1 or not checks:
        progress["global_complete"] = True
        progress["global_skip_reason"] = "single_chunk_or_no_cross_chunk_check"
        return True
    active = progress.get("global_active_layer", [])
    if not active:
        observations = _v4_dedupe_observations([
            item for cell in local for item in cell.get("accepted_payload", {}).get("observations", [])
        ])
        if not observations:
            progress["global_complete"] = True
            progress["global_skip_reason"] = "no_observations"
            return True
        progress["global_dependencies"] = [cell["id"] for cell in local]
        _v4_schedule_global_layer(run, observations, 1)
        return True
    layer = [run["cells"][cell_id] for cell_id in active]
    if any(cell["status"] != "accepted" for cell in layer):
        return False
    if len(layer) == 1 and layer[0]["dimension"] == "document-global":
        progress["global_complete"] = True
        return True
    depth = int(progress["global_depth"])
    if depth >= MAX_REDUCER_DEPTH:
        raise _V4CapacityError("hierarchical reducer did not converge within max depth=3")
    observations = _v4_dedupe_observations([
        item for cell in layer for item in cell.get("accepted_payload", {}).get("observations", [])
    ])
    if not observations:
        progress["global_complete"] = True
        return True
    progress["global_dependencies"] = [cell["id"] for cell in layer]
    _v4_schedule_global_layer(run, observations, depth + 1)
    return True


def _v4_claim_records(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Deduplicate accepted claims while retaining the originating claim cell."""
    unique: dict[str, tuple[dict[str, Any], str]] = {}
    for cell in _v4_cell_values(run, stage="claim"):
        if cell["status"] != "accepted":
            continue
        for claim in cell.get("accepted_payload", {}).get("claims", []):
            key = _v4_hash(claim)
            unique.setdefault(key, (claim, cell["id"]))
    output = []
    for number, key in enumerate(sorted(unique), 1):
        claim, origin = unique[key]
        output.append({"claim_id": f"claim-{number:04d}", "claim": claim, "origin": origin})
    return output


def _v4_pack_passages(run: dict[str, Any], claims: list[dict[str, Any]]) -> list[list[str]]:
    passage_ids = sorted(_v4_passage_map(run))
    groups: list[list[str]] = []
    active: list[str] = []
    claim_data = [item["claim"] for item in claims]
    for passage_id in passage_ids:
        candidate = active + [passage_id]
        body = {
            "claims": claim_data,
            "passages": [_v4_passage_payload(run, value) for value in candidate],
        }
        if len(_v4_json(body)) > MAX_REDUCER_INPUT_CHARS:
            if not active:
                raise _V4CapacityError(f"semantic input cannot fit passage {passage_id} within 50,000 characters")
            groups.append(active)
            active = [passage_id]
        else:
            active = candidate
    if active:
        groups.append(active)
    return groups


def _v4_schedule_reference_semantics(run: dict[str, Any]) -> None:
    claims = _v4_claim_records(run)
    run["coverage"]["claims"] = claims
    claim_groups: list[list[dict[str, Any]]] = []
    if claims:
        by_origin: dict[str, list[dict[str, Any]]] = {}
        for claim in claims:
            by_origin.setdefault(claim["origin"], []).append(claim)
        claim_groups = [by_origin[key] for key in sorted(by_origin)]
    else:
        claim_groups = [[]]
    jobs: list[tuple[list[dict[str, Any]], list[str], bool]] = []
    for group_index, claim_group in enumerate(claim_groups, 1):
        for passages in _v4_pack_passages(run, claim_group):
            jobs.append((claim_group, passages, group_index == 1))
    _v4_require_stage_room(run, "semantic", len(jobs))
    expected: list[list[str]] = []
    for index, (claim_group, passages, collect_facts) in enumerate(jobs, 1):
        claim_ids = [item["claim_id"] for item in claim_group]
        dependencies = sorted({item["origin"] for item in claim_group})
        input_chars = len(_v4_json({
            "claims": [item["claim"] for item in claim_group],
            "passages": [_v4_passage_payload(run, value) for value in passages],
        }))
        cell = _v4_create_cell(
            run,
            cell_id=f"reference:semantic:{index:03d}",
            stage="semantic",
            dimension="facts-only" if not claim_ids else "semantic-routing",
            checks=[],
            dependencies=dependencies,
            input_data={
                "claim_ids": claim_ids,
                "passage_ids": passages,
                "collect_reference_facts": collect_facts,
                "serialized_chars": input_chars,
            },
        )
        expected.extend([[claim_id, passage] for claim_id in claim_ids for passage in passages])
    run["coverage"]["expected_matrix"] = expected
    run["progress"]["semantic_scheduled"] = True


def _v4_dedupe_facts(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for cell in cells:
        for fact in cell.get("accepted_payload", {}).get("reference_facts", []):
            unique.setdefault(_v4_hash(fact), fact)
    return [unique[key] for key in sorted(unique)]


def _v4_schedule_reference_final(run: dict[str, Any]) -> None:
    semantic = _v4_cell_values(run, stage="semantic")
    claims = run["coverage"].get("claims", [])
    expected = {(claim, passage) for claim, passage in run["coverage"].get("expected_matrix", [])}
    actual: dict[tuple[str, str], str] = {}
    for cell in semantic:
        for assessment in cell.get("accepted_payload", {}).get("assessments", []):
            actual[(assessment["claim_id"], assessment["passage_id"])] = assessment["status"]
    groundings = []
    candidates = []
    all_passages = sorted(_v4_passage_map(run))
    for claim in claims:
        claim_id = claim["claim_id"]
        statuses = [actual.get((claim_id, passage)) for passage in all_passages]
        if any(value is None for value in statuses):
            status = "unverified/incomplete"
        elif "contradicted" in statuses:
            status = "contradicted"
        elif statuses and all(value == "no-basis" for value in statuses):
            status = "no-basis"
        else:
            status = "supported"
        grounding = {"claim_id": claim_id, "status": status, "passages": all_passages}
        groundings.append(grounding)
        if status in {"contradicted", "no-basis"}:
            candidates.append({"claim": claim, "grounding": grounding})
    run["coverage"]["groundings"] = groundings
    run["coverage"]["reference_facts"] = _v4_dedupe_facts(semantic)
    run["coverage"]["candidates"] = candidates
    dependencies = [cell["id"] for cell in semantic]
    _v4_require_stage_room(run, "coverage", 1)
    _v4_create_cell(
        run,
        cell_id="reference:coverage",
        stage="coverage",
        dimension="reference-coverage",
        checks=[],
        dependencies=dependencies,
        input_data={"claim_count": len(claims), "fact_count": len(run["coverage"]["reference_facts"])},
    )
    if candidates:
        _v4_require_stage_room(run, "adjudication", 1)
        _v4_create_cell(
            run,
            cell_id="reference:adjudication",
            stage="adjudication",
            dimension="reference-adjudication",
            checks=[],
            dependencies=dependencies,
            input_data={"candidate_count": len(candidates)},
        )
    run["progress"]["reference_final_scheduled"] = True


def _v4_mark_incomplete_groundings(run: dict[str, Any]) -> bool:
    """Preserve explicit unverified coverage when a semantic batch is lost."""
    semantic = _v4_cell_values(run, stage="semantic")
    actual: dict[tuple[str, str], str] = {}
    for cell in semantic:
        if cell["status"] != "accepted":
            continue
        for assessment in cell.get("accepted_payload", {}).get("assessments", []):
            actual[(assessment["claim_id"], assessment["passage_id"])] = assessment["status"]
    passages = sorted(_v4_passage_map(run))
    groundings = []
    for claim in run["coverage"].get("claims", []):
        statuses = [actual.get((claim["claim_id"], passage)) for passage in passages]
        if any(status is None for status in statuses):
            status = "unverified/incomplete"
        elif "contradicted" in statuses:
            status = "contradicted"
        elif statuses and all(status == "no-basis" for status in statuses):
            status = "no-basis"
        else:
            status = "supported"
        groundings.append({"claim_id": claim["claim_id"], "status": status, "passages": passages})
    changed = run["coverage"].get("groundings") != groundings
    run["coverage"]["groundings"] = groundings
    run["coverage"]["reference_facts"] = _v4_dedupe_facts(
        [cell for cell in semantic if cell["status"] == "accepted"]
    )
    return changed


def _v4_advance_reference(run: dict[str, Any]) -> bool:
    if not run["manifest"].get("references"):
        return False
    progress = run["progress"]
    claims = _v4_cell_values(run, stage="claim")
    active_claims = [cell for cell in claims if cell["required"]]
    if not progress.get("semantic_scheduled"):
        if any(cell["status"] not in {"accepted", "split"} for cell in claims):
            return False
        if any(cell["status"] != "accepted" for cell in active_claims):
            return False
        _v4_schedule_reference_semantics(run)
        return True
    semantic = _v4_cell_values(run, stage="semantic")
    if semantic and not progress.get("reference_final_scheduled"):
        if all(cell["status"] == "accepted" for cell in semantic):
            _v4_schedule_reference_final(run)
            return True
        if any(cell["status"] == "failed" for cell in semantic):
            return _v4_mark_incomplete_groundings(run)
    return False


def _v4_advance(run: dict[str, Any]) -> None:
    while True:
        changed = _v4_advance_global(run)
        changed = _v4_advance_reference(run) or changed
        if not changed:
            return


def _v4_new_run(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    cfg = load_config()
    chunk_lines = args.chunk_lines or int(cfg["chunk_lines"])
    max_chunks = min(int(cfg["max_chunks"]), HARD_MAX_CHUNKS)
    if chunk_lines < 1 or max_chunks < 1:
        die("Configured chunk_lines and max_chunks must be positive")
    if args.workspace:
        workspace = os.path.abspath(args.workspace)
        os.makedirs(workspace, exist_ok=True)
        if os.path.exists(os.path.join(workspace, STATE_FILENAME)):
            die(f"Workspace already contains {STATE_FILENAME}; use its --state to resume.")
    else:
        workspace = tempfile.mkdtemp(prefix="content-review-")
    source, source_text = _v4_text_descriptor(args.input, workspace=workspace, label="source")
    chunks = chunk_text(source_text, chunk_lines)
    if len(chunks) > max_chunks:
        die(f"Chunk count {len(chunks)} exceeds max_chunks={max_chunks}; increase --chunk-lines or split the document.", code=2)
    manifest_chunks = []
    for chunk in chunks:
        if len(chunk["text"]) > MAX_CHUNK_CHARS:
            die(f"Chunk {chunk['index']} exceeds max chunk character budget {MAX_CHUNK_CHARS}.", code=2)
        path = os.path.join(workspace, "chunks", f"chunk_{chunk['index']:03d}.md")
        _v4_write_text(path, chunk["text"])
        manifest_chunks.append({
            "id": f"chunk-{chunk['index']:03d}", "index": chunk["index"],
            "start_line": chunk["start"], "end_line": chunk["end"],
            "path": _norm(path), "sha256": _sha256_text(chunk["text"]),
        })
    references = []
    for number, raw in enumerate(_expand_references(args.references), 1):
        try:
            descriptor, reference_text = _v4_text_descriptor(raw, workspace=workspace, label="reference", number=number)
            descriptor["id"] = f"reference-{number:03d}"
            descriptor["passages"] = _reference_passages(reference_text, descriptor["id"])
        except _V4CapacityError as exc:
            die(str(exc), code=2)
        references.append(descriptor)
    checks = compute_checks(args.focus)
    run = {
        "schema": RUN_SCHEMA,
        "run_id": uuid.uuid4().hex,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "state_generation": 0,
        "manifest": {
            "source": source,
            "chunks": manifest_chunks,
            "references": references,
            "requested_language": args.language,
            "resolved_language": resolve_language(args.language, source_text),
            "document_language": detect_language(source_text),
        },
        "config": {"focus": args.focus, "checks": checks, "chunk_lines": chunk_lines, "keep_workspace": bool(args.keep_workspace)},
        "cells": {},
        "coverage": {"claims": [], "expected_matrix": [], "groundings": [], "reference_facts": [], "candidates": []},
        "progress": {},
    }
    local_dimensions = compute_local_dimensions(args.focus)
    _v4_require_stage_room(run, "local", len(local_dimensions) * len(manifest_chunks))
    for chunk in manifest_chunks:
        for dimension, dimension_checks in local_dimensions:
            _v4_create_cell(
                run,
                cell_id=f"local:{dimension}:{chunk['index']:03d}",
                stage="local",
                dimension=dimension,
                checks=dimension_checks,
                dependencies=[],
                input_data={"chunk_id": chunk["id"], "chunk_path": chunk["path"], "core_lines": [chunk["start_line"], chunk["end_line"]]},
            )
    if references:
        try:
            blocks = _v4_claim_blocks(source_text)
        except _V4CapacityError as exc:
            die(str(exc), code=2)
        _v4_require_stage_room(run, "claim", len(blocks))
        for index, block in enumerate(blocks, 1):
            _v4_create_cell(
                run,
                cell_id=f"reference:claim:{index:03d}",
                stage="claim",
                dimension="claim-extraction",
                checks=[],
                dependencies=[],
                input_data=block,
            )
    return run, os.path.join(workspace, STATE_FILENAME)


def _v4_counts(run: dict[str, Any]) -> dict[str, int]:
    cells = list(run["cells"].values())
    return {
        "planned": len(cells),
        "dispatched": sum(cell["status"] == "dispatched" for cell in cells),
        "accepted": sum(cell["status"] == "accepted" for cell in cells),
        "retryable": sum(cell["status"] == "pending" and cell["attempts"] for cell in cells),
        "failed": sum(cell["status"] == "failed" for cell in cells),
        "split": sum(cell["status"] == "split" for cell in cells),
    }


def _v4_status_payload(run: dict[str, Any], state_path: str, *, recovered: bool = False) -> dict[str, Any]:
    cells = []
    for cell in _v4_cell_values(run):
        cells.append({
            "cell_id": cell["id"], "stage": cell["stage"], "dimension": cell["dimension"],
            "status": cell["status"], "attempt_count": len(cell["attempts"]),
            "retry_remaining": max(0, RESULT_MAX_ATTEMPTS - len(cell["attempts"])),
        })
    required = [cell for cell in run["cells"].values() if cell["required"]]
    unverified = any(item.get("status") == "unverified/incomplete" for item in run["coverage"].get("groundings", []))
    return {
        "schema": RUN_STATUS_SCHEMA,
        "run_id": run["run_id"],
        "state": _norm(os.path.abspath(state_path)),
        "state_generation": run["state_generation"],
        "recovered_from_backup": recovered,
        "complete": bool(required) and all(cell["status"] == "accepted" for cell in required) and not unverified and not run.get("stop_reason"),
        "counts": _v4_counts(run),
        "cells": cells,
        "stop_reason": run.get("stop_reason"),
    }


def cmd_plan_v4(args: argparse.Namespace) -> None:
    run, state_path = _v4_new_run(args)
    dispatched = _v4_dispatch_ready(run)
    _v4_save_state(state_path, run)
    output = _v4_status_payload(run, state_path)
    output["ready"] = _v4_ready_tasks(run, dispatched)
    print(json.dumps(output, ensure_ascii=False, indent=2))


def _v4_read_results(path: str) -> list[dict[str, Any]]:
    try:
        text = sys.stdin.read() if path == "-" else _v4_read_text(path)
        raw = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"Cannot read --results: {exc}")
    if isinstance(raw, dict):
        raw = raw.get("results")
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        die("--results must be a JSON array or an object with a results array")
    return raw


def _v4_record_invalid(cell: dict[str, Any], dispatch_id: str, payload_hash: str, errors: list[str]) -> None:
    cell["attempts"].append({
        "dispatch_id": dispatch_id,
        "payload_hash": payload_hash,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "errors": errors,
    })
    cell["status"] = "failed" if len(cell["attempts"]) >= RESULT_MAX_ATTEMPTS else "pending"


def cmd_ingest_v4(args: argparse.Namespace) -> None:
    try:
        run, recovered = _v4_load_state(args.state, restore_backup=True)
    except _V4StateError as exc:
        die(str(exc))
    manifest_errors = _v4_validate_manifest(run)
    if manifest_errors:
        die("Immutable source/reference validation failed: " + "; ".join(manifest_errors))
    results = _v4_read_results(args.results)
    outcomes = []
    changed = False
    for item in results:
        cell_id = item.get("cell_id")
        dispatch_id = item.get("dispatch_id")
        payload = item.get("payload")
        payload_hash = _v4_hash(payload)
        cell = run["cells"].get(cell_id) if isinstance(cell_id, str) else None
        if cell is None:
            outcomes.append({"cell_id": cell_id, "status": "stale", "reason": "unknown cell"})
            continue
        dispatch = cell.get("dispatch", {})
        if cell["status"] == "accepted":
            if dispatch_id == dispatch.get("dispatch_id") and payload_hash == cell.get("accepted_payload_hash"):
                outcomes.append({"cell_id": cell_id, "status": "idempotent"})
            else:
                outcomes.append({"cell_id": cell_id, "status": "conflict", "reason": "accepted dispatch has a different payload"})
            continue
        if cell["status"] != "dispatched" or dispatch_id != dispatch.get("dispatch_id"):
            outcomes.append({"cell_id": cell_id, "status": "stale", "reason": "not the current dispatch"})
            continue
        if (item.get("state_generation") != run["state_generation"]
                or item.get("dependency_hash") != dispatch.get("dependency_hash")):
            outcomes.append({"cell_id": cell_id, "status": "stale", "reason": "old state generation or dependency hash"})
            continue
        if cell["stage"] == "claim" and isinstance(payload, dict) and isinstance(payload.get("claims"), list) and not _v4_output_within_bound(cell, payload):
            try:
                _v4_split_claim_cell(run, cell)
            except _V4CapacityError as exc:
                run["stop_reason"] = str(exc)
                changed = True
                _v4_save_state(args.state, run)
                die(str(exc), code=2)
            outcomes.append({"cell_id": cell_id, "status": "split", "reason": "claim payload exceeded output budget"})
            changed = True
            continue
        errors = _v4_validate_payload(run, cell, payload)
        if not errors and not _v4_output_within_bound(cell, payload):
            errors.append(f"payload exceeds {MAX_REDUCER_OUTPUT_CHARS} characters")
        if errors:
            _v4_record_invalid(cell, str(dispatch_id), payload_hash, errors)
            outcomes.append({"cell_id": cell_id, "status": "invalid", "errors": errors})
            changed = True
            continue
        cell["status"] = "accepted"
        cell["accepted_payload"] = payload
        cell["accepted_payload_hash"] = payload_hash
        outcomes.append({"cell_id": cell_id, "status": "accepted"})
        changed = True
    if changed:
        try:
            _v4_advance(run)
            dispatched = _v4_dispatch_ready(run)
        except _V4CapacityError as exc:
            run["stop_reason"] = str(exc)
            _v4_save_state(args.state, run)
            die(str(exc), code=2)
        _v4_save_state(args.state, run)
    else:
        dispatched = []
    output = _v4_status_payload(run, args.state, recovered=recovered)
    output["outcomes"] = outcomes
    output["ready"] = _v4_ready_tasks(run, dispatched)
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_status_v4(args: argparse.Namespace) -> None:
    try:
        run, recovered = _v4_load_state(args.state, restore_backup=False)
    except _V4StateError as exc:
        die(str(exc))
    output = _v4_status_payload(run, args.state, recovered=recovered)
    output["ready"] = _v4_ready_tasks(run)
    print(json.dumps(output, ensure_ascii=False, indent=2))


def _v4_uncovered_results(run: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for cell in _v4_cell_values(run):
        if cell["required"] and cell["status"] != "accepted":
            output.append({"cell": cell, "status": "FAILED", "errors": [cell["status"]]})
    for grounding in run["coverage"].get("groundings", []):
        if grounding.get("status") == "unverified/incomplete":
            output.append({"cell": {"dimension": "claim-grounding", "stage": "reference"}, "status": "FAILED", "errors": ["unverified/incomplete"]})
    return output


def _v4_assemble_findings(run: dict[str, Any]) -> list[dict[str, Any]]:
    values = []
    for cell in _v4_cell_values(run):
        if cell["status"] == "accepted" and cell["stage"] in {"local", "global", "coverage", "adjudication"}:
            values.extend(cell.get("accepted_payload", {}).get("findings", []))
    return _dedupe(values)


def _v4_cleanup_workspace(state_path: str) -> None:
    workspace = os.path.dirname(os.path.abspath(state_path))
    expected_state = os.path.join(workspace, STATE_FILENAME)
    if os.path.normcase(os.path.abspath(state_path)) != os.path.normcase(expected_state):
        raise _V4StateError("refusing to clean a workspace whose state file is not run-state.json")
    # workspace is the explicit run directory selected by plan, never a
    # computed parent; resolving it before deletion prevents path traversal.
    resolved = os.path.realpath(workspace)
    if not os.path.isfile(os.path.join(resolved, STATE_FILENAME)):
        raise _V4StateError("refusing to clean workspace without its run-state.json")
    shutil.rmtree(resolved)


def cmd_assemble_v4(args: argparse.Namespace) -> None:
    try:
        run, recovered = _v4_load_state(args.state, restore_backup=True)
    except _V4StateError as exc:
        die(str(exc))
    manifest_errors = _v4_validate_manifest(run)
    if manifest_errors:
        die("Immutable source/reference validation failed: " + "; ".join(manifest_errors))
    findings = _v4_assemble_findings(run)
    findings.sort(key=lambda item: (_first_line(item.get("locations", [])) or sys.maxsize,
                                   SEVERITY_RANK.get(item.get("severity", "medium"), 1)))
    uncovered = _v4_uncovered_results(run)
    incomplete = bool(uncovered or run.get("stop_reason"))
    source = run["manifest"]["source"]
    pseudo_plan = {
        "resolved_language": run["manifest"]["resolved_language"],
        "document_language": run["manifest"]["document_language"],
        "input": source["original"],
        "source": {"diff_applicable": source["diff_applicable"], "original": source["read_path"]},
        "reference_artifacts": run["manifest"].get("references", []),
    }
    diff_status, diff_reason, diff = "not_requested", "", ""
    if args.diff:
        if not source["diff_applicable"]:
            diff_status = "not_applicable"
            diff_reason = "Diffs apply only to direct .md/.markdown/.txt sources, not converted or URL inputs."
        elif incomplete:
            diff_status = "suppressed_incomplete"
            diff_reason = "Diff suppressed because required review cells are incomplete."
        else:
            diff = _build_diff(pseudo_plan, findings)
            diff_status = "generated" if diff else "no_applicable_edits"
    report = _build_report(pseudo_plan, [], findings, incomplete, diff, uncovered_results=uncovered)
    output = {
        "schema": ASSEMBLY_SCHEMA,
        "run_id": run["run_id"],
        "state": _norm(os.path.abspath(args.state)),
        "state_recovered_from_backup": recovered,
        "status": "partial" if incomplete else "complete",
        "complete": not incomplete,
        "incomplete": incomplete,
        "report": report,
        "deduped_count": len(findings),
        "diff_status": diff_status,
        "diff_reason": diff_reason,
    }
    if incomplete and args.output and not args.accept_partial:
        die("Required cells are incomplete; pass --accept-partial to write a visibly partial artifact")
    if args.output:
        _v4_write_text(os.path.abspath(args.output), report)
        stdout_output = {key: value for key, value in output.items() if key != "report"}
        stdout_output["output"] = _norm(os.path.abspath(args.output))
        print(json.dumps(stdout_output, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    keep = bool(args.keep_workspace or run["config"].get("keep_workspace"))
    if not incomplete and not keep:
        try:
            _v4_cleanup_workspace(args.state)
        except _V4StateError as exc:
            print(f"WARNING: report succeeded but workspace was retained: {exc}", file=sys.stderr)


def show_version() -> None:
    print(f"content-review review_plan v{VERSION} ({RUN_SCHEMA})")


def main() -> None:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default="")
    pre.add_argument("--version", action="store_true")
    early, _ = pre.parse_known_args()
    if early.version:
        show_version()
        raise SystemExit(0)
    global CONFIG_PATH
    if early.config:
        CONFIG_PATH = os.path.abspath(early.config)
    load_config()

    parser = argparse.ArgumentParser(description="content-review v4 dynamic review-run planner")
    parser.add_argument("--config", default="", help="Path to config.json")
    parser.add_argument("--version", action="store_true", help="Show version")
    sub = parser.add_subparsers(dest="command")

    plan = sub.add_parser("plan", help="Create one ReviewRun/v4 state and emit its initial ready tasks")
    plan.add_argument("--input", required=True)
    plan.add_argument("--focus", choices=sorted(FOCUS_CHECKS), default="all")
    plan.add_argument("--references", nargs="*", default=None)
    plan.add_argument("--language", choices=("auto", "en", "zh"), default="auto")
    plan.add_argument("--chunk-lines", type=int, default=None)
    plan.add_argument("--workspace", default=None, help="Run directory; defaults to a system temporary directory")
    plan.add_argument("--keep-workspace", action="store_true")

    ingest = sub.add_parser("ingest", help="Validate a batch of stage payloads and advance the dynamic DAG")
    ingest.add_argument("--state", required=True)
    ingest.add_argument("--results", required=True, help="JSON file path or - for stdin")

    status = sub.add_parser("status", help="Read state, retry accounting, and currently ready tasks")
    status.add_argument("--state", required=True)

    assemble = sub.add_parser("assemble", help="Verify final artifacts and render a ReviewRun/v4 report")
    assemble.add_argument("--state", required=True)
    assemble.add_argument("--output", default=None)
    assemble.add_argument("--accept-partial", action="store_true")
    assemble.add_argument("--diff", action="store_true", help="Opt in to a diff for direct text sources")
    assemble.add_argument("--keep-workspace", action="store_true")

    args = parser.parse_args()
    if args.command == "plan":
        cmd_plan_v4(args)
    elif args.command == "ingest":
        cmd_ingest_v4(args)
    elif args.command == "status":
        cmd_status_v4(args)
    elif args.command == "assemble":
        cmd_assemble_v4(args)
    else:
        parser.print_help()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
