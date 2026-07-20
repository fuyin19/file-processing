#!/usr/bin/env python3
"""Prepare, plan, apply, and validate reviewed OKF Markdown frontmatter."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, NoReturn, Sequence


VERSION = "1.0.0"
RUN_SCHEMA = "FrontmatterRun/v1"
PROPOSAL_SCHEMA = "FrontmatterProposal/v1"
PLAN_SCHEMA = "FrontmatterPlan/v1"
RECEIPT_SCHEMA = "FrontmatterApplyReceipt/v1"
GENERIC_POLICY_DIGEST = "sha256:" + hashlib.sha256(b'{"state":"generic-okf"}').hexdigest()
BASE_REQUIRED_FIELDS = ("type", "title", "description", "timestamp")
RESERVED_DOCUMENTS = frozenset({"index.md", "log.md"})
TARGET_ACTIONS = frozenset({"fork", "seal", "activate", "rollback", "retire", "gc"})
RUN_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "created_at",
        "run_dir",
        "source_root",
        "context",
        "required_fields",
        "replace_fields",
        "accept_partial",
        "keep_workspace",
        "items",
        "run_id",
    }
)
RUN_CONTEXT_KEYS = frozenset(
    {
        "mode",
        "workspace",
        "cortex_executable",
        "method_version",
        "policy_state",
        "policy_digest",
        "policy_manifest_path",
        "required_fields",
        "policy_files",
        "target",
        "bundle_path",
        "readiness",
        "quick_validation_status",
    }
)
RUN_ITEM_KEYS = frozenset(
    {
        "input_path",
        "target_path",
        "input_sha256",
        "target_before_sha256",
        "had_frontmatter",
        "had_utf8_bom",
        "existing_fields",
        "explicit_fields",
        "suggested_fields",
        "missing_fields_before_proposal",
    }
)
PLAN_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "run_dir",
        "created_at",
        "workspace",
        "policy_digest",
        "readiness",
        "keep_workspace",
        "accept_partial",
        "operations",
        "skipped",
        "plan_id",
    }
)
PLAN_OPERATION_KEYS = frozenset(
    {
        "input_path",
        "input_sha256",
        "target_path",
        "target_before_sha256",
        "staged_output_path",
        "staged_output_sha256",
        "rendered_sha256",
        "changed_fields",
        "frontmatter_before",
        "frontmatter_after",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
H1_RE = re.compile(r"^#(?!#)\s+(.+?)\s*$", re.MULTILINE)
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class PipelineError(Exception):
    exit_code = 1


class IncompleteError(PipelineError):
    exit_code = 2


class ConflictError(PipelineError):
    exit_code = 3


class CortexError(PipelineError):
    exit_code = 4


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _artifact_id(prefix: str, value: Mapping[str, Any]) -> str:
    return f"{prefix}@sha256:{_sha_bytes(_canonical_bytes(value))}"


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def _is_normalized_absolute_path(value: Any) -> bool:
    if not isinstance(value, str) or not Path(value).is_absolute():
        return False
    return str(Path(value).resolve(strict=False)) == value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (
        isinstance(value, (list, tuple, dict)) and not value
    )


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def ensure_ruamel() -> tuple[Any, Any]:
    """Load the pinned round-trip YAML dependency, installing it only when absent."""

    try:
        installed = importlib.metadata.version("ruamel.yaml")
    except importlib.metadata.PackageNotFoundError:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "ruamel.yaml>=0.17,<0.18"],
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise PipelineError(f"Cannot install ruamel.yaml>=0.17,<0.18: {result.stderr.strip()}")
        importlib.invalidate_caches()
        installed = importlib.metadata.version("ruamel.yaml")
    if not ((0, 17, 0) <= _version_tuple(installed) < (0, 18, 0)):
        raise PipelineError(
            f"Unsupported ruamel.yaml {installed}; install an isolated ruamel.yaml>=0.17,<0.18 environment"
        )
    module = importlib.import_module("ruamel.yaml")
    comments = importlib.import_module("ruamel.yaml.comments")
    return module.YAML, comments.CommentedMap


def _yaml_parser() -> tuple[Any, Any]:
    yaml_type, commented_map = ensure_ruamel()
    yaml = yaml_type(typ="rt", pure=True)
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    yaml.width = 4096
    return yaml, commented_map


def split_document(text: str, *, path: Path | None = None) -> tuple[Any, str, bool, bool]:
    """Return round-trip metadata, exact body, whether frontmatter existed, and BOM state."""

    had_bom = text.startswith("\ufeff")
    if had_bom:
        text = text[1:]
    yaml, commented_map = _yaml_parser()
    if not text.startswith("---"):
        return commented_map(), text, False, had_bom
    match = FRONTMATTER_RE.match(text)
    label = str(path) if path is not None else "document"
    if match is None:
        raise PipelineError(f"Invalid frontmatter in {label}: missing closing delimiter")
    try:
        loaded = yaml.load(match.group("yaml")) or commented_map()
    except Exception as exc:
        raise PipelineError(f"Invalid frontmatter in {label}: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise PipelineError(f"Invalid frontmatter in {label}: YAML root must be a mapping")
    if not isinstance(loaded, commented_map):
        loaded = commented_map(loaded)
    return loaded, text[match.end() :], True, had_bom


def render_document(metadata: Any, body: str) -> str:
    yaml, _ = _yaml_parser()
    stream = io.StringIO()
    yaml.dump(metadata, stream)
    rendered = stream.getvalue()
    if rendered.endswith("...\n"):
        rendered = rendered[:-4]
    if not rendered.endswith("\n"):
        rendered += "\n"
    return f"---\n{rendered}---\n{body}"


def _read_markdown(path: Path) -> tuple[str, Any, str, bool, bool]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise PipelineError(f"Markdown must be strict UTF-8: {path}") from exc
    metadata, body, had_frontmatter, had_bom = split_document(text, path=path)
    return text, metadata, body, had_frontmatter, had_bom


def _has_symlink_component(path: str | Path) -> bool:
    """Return whether an existing component is a symlink before resolution."""

    candidate = Path(os.path.abspath(os.fspath(path)))
    return any(component.is_symlink() for component in (candidate, *candidate.parents))


def _safe_path(path: str | Path, *, must_exist: bool = False) -> Path:
    candidate = Path(path)
    if must_exist and not candidate.exists():
        raise PipelineError(f"Input does not exist: {candidate}")
    if _has_symlink_component(candidate):
        raise PipelineError(f"Symlink paths are not supported: {candidate}")
    return candidate.resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _first_h1(body: str) -> str | None:
    match = H1_RE.search(body)
    if match is None:
        return None
    value = match.group(1).strip()
    value = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_`]+", "", value).strip()
    return value or None


def _iso_mtime(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _suggestions(path: Path, metadata: Mapping[str, Any], body: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    title = _first_h1(body) or path.stem
    if title:
        result["title"] = title
    source = metadata.get("source")
    source_path: Path | None = None
    if isinstance(source, str) and URL_RE.match(source):
        result["resource"] = source
    elif isinstance(source, str) and source.strip():
        candidate = Path(source)
        if candidate.exists() and not candidate.is_symlink():
            source_path = candidate
    try:
        result["timestamp"] = _iso_mtime(source_path or path)
    except OSError:
        pass
    return result


def _attributes(payload: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        attributes = payload.get("attributes")
        if isinstance(attributes, list):
            for item in attributes:
                if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                    found[str(item["name"])] = item.get("value")
        for value in payload.values():
            found.update(_attributes(value))
    elif isinstance(payload, list):
        for value in payload:
            found.update(_attributes(value))
    return found


def _find_named_mapping(payload: Any, key: str) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
        for child in payload.values():
            found = _find_named_mapping(child, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = _find_named_mapping(child, key)
            if found is not None:
                return found
    return None


def _run_cortex(executable: str, workspace: Path, arguments: Sequence[str], *, allow_validation_failure: bool = False) -> dict[str, Any]:
    command = [executable, "--workspace", str(workspace), *arguments, "--json"]
    result = subprocess.run(command, text=True, capture_output=True)
    allowed = {0, 3} if allow_validation_failure else {0}
    if result.returncode not in allowed:
        raise CortexError(f"Cortex command failed ({result.returncode}): {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CortexError("Cortex did not return a JSON ResultEnvelope") from exc
    if not isinstance(payload, dict):
        raise CortexError("Cortex ResultEnvelope must be an object")
    return payload


def inspect_cortex_workspace(workspace: str | Path) -> dict[str, Any]:
    """Read and validate the Cortex 2.1 method, workspace, and policy contracts."""

    root = _safe_path(workspace, must_exist=True)
    executable = shutil.which("cortex")
    if executable is None:
        raise CortexError("Cortex CLI is not available on PATH")
    method = _run_cortex(executable, root, ["manage", "status", "--kind", "method"])
    attrs = _attributes(method)
    version = str(attrs.get("version", ""))
    actions = attrs.get("target_actions")
    if not version.startswith("2.1.") or attrs.get("schema_count") != 18 or not isinstance(actions, list) or not TARGET_ACTIONS.issubset(set(actions)):
        raise CortexError("Cortex method contract is not 2.1-compatible")
    validation = _run_cortex(
        executable,
        root,
        ["manage", "validate", "--quick"],
        allow_validation_failure=True,
    )
    config = _run_cortex(executable, root, ["manage", "config", "show"])
    effective = _find_named_mapping(validation, "effective_policy") or {}
    target = _find_named_mapping(config, "target") or _find_named_mapping(validation, "target") or {}
    policy_state = str(effective.get("state", "absent"))
    policy_digest = str(effective.get("digest") or "")
    if policy_state == "invalid":
        raise CortexError("Cortex active policy package is invalid")
    if policy_state not in {"absent", "valid"}:
        raise CortexError(f"Unsupported Cortex policy state: {policy_state}")
    config_attrs = _attributes(config)
    required = list(BASE_REQUIRED_FIELDS)
    policy_files: list[dict[str, Any]] = []
    bundle_path: Path | None = None
    target_path = target.get("path")
    if isinstance(target_path, str):
        bundle_path = _safe_path(root / PurePosixPath(target_path))
    if policy_state == "valid":
        if not policy_digest:
            raise CortexError("Cortex valid policy is missing its digest")
        supplied_required = config_attrs.get("required_frontmatter_fields")
        supplied_files = config_attrs.get("files")
        if not isinstance(supplied_required, list) or any(not isinstance(item, str) for item in supplied_required):
            raise CortexError("Cortex policy required_frontmatter_fields are unavailable")
        for field in supplied_required:
            if field not in required:
                required.append(field)
        if not isinstance(supplied_files, list):
            raise CortexError("Cortex policy file manifest is unavailable")
        if bundle_path is None:
            raise CortexError("Cortex target path is unavailable")
        for item in supplied_files:
            if not isinstance(item, Mapping) or not all(isinstance(item.get(key), str) for key in ("role", "path", "sha256")):
                raise CortexError("Cortex policy file entry is invalid")
            relative = str(item["path"])
            file_path = _safe_path(bundle_path.joinpath(*PurePosixPath(relative).parts), must_exist=True)
            if not _is_within(file_path, bundle_path):
                raise CortexError(f"Cortex policy file escapes the bundle: {relative}")
            actual = _sha_file(file_path)
            if actual != item["sha256"]:
                raise CortexError(f"Cortex policy file digest differs: {relative}")
            policy_files.append({**dict(item), "absolute_path": str(file_path)})
        readiness = "cortex_policy_ready"
    else:
        policy_digest = policy_digest or "sha256:" + hashlib.sha256(b'{"state":"absent"}').hexdigest()
        readiness = "cortex_authoring_ready"
    return {
        "mode": "cortex",
        "workspace": str(root),
        "cortex_executable": executable,
        "method_version": version,
        "policy_state": policy_state,
        "policy_digest": policy_digest,
        "policy_manifest_path": effective.get("manifest_path"),
        "required_fields": required,
        "policy_files": policy_files,
        "target": _plain(target),
        "bundle_path": str(bundle_path) if bundle_path is not None else None,
        "readiness": readiness,
        "quick_validation_status": validation.get("status"),
    }


def generic_context() -> dict[str, Any]:
    return {
        "mode": "generic",
        "workspace": None,
        "cortex_executable": None,
        "method_version": None,
        "policy_state": "generic",
        "policy_digest": GENERIC_POLICY_DIGEST,
        "policy_manifest_path": None,
        "required_fields": list(BASE_REQUIRED_FIELDS),
        "policy_files": [],
        "target": None,
        "bundle_path": None,
        "readiness": "okf_ready",
        "quick_validation_status": None,
    }


def _load_metadata(path: str | None) -> Any:
    if not path:
        return {}
    source = _safe_path(path, must_exist=True)
    try:
        return json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Metadata JSON is invalid: {source}") from exc


def _metadata_for_path(metadata: Any, path: Path) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise PipelineError("Metadata JSON root must be an object")
    items = metadata.get("items")
    if items is None:
        return {str(key): _plain(value) for key, value in metadata.items()}
    if not isinstance(items, list):
        raise PipelineError("Metadata JSON items must be an array")
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str) or not isinstance(item.get("fields"), Mapping):
            raise PipelineError("Metadata JSON item requires path and fields")
        supplied = Path(str(item["path"])).resolve(strict=False)
        if supplied == path or str(item["path"]) == path.name:
            return {str(key): _plain(value) for key, value in item["fields"].items()}
    return {}


def _collect_inputs(source: Path, recursive: bool) -> list[Path]:
    if source.is_file():
        if source.suffix.casefold() not in {".md", ".markdown"}:
            raise PipelineError(f"Input must be Markdown: {source}")
        return [source]
    if not source.is_dir():
        raise PipelineError(f"Input is not a file or directory: {source}")
    iterator = source.rglob("*") if recursive else source.glob("*")
    paths: list[Path] = []
    for path in iterator:
        if path.is_symlink():
            raise PipelineError(f"Symlink paths are not supported: {path}")
        if path.is_file() and path.suffix.casefold() in {".md", ".markdown"}:
            paths.append(path.resolve())
    if not paths:
        raise PipelineError(f"No Markdown files found: {source}")
    return sorted(paths, key=lambda item: item.as_posix().encode("utf-8"))


def _renamed_target(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    sequence = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}-{stamp}-{sequence}{path.suffix}")
        sequence += 1
    return candidate


def _target_for(source_root: Path, input_path: Path, target: str | None, *, overwrite: bool, rename: bool) -> Path:
    if target is None:
        candidate = input_path
    else:
        supplied = Path(target)
        if source_root.is_dir():
            candidate = supplied / input_path.relative_to(source_root)
        elif supplied.exists() and supplied.is_dir():
            candidate = supplied / input_path.name
        else:
            candidate = supplied
    if _has_symlink_component(candidate):
        raise PipelineError(f"Symlink targets are not supported: {candidate}")
    candidate = candidate.resolve(strict=False)
    if candidate.exists() and candidate != input_path and not overwrite:
        if rename:
            candidate = _renamed_target(candidate)
        else:
            raise ConflictError(f"Target already exists: {candidate}; use --overwrite or --rename")
    return candidate


def prepare_run(args: argparse.Namespace) -> tuple[dict[str, Any], Path, bool]:
    source_root = _safe_path(args.input, must_exist=True)
    if source_root.is_symlink():
        raise PipelineError(f"Symlink paths are not supported: {source_root}")
    paths = _collect_inputs(source_root, not args.no_recursive)
    for path in paths:
        if path.name.casefold() in RESERVED_DOCUMENTS:
            raise PipelineError(f"Reserved OKF document cannot be annotated as a concept: {path.name}")
        if path.is_symlink():
            raise PipelineError(f"Symlink paths are not supported: {path}")
    context = inspect_cortex_workspace(args.workspace) if args.workspace else generic_context()
    bundle_path = Path(context["bundle_path"]) if context.get("bundle_path") else None
    metadata_input = _load_metadata(args.metadata_json)
    replace_fields = sorted({item.strip() for item in args.replace_fields.split(",") if item.strip()})
    if "source_ids" in replace_fields:
        raise PipelineError("source_ids is Cortex-managed and cannot be replaced")
    run_dir = _safe_path(args.run_dir) if args.run_dir else Path(tempfile.mkdtemp(prefix="file-processing-okf-")).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    incomplete = False
    for input_path in paths:
        target_path = _target_for(source_root, input_path, args.target, overwrite=args.overwrite, rename=args.rename)
        if bundle_path is not None and _is_within(target_path, bundle_path):
            raise PipelineError(f"Direct writes to the active Cortex bundle are forbidden: {target_path}")
        text, existing, body, had_frontmatter, had_bom = _read_markdown(input_path)
        explicit = _metadata_for_path(metadata_input, input_path)
        if "source_ids" in explicit:
            raise PipelineError("source_ids is Cortex-managed and cannot be supplied")
        suggested = _suggestions(input_path, existing, body)
        combined = dict(_plain(existing))
        for fields in (explicit, suggested):
            for key, value in fields.items():
                if _is_empty(combined.get(key)):
                    combined[key] = value
        missing = [field for field in context["required_fields"] if _is_empty(combined.get(field))]
        incomplete = incomplete or bool(missing)
        items.append(
            {
                "input_path": str(input_path),
                "target_path": str(target_path),
                "input_sha256": _sha_bytes(text.encode("utf-8")),
                "target_before_sha256": _sha_file(target_path) if target_path.exists() else None,
                "had_frontmatter": had_frontmatter,
                "had_utf8_bom": had_bom,
                "existing_fields": _plain(existing),
                "explicit_fields": explicit,
                "suggested_fields": suggested,
                "missing_fields_before_proposal": missing,
            }
        )
    created_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    payload: dict[str, Any] = {
        "schema_version": RUN_SCHEMA,
        "status": "awaiting_proposal",
        "created_at": created_at,
        "run_dir": str(run_dir),
        "source_root": str(source_root),
        "context": context,
        "required_fields": context["required_fields"],
        "replace_fields": replace_fields,
        "accept_partial": bool(args.accept_partial),
        "keep_workspace": bool(args.keep_workspace or args.plan_only),
        "items": items,
    }
    payload["run_id"] = _artifact_id("frontmatter-run", payload)
    state_path = run_dir / "run.json"
    _write_json(state_path, payload)
    return payload, state_path, incomplete


def _load_run(path: str | Path) -> dict[str, Any]:
    source = _safe_path(path, must_exist=True)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Run state is invalid: {source}") from exc
    if not isinstance(value, dict) or set(value) != RUN_KEYS or value.get("schema_version") != RUN_SCHEMA:
        raise PipelineError("Run state schema is invalid")
    context = value.get("context")
    items = value.get("items")
    if (
        value.get("status") != "awaiting_proposal"
        or not isinstance(context, dict)
        or set(context) != RUN_CONTEXT_KEYS
        or not isinstance(items, list)
        or not isinstance(value.get("required_fields"), list)
        or not isinstance(value.get("replace_fields"), list)
        or not isinstance(value.get("accept_partial"), bool)
        or not isinstance(value.get("keep_workspace"), bool)
        or not _is_normalized_absolute_path(value.get("run_dir"))
        or not _is_normalized_absolute_path(value.get("source_root"))
    ):
        raise PipelineError("Run state fields are invalid")
    expected_id = _artifact_id("frontmatter-run", {key: item for key, item in value.items() if key != "run_id"})
    if value.get("run_id") != expected_id:
        raise ConflictError("Run state digest does not match its content")
    for item in items:
        if (
            not isinstance(item, dict)
            or set(item) != RUN_ITEM_KEYS
            or not _is_normalized_absolute_path(item.get("input_path"))
            or not _is_normalized_absolute_path(item.get("target_path"))
            or not _is_sha256(item.get("input_sha256"))
            or (item.get("target_before_sha256") is not None and not _is_sha256(item.get("target_before_sha256")))
            or not isinstance(item.get("had_frontmatter"), bool)
            or not isinstance(item.get("had_utf8_bom"), bool)
            or not all(isinstance(item.get(field), dict) for field in ("existing_fields", "explicit_fields", "suggested_fields"))
            or not isinstance(item.get("missing_fields_before_proposal"), list)
        ):
            raise PipelineError("Run state item is invalid")
    return value


def _load_proposal(path: str | Path, run: Mapping[str, Any]) -> dict[str, Any]:
    source = _safe_path(path, must_exist=True)
    try:
        proposal = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Proposal is invalid JSON: {source}") from exc
    if not isinstance(proposal, dict) or set(proposal) != {"schema_version", "run_id", "complete_coverage", "items"}:
        raise PipelineError("Proposal must contain exactly schema_version, run_id, complete_coverage, and items")
    if proposal["schema_version"] != PROPOSAL_SCHEMA or proposal["run_id"] != run["run_id"]:
        raise PipelineError("Proposal schema or run identity is invalid")
    if not isinstance(proposal["complete_coverage"], bool) or not isinstance(proposal["items"], list):
        raise PipelineError("Proposal coverage and items are invalid")
    seen: set[str] = set()
    allowed = {item["input_path"]: item for item in run["items"]}
    for item in proposal["items"]:
        if not isinstance(item, dict) or set(item) != {"path", "input_sha256", "fields", "evidence"}:
            raise PipelineError("Each proposal item requires exactly path, input_sha256, fields, and evidence")
        path_value = item["path"]
        if path_value not in allowed or path_value in seen or item["input_sha256"] != allowed[path_value]["input_sha256"]:
            raise PipelineError("Proposal item path, coverage, or input hash is invalid")
        if not isinstance(item["fields"], dict) or not isinstance(item["evidence"], dict):
            raise PipelineError("Proposal fields and evidence must be objects")
        if set(item["evidence"]) != set(item["fields"]):
            raise PipelineError("Proposal evidence must exactly cover proposed fields")
        for field in item["fields"]:
            evidence = item["evidence"].get(field)
            if not isinstance(evidence, str) or not evidence.strip():
                raise PipelineError(f"Proposal field {field} lacks evidence")
        if "source_ids" in item["fields"]:
            raise PipelineError("source_ids is Cortex-managed and cannot be proposed")
        seen.add(path_value)
    if not run["accept_partial"] and (not proposal["complete_coverage"] or seen != set(allowed)):
        raise IncompleteError("Proposal does not cover every selected file")
    return proposal


def _validate_known_fields(metadata: Mapping[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    for field in ("type", "title", "description"):
        value = metadata.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{path}: {field} must be a non-empty string")
    timestamp = metadata.get("timestamp")
    if timestamp is not None:
        try:
            if isinstance(timestamp, (dt.date, dt.datetime)):
                pass
            else:
                dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{path}: timestamp must be ISO 8601")
    tags = metadata.get("tags")
    if tags is not None and (not isinstance(tags, list) or any(not isinstance(item, str) or not item.strip() for item in tags)):
        errors.append(f"{path}: tags must be a list of non-empty strings")
    for field in ("resource", "source", "converted_at", "converted_by"):
        value = metadata.get(field)
        if value is not None and not isinstance(value, str):
            errors.append(f"{path}: {field} must be a string")
    return errors


def _merge_fields(metadata: Any, run_item: Mapping[str, Any], proposal_fields: Mapping[str, Any], replace_fields: set[str]) -> list[str]:
    changed: list[str] = []
    explicit = run_item["explicit_fields"]
    suggested = run_item["suggested_fields"]
    preferred_order = {
        field: index
        for index, field in enumerate(
            ("type", "title", "description", "timestamp", "tags", "resource", "source", "converted_at", "converted_by")
        )
    }
    fields = sorted(
        set(explicit) | set(suggested) | set(proposal_fields),
        key=lambda field: (preferred_order.get(field, len(preferred_order)), field.encode("utf-8")),
    )
    for field in fields:
        if field == "source_ids":
            continue
        existing_is_empty = _is_empty(metadata.get(field))
        if not existing_is_empty and field not in replace_fields:
            continue
        value = None
        found = False
        sources = (explicit, suggested, proposal_fields) if existing_is_empty else (explicit, proposal_fields)
        for source_fields in sources:
            if field in source_fields and not _is_empty(source_fields[field]):
                value = source_fields[field]
                found = True
                break
        if found and _plain(metadata.get(field)) != _plain(value):
            metadata[field] = value
            changed.append(field)
    return sorted(set(changed))


def create_plan(state_path: str | Path, proposal_path: str | Path) -> tuple[dict[str, Any], Path, int]:
    run = _load_run(state_path)
    proposal = _load_proposal(proposal_path, run)
    proposal_map = {item["path"]: item for item in proposal["items"]}
    replace_fields = set(run["replace_fields"])
    run_dir = _safe_path(run["run_dir"], must_exist=True)
    outputs_dir = run_dir / "outputs"
    operations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, run_item in enumerate(run["items"], 1):
        proposal_item = proposal_map.get(run_item["input_path"])
        if proposal_item is None:
            skipped.append({"path": run_item["input_path"], "reason": "proposal_missing"})
            continue
        input_path = _safe_path(run_item["input_path"], must_exist=True)
        current_sha = _sha_file(input_path)
        if current_sha != run_item["input_sha256"]:
            raise ConflictError(f"Input changed after prepare: {input_path}")
        _, metadata, body, _, _ = _read_markdown(input_path)
        before = _plain(metadata)
        changed = _merge_fields(metadata, run_item, proposal_item["fields"], replace_fields)
        errors = _validate_known_fields(metadata, input_path)
        missing = [field for field in run["required_fields"] if _is_empty(metadata.get(field))]
        if errors or missing:
            skipped.append({"path": str(input_path), "reason": "metadata_incomplete", "missing": missing, "errors": errors})
            continue
        rendered = render_document(metadata, body).encode("utf-8")
        output_sha = _sha_bytes(rendered)
        staged = outputs_dir / f"{index:04d}-{output_sha}.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(rendered)
        target = _safe_path(run_item["target_path"])
        operations.append(
            {
                "input_path": str(input_path),
                "input_sha256": run_item["input_sha256"],
                "target_path": str(target),
                "target_before_sha256": run_item["target_before_sha256"],
                "staged_output_path": str(staged),
                "staged_output_sha256": output_sha,
                "rendered_sha256": output_sha,
                "changed_fields": changed,
                "frontmatter_before": before,
                "frontmatter_after": _plain(metadata),
            }
        )
    if skipped and not run["accept_partial"]:
        raise IncompleteError(f"Metadata is incomplete for {len(skipped)} selected file(s)")
    if not operations:
        raise IncompleteError("No complete proposal item can be planned")
    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA,
        "run_id": run["run_id"],
        "run_dir": str(run_dir),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "workspace": run["context"]["workspace"],
        "policy_digest": run["context"]["policy_digest"],
        "readiness": run["context"]["readiness"],
        "keep_workspace": run["keep_workspace"],
        "accept_partial": run["accept_partial"],
        "operations": operations,
        "skipped": skipped,
    }
    payload["plan_id"] = _artifact_id("frontmatter-plan", payload)
    digest = payload["plan_id"].rsplit(":", 1)[1]
    plan_path = run_dir / "plans" / f"sha256-{digest}.json"
    _write_json(plan_path, payload)
    return payload, plan_path, len(skipped)


def _validate_plan_digest(plan: Mapping[str, Any]) -> None:
    supplied = plan.get("plan_id")
    payload = {key: value for key, value in plan.items() if key != "plan_id"}
    expected = _artifact_id("frontmatter-plan", payload)
    if supplied != expected:
        raise ConflictError("Plan digest does not match its content")


def _validate_plan_schema(plan: Any, source: Path) -> None:
    if not isinstance(plan, dict) or set(plan) != PLAN_KEYS or plan.get("schema_version") != PLAN_SCHEMA:
        raise PipelineError("Plan schema is invalid")
    operations = plan.get("operations")
    if (
        not isinstance(operations, list)
        or not operations
        or not isinstance(plan.get("skipped"), list)
        or not isinstance(plan.get("keep_workspace"), bool)
        or not isinstance(plan.get("accept_partial"), bool)
        or not _is_normalized_absolute_path(plan.get("run_dir"))
    ):
        raise PipelineError("Plan fields are invalid")
    for operation in operations:
        if (
            not isinstance(operation, dict)
            or set(operation) != PLAN_OPERATION_KEYS
            or not all(
                _is_normalized_absolute_path(operation.get(field))
                for field in ("input_path", "target_path", "staged_output_path")
            )
            or not all(
                _is_sha256(operation.get(field))
                for field in ("input_sha256", "staged_output_sha256", "rendered_sha256")
            )
            or (operation.get("target_before_sha256") is not None and not _is_sha256(operation.get("target_before_sha256")))
            or operation.get("staged_output_sha256") != operation.get("rendered_sha256")
            or not isinstance(operation.get("changed_fields"), list)
            or not isinstance(operation.get("frontmatter_before"), dict)
            or not isinstance(operation.get("frontmatter_after"), dict)
        ):
            raise PipelineError("Plan operation is invalid")
    _validate_plan_digest(plan)
    digest = str(plan["plan_id"]).rsplit(":", 1)[-1]
    expected_path = Path(str(plan["run_dir"])) / "plans" / f"sha256-{digest}.json"
    if source != expected_path.resolve(strict=False):
        raise ConflictError("Plan path does not match its content-addressed identity")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply_plan(plan_path: str | Path) -> dict[str, Any]:
    source = _safe_path(plan_path, must_exist=True)
    try:
        plan = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"Plan is invalid JSON: {source}") from exc
    _validate_plan_schema(plan, source)
    if plan.get("workspace"):
        current_context = inspect_cortex_workspace(str(plan["workspace"]))
        if current_context["policy_digest"] != plan["policy_digest"]:
            raise ConflictError("Cortex policy digest changed after planning")
        current_bundle = Path(current_context["bundle_path"]) if current_context.get("bundle_path") else None
        if current_bundle is not None:
            for operation in plan["operations"]:
                if _is_within(_safe_path(str(operation["target_path"])), current_bundle):
                    raise ConflictError("Plan target is now inside the active Cortex bundle")
    statuses: list[dict[str, Any]] = []
    pending: list[tuple[Mapping[str, Any], Path, bytes]] = []
    for operation in plan["operations"]:
        if not isinstance(operation, Mapping):
            raise PipelineError("Plan operation is invalid")
        input_path = _safe_path(str(operation["input_path"]), must_exist=True)
        target_path = _safe_path(str(operation["target_path"]))
        if target_path.is_symlink():
            raise ConflictError(f"Target became a symlink: {target_path}")
        if target_path.exists() and _sha_file(target_path) == operation["rendered_sha256"]:
            statuses.append({"path": str(target_path), "status": "already_applied"})
            continue
        if _sha_file(input_path) != operation["input_sha256"]:
            raise ConflictError(f"Input changed after planning: {input_path}")
        current_target = _sha_file(target_path) if target_path.exists() else None
        if current_target != operation["target_before_sha256"]:
            raise ConflictError(f"Target changed after planning: {target_path}")
        staged = _safe_path(str(operation["staged_output_path"]), must_exist=True)
        content = staged.read_bytes()
        if _sha_bytes(content) != operation["staged_output_sha256"]:
            raise ConflictError(f"Staged output changed after planning: {staged}")
        pending.append((operation, target_path, content))
    for operation, target_path, content in pending:
        _atomic_write(target_path, content)
        statuses.append({"path": str(target_path), "status": "applied", "sha256": operation["rendered_sha256"]})
    overall = "already_applied" if statuses and all(item["status"] == "already_applied" for item in statuses) else "applied"
    receipt_payload: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "plan_id": plan["plan_id"],
        "status": overall,
        "applied_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": statuses,
        "skipped": plan["skipped"],
    }
    receipt_payload["receipt_id"] = _artifact_id("frontmatter-receipt", receipt_payload)
    run_dir = _safe_path(str(plan["run_dir"]), must_exist=True)
    if plan["keep_workspace"]:
        _write_json(run_dir / "receipt.json", receipt_payload)
    else:
        shutil.rmtree(run_dir)
    return receipt_payload


def validate_inputs(input_value: str, workspace: str | None, no_recursive: bool) -> tuple[dict[str, Any], bool]:
    source = _safe_path(input_value, must_exist=True)
    paths = _collect_inputs(source, not no_recursive)
    context = inspect_cortex_workspace(workspace) if workspace else generic_context()
    results: list[dict[str, Any]] = []
    all_valid = True
    for path in paths:
        issues: list[str] = []
        if path.name.casefold() in RESERVED_DOCUMENTS:
            issues.append("reserved_document")
        try:
            _, metadata, _, had_frontmatter, had_bom = _read_markdown(path)
            if not had_frontmatter:
                issues.append("missing_frontmatter")
            if had_bom:
                issues.append("utf8_bom")
            issues.extend(_validate_known_fields(metadata, path))
            for field in context["required_fields"]:
                if _is_empty(metadata.get(field)):
                    issues.append(f"missing:{field}")
        except PipelineError as exc:
            issues.append(str(exc))
        valid = not issues
        all_valid = all_valid and valid
        results.append({"path": str(path), "valid": valid, "issues": issues})
    return {
        "schema_version": "FrontmatterValidation/v1",
        "valid": all_valid,
        "readiness": context["readiness"] if all_valid else "blocked",
        "policy_digest": context["policy_digest"],
        "items": results,
    }, all_valid


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare reviewed OKF/Cortex Markdown frontmatter")
    parser.add_argument("--version", action="store_true", help="Show version and dependency status")
    sub = parser.add_subparsers(dest="command")

    prepare = sub.add_parser("prepare", help="Create an immutable run manifest")
    prepare.add_argument("--input", required=True)
    prepare.add_argument("--target")
    prepare.add_argument("--workspace")
    prepare.add_argument("--metadata-json")
    prepare.add_argument("--replace-fields", default="")
    prepare.add_argument("--no-recursive", action="store_true")
    prepare.add_argument("--accept-partial", action="store_true")
    prepare.add_argument("--plan-only", action="store_true")
    prepare.add_argument("--keep-workspace", action="store_true")
    prepare.add_argument("--run-dir")
    prepare.add_argument("--overwrite", action="store_true")
    prepare.add_argument("--rename", action="store_true")

    plan = sub.add_parser("plan", help="Validate a proposal and seal a content-addressed plan")
    plan.add_argument("--state", required=True)
    plan.add_argument("--proposal", required=True)

    apply = sub.add_parser("apply", help="Apply one exact content-addressed plan")
    apply.add_argument("--plan", required=True)

    validate = sub.add_parser("validate", help="Validate Markdown without writing")
    validate.add_argument("--input", required=True)
    validate.add_argument("--workspace")
    validate.add_argument("--no-recursive", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.version:
            try:
                installed = importlib.metadata.version("ruamel.yaml")
                dependency = f"ruamel.yaml {installed}"
            except importlib.metadata.PackageNotFoundError:
                dependency = "ruamel.yaml missing"
            print(f"okf-frontmatter {VERSION}; {dependency}; cortex={'available' if shutil.which('cortex') else 'missing'}")
            return
        if args.command == "prepare":
            if args.overwrite and args.rename:
                raise PipelineError("--overwrite and --rename are mutually exclusive")
            run, state_path, incomplete = prepare_run(args)
            _print_json(
                {
                    "schema_version": RUN_SCHEMA,
                    "status": run["status"],
                    "readiness": "blocked" if incomplete else run["context"]["readiness"],
                    "run_id": run["run_id"],
                    "state_path": str(state_path),
                    "run_dir": run["run_dir"],
                    "item_count": len(run["items"]),
                }
            )
            if incomplete:
                raise SystemExit(2)
            return
        if args.command == "plan":
            plan, plan_path, skipped = create_plan(args.state, args.proposal)
            _print_json(
                {
                    "schema_version": PLAN_SCHEMA,
                    "plan_id": plan["plan_id"],
                    "plan_path": str(plan_path),
                    "readiness": plan["readiness"],
                    "operation_count": len(plan["operations"]),
                    "skipped_count": skipped,
                }
            )
            return
        if args.command == "apply":
            _print_json(apply_plan(args.plan))
            return
        if args.command == "validate":
            report, valid = validate_inputs(args.input, args.workspace, args.no_recursive)
            _print_json(report)
            if not valid:
                raise SystemExit(2)
            return
        parser.error("a command is required")
    except SystemExit:
        raise
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        recovery: Path | None = None
        if getattr(args, "command", None) == "prepare" and getattr(args, "run_dir", None):
            recovery = Path(args.run_dir).resolve(strict=False)
        elif getattr(args, "command", None) == "plan" and getattr(args, "state", None):
            try:
                recovery = Path(_load_run(args.state)["run_dir"])
            except PipelineError:
                recovery = None
        elif getattr(args, "command", None) == "apply" and getattr(args, "plan", None):
            try:
                recovery_plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
                recovery = Path(recovery_plan["run_dir"])
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                recovery = None
        if recovery is not None and recovery.exists():
            print(f"RECOVERY: run retained at {recovery}", file=sys.stderr)
        raise SystemExit(exc.exit_code)


if __name__ == "__main__":
    main()
