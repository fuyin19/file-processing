"""Deterministic v3 run-state primitives for the translate skill.

The language-model orchestration remains in ``SKILL.md``.  This module owns the
parts that must be reproducible without a model: run identity, manifests,
artifact hashes, cache keys, occurrence ledgers, and atomic state transitions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "3.0"
PIPELINE_REVISION = "translate-v3"
TERMINAL_OCCURRENCE_STATES = {
    "applied", "preserved", "transliterated", "user_confirmed",
    "not_applicable", "conflict", "unresolved",
}
STAGE_STATES = {
    "pending", "running", "completed", "failed_transient",
    "failed_permanent", "blocked_budget",
}


def runtime_fingerprint(runtime_mode: str) -> str:
    """Fingerprint every runtime-controlled semantic dependency.

    The orchestrator supplies provider/model/decode identity through
    ``TRANSLATE_AGENT_FINGERPRINT``.  Keeping that value in the fingerprint
    makes a resumed run reject artifacts produced by another model/runtime.
    """
    return fingerprint(
        pipeline_revision=PIPELINE_REVISION,
        schema_version=SCHEMA_VERSION,
        runtime_mode=runtime_mode,
        agent_fingerprint=os.environ.get("TRANSLATE_AGENT_FINGERPRINT", "unavailable"),
        prompt_digest=os.environ.get("TRANSLATE_PROMPT_DIGEST", "skill-managed"),
        retrieval_revision=os.environ.get("TRANSLATE_RETRIEVAL_REVISION", "semantic-v3"),
        validator_revision="v3",
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(**parts: Any) -> str:
    """Return a stable content-addressed key for a semantic-stage input."""
    return sha256_text(_canonical(parts).decode("utf-8"))


def manifest_digest(manifest: dict) -> str:
    """Hash a manifest without its convenience self-reference field."""
    stable = dict(manifest)
    stable.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical(stable)).hexdigest()


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> str:
    """Atomically publish JSON and return its content hash."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return sha256_text(payload)


def atomic_write_text(path: str | os.PathLike[str], text: str) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return sha256_text(text)


def cache_path(cache_root: str | os.PathLike[str], key: str) -> Path:
    """Return a sharded immutable cache path for a hexadecimal cache key."""
    if not re.fullmatch(r"[0-9a-f]{32,128}", key):
        raise ValueError("cache key must be a hexadecimal digest")
    return Path(cache_root) / key[:2] / f"{key}.json"


def publish_cache_json(cache_root: str | os.PathLike[str], key: str, value: Any) -> dict:
    """Content-addressed, immutable JSON publication safe for competing writers.

    ``os.replace`` is atomic but still overwrites an existing key.  Publish via
    an atomic hard-link instead: exactly one competing writer creates the final
    path and every other writer reads that first value.  Both names live in the
    same directory, so the link operation is atomic on the supported filesystems.
    """
    path = cache_path(cache_root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            return {"path": str(path), "sha256": sha256_file(path), "hit": True,
                    "value": cached}
        except (OSError, json.JSONDecodeError):
            # A corrupt cache entry is never reused. Atomic replacement repairs it.
            pass
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            return {"path": str(path), "sha256": sha256_file(path), "hit": False,
                    "value": value}
        except FileExistsError:
            with open(path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            return {"path": str(path), "sha256": sha256_file(path), "hit": True,
                    "value": cached}
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def new_run_id() -> str:
    return "tr-" + uuid.uuid4().hex


def source_line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _normalise_source(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def term_id(source: str, source_hash: str, first_offset: int) -> str:
    return fingerprint(kind="term", source=_normalise_source(source),
                       source_hash=source_hash, first_offset=first_offset)[:24]


def occurrence_id(term: str, source_hash: str, offset: int, length: int) -> str:
    return fingerprint(kind="occurrence", term_id=term, source_hash=source_hash,
                       offset=offset, length=length)[:24]


def build_occurrence_ledger(source_text: str, chunks: list[dict], terms: list[dict]) -> list[dict]:
    """Build an auditable occurrence universe from structured glossary terms.

    Only terms supplied by the source-term/grounding stages are eligible.  This
    deliberately avoids pretending that a regex can exhaustively discover all
    terminology without the orchestration stage.
    """
    src_hash = sha256_text(source_text)
    ledger: list[dict] = []
    for entry in terms:
        source = str(entry.get("source") or "").strip()
        if not source:
            continue
        flags = entry.get("type") or entry.get("kind") or "common"
        pattern = re.escape(source)
        if re.search(r"[A-Za-z0-9]", source):
            pattern = r"(?<!\w)" + pattern + r"(?!\w)"
            matches = re.finditer(pattern, source_text, re.IGNORECASE)
        else:
            matches = re.finditer(pattern, source_text)
        found = list(matches)
        if not found:
            continue
        tid = term_id(source, src_hash, found[0].start())
        is_key = flags in {"proper-noun", "jargon", "acronym"} or (
            " " in source and len(found) >= 2
        )
        for match in found:
            offset = match.start()
            chunk = next((c for c in chunks if c["start"] <= source_line(source_text, offset) <= c["end"]), None)
            line = source_line(source_text, offset)
            ledger.append({
                "schema_version": SCHEMA_VERSION,
                "term_id": tid,
                "occurrence_id": occurrence_id(tid, src_hash, offset, len(match.group(0))),
                "source": source,
                "source_hash": src_hash,
                "source_offset": offset,
                "source_length": len(match.group(0)),
                "source_line": line,
                "chunk": chunk.get("index") if chunk else None,
                "chunk_line": line - chunk["start"] + 1 if chunk else None,
                "key_item": is_key,
                "target": entry.get("target"),
                "origin": entry.get("origin", "reference"),
                "evidence": entry.get("evidence", []),
                "disposition": None,
                "reason": None,
            })
    return ledger


def validate_occurrence_ledger(ledger: list[dict]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for item in ledger:
        oid = item.get("occurrence_id")
        if not oid or oid in seen:
            errors.append(f"duplicate or missing occurrence_id: {oid!r}")
        seen.add(oid)
        disposition = item.get("disposition")
        if disposition not in TERMINAL_OCCURRENCE_STATES:
            errors.append(f"occurrence {oid} has no terminal disposition")
        if disposition in {"not_applicable", "preserved", "transliterated", "user_confirmed", "conflict"} and not item.get("reason"):
            errors.append(f"occurrence {oid} disposition {disposition} requires a reason")
        if disposition in {"unresolved", "conflict"}:
            errors.append(f"occurrence {oid} has unresolved disposition {disposition}")
        if item.get("key_item") and disposition is None:
            errors.append(f"key occurrence {oid} is unresolved")
    return errors


def validate_translation_occurrences(ledger: list[dict], payload: dict) -> list[str]:
    """Require a translation self-audit to acknowledge every ledger occurrence.

    A target string appearing once in a chunk cannot prove that multiple source
    occurrences were handled. The agent-owned translation artifact therefore
    carries ``occurrence_ids`` and Python compares it against the complete
    deterministic ledger universe.
    """
    if not isinstance(payload, dict):
        return ["translation artifact is not an object"]
    ids = payload.get("occurrence_ids")
    if ids is None and isinstance(payload.get("self_audit"), dict):
        ids = payload["self_audit"].get("occurrence_ids")
    if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
        return ["translation artifact requires occurrence_ids string list"]
    expected = {str(item.get("occurrence_id")) for item in ledger if item.get("occurrence_id")}
    actual = set(ids)
    errors = []
    if len(ids) != len(actual):
        errors.append("translation artifact contains duplicate occurrence_ids")
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        errors.append(f"translation artifact misses occurrence_ids: {', '.join(missing[:5])}")
    if unknown:
        errors.append(f"translation artifact has unknown occurrence_ids: {', '.join(unknown[:5])}")
    return errors


def make_manifest(*, source_path: str, source_hash: str, language: str,
                  runtime_mode: str, quality_mode: str, workspace: str,
                  reference_hashes: list[dict], config: dict, stages: list[str],
                  run_id: str | None = None) -> dict:
    run_id = run_id or new_run_id()
    current_runtime_fingerprint = runtime_fingerprint(runtime_mode)
    config_digest = fingerprint(config=config)
    input_fingerprint = fingerprint(
        source_hash=source_hash, references=reference_hashes, language=language,
        config=config, runtime_fingerprint=current_runtime_fingerprint,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime_mode": runtime_mode,
        "quality_mode": quality_mode,
        "language": language,
        "source": {"path": os.path.abspath(source_path), "sha256": source_hash},
        "references": reference_hashes,
        "workspace": os.path.abspath(workspace),
        "runtime_fingerprint": current_runtime_fingerprint,
        "config": config,
        "config_digest": config_digest,
        "input_fingerprint": input_fingerprint,
        "stage_order": list(stages),
        "stages": {name: {"state": "pending", "artifacts": {}} for name in stages},
        "artifacts": {},
    }


def load_manifest(path: str | os.PathLike[str]) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        value = json.load(f)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported or missing v3 manifest schema_version")
    if not value.get("run_id") or not isinstance(value.get("stages"), dict):
        raise ValueError("invalid v3 manifest")
    expected_digest = value.get("manifest_sha256")
    if not isinstance(expected_digest, str) or expected_digest != manifest_digest(value):
        raise ValueError("manifest digest is missing or does not match content")
    return value


def stage_input_hash(manifest: dict, stage: str) -> str:
    """Hash one stage's immutable inputs plus all upstream published artifacts."""
    order = manifest.get("stage_order") or list(manifest.get("stages", {}))
    if stage not in order:
        raise ValueError(f"unknown manifest stage: {stage}")
    upstream: dict[str, dict[str, str]] = {}
    for name in order[:order.index(stage)]:
        records = manifest.get("stages", {}).get(name, {}).get("artifacts", {})
        upstream[name] = {
            artifact_name: str(record.get("sha256"))
            for artifact_name, record in records.items()
            if isinstance(record, dict) and record.get("sha256")
        }
    return fingerprint(
        input_fingerprint=manifest.get("input_fingerprint"),
        runtime_fingerprint=manifest.get("runtime_fingerprint"),
        schema_version=manifest.get("schema_version"),
        stage=stage,
        upstream_artifacts=upstream,
    )


def update_stage(manifest: dict, stage: str, state: str, *, artifacts: dict | None = None,
                 detail: str | None = None) -> None:
    if state not in STAGE_STATES:
        raise ValueError(f"invalid stage state: {state}")
    if stage not in manifest["stages"]:
        raise ValueError(f"unknown manifest stage: {stage}")
    record = manifest["stages"][stage]
    previous = record.get("state", "pending")
    record["state"] = state
    if previous != state:
        record.setdefault("history", []).append({"from": previous, "to": state})
    if artifacts:
        record.setdefault("artifacts", {}).update(artifacts)
        manifest.setdefault("artifacts", {}).update(artifacts)
    if detail:
        record["detail"] = detail


def verify_artifact(record: dict) -> bool:
    """Return whether a manifest artifact still exists and matches its hash."""
    path = record.get("path") if isinstance(record, dict) else None
    expected = record.get("sha256") if isinstance(record, dict) else None
    try:
        return bool(path and expected and os.path.isfile(path) and sha256_file(path) == expected)
    except OSError:
        return False


def invalidate_downstream(manifest: dict, stage: str, reason: str) -> list[str]:
    """Reset ``stage`` and every later stage, returning the reset names."""
    order = manifest.get("stage_order") or list(manifest.get("stages", {}))
    if stage not in order:
        raise ValueError(f"unknown manifest stage: {stage}")
    reset = []
    for name in order[order.index(stage):]:
        record = manifest["stages"][name]
        record["state"] = "pending"
        record["artifacts"] = {}
        record["detail"] = reason
        reset.append(name)
    return reset


def strict_ready(manifest: dict) -> tuple[bool, list[str]]:
    required = ("reference_mining", "source_matching", "translation", "semantic_qa", "deterministic_qa")
    required_artifacts = {
        "reference_mining": ("reference_memory",),
        "source_matching": ("occurrence_ledger",),
        "translation": ("translation",),
        "semantic_qa": ("semantic_qa",),
        "deterministic_qa": ("qa_report", "coverage_report"),
    }
    errors = []
    if manifest.get("runtime_mode") != "orchestrated":
        errors.append("strict output requires orchestrated runtime")
    for stage in required:
        stage_record = manifest.get("stages", {}).get(stage, {})
        if stage_record.get("state") != "completed":
            errors.append(f"required stage not completed: {stage}")
        for required_name in required_artifacts[stage]:
            if required_name not in stage_record.get("artifacts", {}):
                errors.append(f"required artifact missing: {required_name} from {stage}")
        for name, record in stage_record.get("artifacts", {}).items():
            if not verify_artifact(record):
                errors.append(f"stale or missing artifact {name} from {stage}")
    return not errors, errors
