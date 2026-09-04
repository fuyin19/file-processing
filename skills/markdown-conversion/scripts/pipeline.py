#!/usr/bin/env python3
"""Unified PDF/Office-to-canonical/Markdown pipeline (v6)."""
from __future__ import annotations

import hashlib as _bootstrap_hashlib
import importlib.util as _bootstrap_importlib
import json as _bootstrap_json
import os as _bootstrap_os
import stat as _bootstrap_stat
import sys as _bootstrap_sys

_BOOTSTRAP_SKILL_ID = 'markdown-conversion'
_BOOTSTRAP_SCRIPTS = _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))
_BOOTSTRAP_SKILLS_ROOT = _bootstrap_os.path.dirname(_bootstrap_os.path.dirname(_BOOTSTRAP_SCRIPTS))
_BOOTSTRAP_LAYOUT = _bootstrap_os.path.join(
    _BOOTSTRAP_SKILLS_ROOT, "file-processing", "scripts", "runtime_layout.py"
)
_BOOTSTRAP_RESTORE = (
    "restore the complete unified installation so sibling file-processing and "
    "conversion skills come from one skills root"
)

if __name__ == "__main__":
    for _stream in (_bootstrap_sys.stdout, _bootstrap_sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def _bootstrap_fail(path: str, reason: str) -> "NoReturn":
    if "--runtime-preflight-json" in _bootstrap_sys.argv[1:]:
        print(_bootstrap_json.dumps({
            "schema_version": 1,
            "status": "error",
            "scope": "installation",
            "code": "conversion_runtime_unavailable",
        }, sort_keys=True, separators=(",", ":")))
        raise SystemExit(1)
    print(
        f"ERROR: {_BOOTSTRAP_SKILL_ID}: incomplete unified file-processing installation; "
        f"skills root: {_BOOTSTRAP_SKILLS_ROOT}; required path: {path}; "
        f"reason: {reason}; {_BOOTSTRAP_RESTORE}.",
        file=_bootstrap_sys.stderr,
    )
    raise SystemExit(1)


def _bootstrap_within(root: str, candidate: str) -> bool:
    try:
        common = _bootstrap_os.path.commonpath((root, candidate))
    except ValueError:
        return False
    return _bootstrap_os.path.normcase(common) == _bootstrap_os.path.normcase(root)


def _bootstrap_require_layout() -> str:
    root = _bootstrap_os.path.abspath(_BOOTSTRAP_SKILLS_ROOT)
    candidate = _bootstrap_os.path.abspath(_BOOTSTRAP_LAYOUT)
    if not _bootstrap_within(root, candidate):
        _bootstrap_fail(candidate, "path escapes the selected skills root")
    relative = _bootstrap_os.path.relpath(candidate, root)
    current = root
    components = [] if relative == _bootstrap_os.curdir else list(PathLikeParts(relative))
    for index, component in enumerate([None, *components]):
        if component is not None:
            current = _bootstrap_os.path.join(current, component)
        try:
            info = _bootstrap_os.lstat(current)
        except OSError as exc:
            _bootstrap_fail(current, f"unavailable ({exc})")
        if _bootstrap_stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & 0x400
        ):
            _bootstrap_fail(current, "link or Windows reparse point is forbidden")
        final = index == len(components)
        if final:
            if not _bootstrap_stat.S_ISREG(info.st_mode):
                _bootstrap_fail(current, "required dependency is not an ordinary file")
        elif not _bootstrap_stat.S_ISDIR(info.st_mode):
            _bootstrap_fail(current, "path component is not an ordinary directory")
    resolved_root = _bootstrap_os.path.realpath(root)
    resolved_candidate = _bootstrap_os.path.realpath(candidate)
    if not _bootstrap_within(resolved_root, resolved_candidate):
        _bootstrap_fail(candidate, "resolved path escapes the selected skills root")
    return candidate


def PathLikeParts(relative: str) -> tuple[str, ...]:
    drive, tail = _bootstrap_os.path.splitdrive(relative)
    del drive
    parts = []
    while tail not in ("", _bootstrap_os.curdir):
        head, leaf = _bootstrap_os.path.split(tail)
        if leaf:
            parts.append(leaf)
        if head == tail:
            break
        tail = head
    return tuple(reversed(parts))


_runtime_layout_path = _bootstrap_require_layout()
_runtime_layout_name = (
    "_file_processing_runtime_layout_"
    + _bootstrap_hashlib.sha256(_bootstrap_os.fsencode(_runtime_layout_path)).hexdigest()[:16]
)
_runtime_layout = _bootstrap_sys.modules.get(_runtime_layout_name)
if _runtime_layout is None:
    _runtime_layout_spec = _bootstrap_importlib.spec_from_file_location(
        _runtime_layout_name, _runtime_layout_path
    )
    if _runtime_layout_spec is None or _runtime_layout_spec.loader is None:
        _bootstrap_fail(_runtime_layout_path, "could not create a module loader")
    _runtime_layout = _bootstrap_importlib.module_from_spec(_runtime_layout_spec)
    _bootstrap_sys.modules[_runtime_layout_name] = _runtime_layout
    try:
        _runtime_layout_spec.loader.exec_module(_runtime_layout)
    except BaseException:
        _bootstrap_sys.modules.pop(_runtime_layout_name, None)
        raise

_RUNTIME_LAYOUT = _runtime_layout.bootstrap(
    entrypoint=__file__,
    skill_id=_BOOTSTRAP_SKILL_ID,
    carrier_files=('native_paths.py', 'conversion_runtime.py'),
    sibling_files=(('markdown-conversion', 'scripts/provider_worker.py'), ('markdown-conversion', 'scripts/anti_entropy_core_adapter.py')),
    import_siblings=(),
)


import argparse
from collections import Counter
import copy
import datetime
import importlib.metadata
import json
import math
import mimetypes
import os
import re
import shutil  # compatibility seam; native_paths performs the actual copy
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

import native_paths as np
import anti_entropy_core_adapter as core
import conversion_runtime as _conversion_runtime
import knowledge_unit

_RUNTIME_LAYOUT.verify_module(
    np, label="native_paths", expected=_RUNTIME_LAYOUT.carrier_scripts / "native_paths.py"
)
_RUNTIME_LAYOUT.verify_module(
    _conversion_runtime,
    label="conversion_runtime",
    expected=_RUNTIME_LAYOUT.carrier_scripts / "conversion_runtime.py",
)
_RUNTIME_LAYOUT.verify_module(core, label="anti_entropy_core_adapter")
_RUNTIME_LAYOUT.verify_module(
    knowledge_unit,
    label="knowledge_unit",
    expected=_RUNTIME_LAYOUT.scripts / "knowledge_unit.py",
)

from conversion_runtime import ConversionError as SharedConversionError
from conversion_runtime import OutputCollision as SharedOutputCollision
from conversion_runtime import SourceSnapshot, acquire_source_snapshot
from conversion_runtime import publish_owned as shared_publish_owned

from adapters import (
    ANYDOC_FORMAT_BY_EXTENSION,
    ANYDOC_SUFFIXES,
    AnyDocAdapter,
    MarkItDownAdapter,
    anydoc_capability_check,
    anydoc_version,
    convert_basic,
    markdown_to_canonical,
    markitdown_version,
)
from canonical import (
    CanonicalValidationError,
    convert_chinese,
    frontmatter,
    quality_from_warnings,
    render_markdown,
    sha256_bytes,
    sha256_file,
    stable_id,
    title_from_markdown,
    validate_canonical,
)
from ocr_provider import NullOcrProvider, OcrSettings, RapidOcrProvider
from pdf_inspector_adapter import PdfInspectorAdapter
from safe_url import redact_url


VERSION = "7.1.0"
DEFAULT_CONFIG: dict[str, Any] = {
    "pdf_ocr": {
        "mode": "auto",
        "engine": "rapidocr",
        "language": "ch",
        "dpi": 300.0,
        "max_long_edge": 4096,
        "min_confidence": 0.5,
    },
    "pdf_images": {"mode": "auto", "timeout_seconds": 1000.0},
}
SUPPORTED_EXTENSIONS = set(ANYDOC_FORMAT_BY_EXTENSION) | {
    ".pdf",
    ".html", ".csv", ".json", ".jsonl", ".xml", ".epub", ".md",
    ".jpg", ".jpeg", ".png", ".gif", ".mp3", ".wav", ".mp4",
    ".zip", ".txt",
}
DEPS = [
    ("opencc", "opencc-python-reimplemented", "core"),
    ("markdown_it", "markdown-it-py", "core"),
    ("jsonschema", "jsonschema", "route-specific"),
    ("markitdown", "markitdown", "route-specific"),
    ("pypdf", "pypdf", "route-specific"),
    ("pdf_inspector", "pdf-inspector", "route-specific"),
    ("pypdfium2", "pypdfium2", "route-specific"),
    ("rapidocr", "rapidocr", "route-specific"),
    ("onnxruntime", "onnxruntime", "route-specific"),
    ("pdfminer", "pdfminer.six", "optional"),
]
_rfc3339_datetime_re = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
PROVIDER_TIMEOUT_SECONDS = 1000.0
PROVIDER_WORKER = Path(__file__).with_name("provider_worker.py")
PDF_IMAGE_RESULT_LIMIT = 80 * 1024 * 1024


class PipelineError(RuntimeError):
    pass


def _complete_envelope_stage(stage: Path) -> None:
    try:
        core.stage_complete(stage)
    except core.CoreAdapterError as exc:
        raise PipelineError(str(exc)) from exc


class OutputCollision(PipelineError):
    pass


def _run_provider_worker(request: dict[str, Any], timeout: float = PROVIDER_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run native parsing/OCR outside the publishing process with a hard deadline."""
    image_settings = request.get("pdf_images") if request.get("adapter") == "pdf_inspector" else None
    enhance = bool(request.get("asset_dir") and image_settings and image_settings["mode"] != "off")
    image_parent = Path(request["asset_dir"]).parent if enhance else None
    if image_parent is not None:
        np.mkdir(image_parent, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".conversion-worker-", dir=np.native(image_parent) if image_parent else None) as directory:
        root = Path(directory)
        request_path = root / "request.json"
        result_path = root / "result.json"
        fallback_path = root / "body-fallback.json"
        if enhance:
            request = {**request, "image_fallback_path": str(fallback_path)}
        np.write_text(request_path, json.dumps(request, ensure_ascii=False), encoding="utf-8")
        environment = dict(os.environ)
        environment.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
        body_started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(PROVIDER_WORKER), "--request", str(request_path), "--result", str(result_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(f"Native conversion worker exceeded {timeout:g} seconds") from exc
        body_seconds = time.monotonic() - body_started
        image_seconds = 0.0
        accepted_image_dir = None
        if not np.is_file(result_path):
            raise PipelineError(f"Native conversion worker exited {completed.returncode} without a result")
        if enhance and completed.returncode == 0:
            candidate_path = root / "image-result.json"
            image_dir = root / "images"
            settings = {**image_settings, "document_id": request["document_id"]}
            image_started = time.monotonic()
            accepted = _run_pdf_image_worker(result_path, candidate_path, image_dir, request["source"], settings)
            image_seconds = time.monotonic() - image_started
            if accepted:
                accepted_image_dir = image_dir
                result_path = candidate_path
            else:
                result_path = fallback_path
        load_started = time.monotonic()
        try:
            envelope = json.loads(np.read_text(result_path, encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PipelineError("Native conversion worker returned an invalid result") from exc
        if completed.returncode != 0 or envelope.get("ok") is not True:
            error_type = str(envelope.get("error_type") or "WorkerError")
            message = str(envelope.get("message") or "native provider failed")
            raise PipelineError(f"Native conversion worker failed ({error_type}): {message}")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise PipelineError("Native conversion worker result is not an object")
        if accepted_image_dir is not None and result.get("assets"):
            # Common selected-result loading is complete. Promote the worker's
            # prevalidated files without a second asset walk or hashing pass.
            np.rename_no_replace(accepted_image_dir, Path(request["asset_dir"]))
        if request.get("adapter") == "pdf_inspector":
            timings = {"body_seconds": round(body_seconds, 6), "image_seconds": round(image_seconds, 6),
                       "result_load_seconds": round(time.monotonic() - load_started, 6)}
            print("[PDF stages] " + json.dumps(timings, ensure_ascii=True), file=sys.stderr)
            metrics_log = result.get("_image_metrics_log")
            if isinstance(metrics_log, str):
                print("[PDF image stages] " + metrics_log[:4000], file=sys.stderr)
        return result


def _run_pdf_image_worker(
    body_path: Path,
    result_path: Path,
    image_dir: Path,
    source: str,
    settings: dict[str, Any],
) -> bool:
    """Select an image result using only bounded process and identity checks.

    The absolute deadline includes launch, native work, validation and writing.
    The shared supervisor uses a Windows Job so timeout also kills children.
    """
    from libreoffice_pdf import _run_job_process

    deadline = time.monotonic() + float(settings["timeout_seconds"])
    with np.open_file(result_path, "xb"):
        pass
    result_identity = np.EntryIdentity.capture(result_path)
    np.mkdir(image_dir)
    image_identity = np.EntryIdentity.capture(image_dir)
    environment = dict(os.environ)
    environment.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    command = [
        sys.executable, "-I", str(PROVIDER_WORKER),
        "--image-body", str(body_path), "--image-source", source,
        "--image-dir", str(image_dir), "--image-mode", settings["mode"],
        "--image-document-id", settings["document_id"],
        "--image-deadline", str(deadline), "--result", str(result_path),
    ]
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        completed = _run_job_process(
            command, cwd=body_path.parent, environment=environment,
            timeout=remaining, diagnostic_limit=4096,
        )
        return (
            completed.returncode == 0
            and time.monotonic() <= deadline
            and result_identity.matches(result_path)
            and image_identity.matches(image_dir)
            and 0 < np.lstat(result_path).st_size <= PDF_IMAGE_RESULT_LIMIT
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False


@dataclass(frozen=True)
class Target:
    mode: str
    path: Path
    stem: str


def die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def _reject_non_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-JSON numeric constant {value}")


def is_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def url_to_slug(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    raw = (parsed.netloc.split("@")[-1] + parsed.path).strip("/")
    raw = urllib.parse.unquote(raw)
    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", raw).strip("-")
    if not slug:
        slug = parsed.netloc.split("@")[-1].replace(":", "-")
    return (slug[:120].rstrip("-") or "untitled")


def _source_stem(source: str) -> str:
    if is_url(source):
        path = urllib.parse.unquote(urllib.parse.urlparse(source).path)
        return Path(path).stem or url_to_slug(source)
    return Path(source).stem or "untitled"


def resolve_timestamp(value: str) -> str:
    if not value:
        return datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            die("--timestamp must be an ISO date or a timezone-aware ISO datetime")
        return value
    if _rfc3339_datetime_re.fullmatch(value) is None:
        die("--timestamp must be an ISO date or RFC3339 timezone-aware datetime")
    try:
        parsed = datetime.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        die("--timestamp must be an ISO date or a timezone-aware ISO datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        die("--timestamp datetime must include a timezone offset")
    return value


def _read_explicit_config(path: Path) -> dict[str, Any]:
    try:
        selected = np.logical(path)
        info = np.lstat(selected)
    except (OSError, TypeError, ValueError) as exc:
        die(f"Could not access config path: {exc}")
    if stat.S_ISLNK(info.st_mode) or np.is_reparse(info) or not stat.S_ISREG(info.st_mode):
        die(f"Config path is not an ordinary regular non-link/reparse file: {selected}")
    try:
        value = json.loads(
            np.read_text(selected, encoding="utf-8"),
            parse_constant=_reject_non_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        die(f"Could not read config as strict UTF-8 JSON: {exc}")
    if not isinstance(value, dict):
        die("config root must be an object")
    return value


def load_config(path: Path | None = None) -> dict[str, Any]:
    result = copy.deepcopy(DEFAULT_CONFIG)
    if path is None:
        return result
    value = _read_explicit_config(path)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(DEFAULT_CONFIG.get(key), dict):
            try:
                result[key] = {**result[key], **item}
            except (TypeError, ValueError) as exc:
                die(f"Could not merge config block {key}: {exc}")
        elif isinstance(DEFAULT_CONFIG.get(key), dict):
            die(f"config {key} must be an object")
        else:
            result[key] = item
    return result


def resolve_ocr_settings(args, config: dict[str, Any]) -> OcrSettings:
    raw = config.get("pdf_ocr", {})
    if not isinstance(raw, dict):
        die("config pdf_ocr must be an object")
    values = dict(raw)
    overrides = {
        "mode": getattr(args, "ocr", None),
        "engine": getattr(args, "ocr_engine", None),
        "language": getattr(args, "ocr_language", None),
        "dpi": getattr(args, "ocr_dpi", None),
        "max_long_edge": getattr(args, "ocr_max_long_edge", None),
        "min_confidence": getattr(args, "ocr_min_confidence", None),
    }
    values.update({key: value for key, value in overrides.items() if value is not None})
    try:
        settings = OcrSettings.from_mapping(values)
    except (TypeError, ValueError) as exc:
        die(f"Invalid PDF OCR configuration: {exc}")
    if settings.mode != "off" and settings.engine != "rapidocr":
        die(f"Unsupported OCR engine: {settings.engine}")
    return settings


def resolve_pdf_image_settings(args, config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("pdf_images", {})
    if not isinstance(raw, dict):
        die("config pdf_images must be an object")
    values = {**DEFAULT_CONFIG["pdf_images"], **raw}
    for key, value in (("mode", getattr(args, "pdf_images", None)),
                       ("timeout_seconds", getattr(args, "pdf_image_timeout", None))):
        if value is not None:
            values[key] = value
    if values["mode"] not in ("auto", "objects", "off"):
        die("PDF image mode must be auto, objects, or off")
    if isinstance(values["timeout_seconds"], bool):
        die("PDF image timeout must be a positive finite number")
    try:
        timeout = float(values["timeout_seconds"])
    except (TypeError, ValueError):
        die("PDF image timeout must be a positive finite number")
    if not math.isfinite(timeout) or timeout <= 0:
        die("PDF image timeout must be a positive finite number")
    return {"mode": values["mode"], "timeout_seconds": timeout}


def create_ocr_provider(settings: OcrSettings):
    if settings.mode == "off":
        return NullOcrProvider(settings)
    if settings.engine == "rapidocr":
        return RapidOcrProvider(settings)
    raise PipelineError(f"Unsupported OCR engine: {settings.engine}")


def _normalize_mode(args) -> None:
    if args.output_path and args.output_dir:
        die("--output-path and --output-dir are mutually exclusive")
    if args.input_dir and args.output_path:
        args.output_dir = args.output_path
        args.output_path = ""
        print("Warning: batch --output-path is deprecated; use --output-dir", file=sys.stderr)
    if args.output_mode is None:
        args.output_mode = "markdown" if args.input and args.output_path else "bundle"
    if args.output_path and args.output_mode != "markdown":
        die("--output-path is valid only for single-file --output-mode markdown")


def precheck(args) -> None:
    if bool(args.input) == bool(args.input_dir):
        die("Exactly one of --input or --input-dir is required")
    if args.overwrite and args.rename:
        die("--overwrite and --rename are mutually exclusive")
    _normalize_mode(args)
    if getattr(args, "enrich_images", False) and args.output_mode != "bundle":
        die("--enrich-images requires bundle output so extracted image assets remain available")
    if args.input and not is_url(args.input) and not np.is_file(args.input):
        die(f"File not found: {args.input}")
    if args.input_dir and not np.is_dir(args.input_dir):
        die(f"Directory not found: {args.input_dir}")
    if args.input_dir:
        try:
            _validate_batch_output_root(Path(args.input_dir), _batch_root(args))
        except PipelineError as exc:
            die(str(exc))


def _batch_root(args) -> Path:
    return np.logical(args.output_dir) if args.output_dir else (np.logical(args.input_dir) / "_converted")


def _validate_batch_output_root(input_root: Path, output_root: Path) -> None:
    resolved_input = np.logical(input_root)
    resolved_output = np.logical(output_root)
    if np.paths_equal(resolved_output, resolved_input):
        raise PipelineError("Batch output root must not equal the input root")
    if np.is_within(resolved_input, resolved_output):
        raise PipelineError("Batch output root must not be an ancestor of the input root")


def resolve_target(args, source: str, relative_path: Path | None = None) -> Target:
    stem = _source_stem(source)
    if args.input and args.output_path:
        return Target("markdown", np.logical(args.output_path), Path(args.output_path).stem)
    if args.input_dir:
        relative_path = relative_path or Path(source).name
        parent = relative_path.parent
        root = _batch_root(args)
        bundle_parent = root / parent
    else:
        root = np.logical(args.output_dir) if args.output_dir else (
            np.logical(Path.cwd()) if is_url(source) else np.logical(source).parent
        )
        bundle_parent = root
    if args.output_mode == "bundle":
        mode = getattr(args, "bundle_name_mode", "stem")
        if mode not in {"stem", "source-basename"}:
            raise PipelineError(f"Unsupported bundle name mode: {mode}")
        if mode == "source-basename":
            stem = Path(source).name or "untitled"
        path = knowledge_unit.bundle_target(
            bundle_parent,
            stem,
            boundary=root if args.input_dir else bundle_parent,
        )
    else:
        path = np.logical(bundle_parent / f"{stem}.md")
    return Target(args.output_mode, path, stem)


def _renamed_target(target: Target) -> Target:
    if not np.exists(target.path):
        return target
    for index in range(1, 10000):
        stem = f"{target.stem}_{index}"
        suffix = "" if target.mode == "bundle" else target.path.suffix
        path = target.path.with_name(f"{stem}{suffix}")
        if not np.exists(path):
            return Target(target.mode, path, stem)
    raise PipelineError(f"Could not find an available renamed target for {target.path}")


def _preflight_target(target: Target, source: str, overwrite: bool, rename: bool) -> Target:
    if not is_url(source) and np.paths_equal(target.path, source):
        raise PipelineError("Refusing to overwrite the source file")
    if not is_url(source) and target.mode == "bundle" and np.is_within(source, target.path):
        raise PipelineError("Refusing bundle output that contains the local source")
    if np.exists(target.path):
        if rename:
            return _renamed_target(target)
        if not overwrite:
            raise OutputCollision(f"Output already exists: {target.path}")
    return target


def validate_markdown_bundle_stem(stem: str) -> None:
    """Reject a generated canonical JSON name reserved by Cortex metadata."""
    if f"{stem}.json".casefold() == "record.json":
        raise PipelineError("Generated bundle JSON collides with reserved Cortex record.json")


def collect_files(
    input_dir: str,
    recursive: bool,
    types: list[str] | None,
    exclude_root: Path | None = None,
) -> list[str]:
    root = np.logical(input_dir)
    if not np.is_dir(root):
        die(f"Directory not found: {input_dir}")
    normalized = None
    if types is not None:
        normalized = {value.lower() if value.startswith(".") else f".{value.lower()}" for value in types}
    files: list[str] = []
    for path in np.walk_files(root, recursive):
        if exclude_root is not None:
            if np.is_within(path, exclude_root):
                continue
        suffix = path.suffix.lower()
        if normalized is not None and suffix not in normalized:
            continue
        if suffix in SUPPORTED_EXTENSIONS or (normalized and suffix in normalized):
            files.append(str(path))
    return sorted(files)


def _source_record(
    source: str,
    adapter_text: str | None = None,
    remote_bytes: bytes | None = None,
    remote_locator: str | None = None,
    remote_media_type: str | None = None,
    remote_sha256: str | None = None,
    remote_size_bytes: int | None = None,
) -> tuple[dict[str, Any], str]:
    if is_url(source):
        if remote_sha256 is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", remote_sha256):
                raise PipelineError("URL worker returned an invalid response digest")
            digest = remote_sha256
        elif remote_bytes is not None:
            digest = sha256_bytes(remote_bytes)
        elif adapter_text is not None:
            digest = sha256_bytes(adapter_text.encode("utf-8"))
        else:
            raise PipelineError("URL source identity requires response bytes or adapter text")
        byte_identity = remote_bytes is not None or remote_sha256 is not None
        return (
            {
                "kind": "url",
                "file_name": _source_stem(source),
                "locator": remote_locator or redact_url(source),
                "sha256": digest,
                "hash_basis": "remote_response_bytes" if byte_identity else "adapter_text",
                "size_bytes": len(remote_bytes) if remote_bytes is not None else remote_size_bytes,
                "media_type": remote_media_type,
            },
            f"sha256:{digest}",
        )
    path = np.logical(source)
    digest = sha256_file(path)
    return (
        {
            "kind": "file",
            "file_name": path.name,
            "locator": str(path),
            "sha256": digest,
            "hash_basis": "source_bytes",
            "size_bytes": np.stat(path).st_size,
            "media_type": mimetypes.guess_type(path.name)[0],
        },
        f"sha256:{digest}",
    )


def _archived_source_identity(
    logical_source: str,
    archived_source: Path,
) -> tuple[dict[str, Any], str]:
    source_path = np.logical(logical_source)
    digest = sha256_file(archived_source)
    record = {
        "kind": "file",
        "file_name": source_path.name,
        "locator": str(source_path),
        "sha256": digest,
        "hash_basis": "source_bytes",
        "size_bytes": np.stat(archived_source).st_size,
        "media_type": mimetypes.guess_type(source_path.name)[0],
    }
    return record, f"sha256:{digest}"


def _extract(
    source: str,
    mode: str,
    asset_dir: Path | None,
    identity_source: str | None = None,
    ocr_provider=None,
    ocr_mode: str = "off",
    local_adapter: str = "anydoc",
    precomputed_identity: tuple[dict[str, Any], str] | None = None,
    pdf_image_settings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    identity_source = identity_source or source
    if is_url(source):
        remote = _run_provider_worker({"adapter": "url_markitdown", "source": source}, timeout=45.0)
        markdown = str(remote.get("markdown") or "")
        source_record, document_id = _source_record(
            source,
            markdown,
            remote_locator=str(remote.get("locator") or redact_url(source)),
            remote_media_type=remote.get("media_type"),
            remote_sha256=str(remote.get("sha256") or ""),
            remote_size_bytes=remote.get("size_bytes"),
        )
        result = markdown_to_canonical(
            markdown,
            document_id,
            mode,
            "url",
            warn_unexported_images=asset_dir is not None,
        )
        result["adapter"] = {
            "name": "markitdown",
            "version": markitdown_version(),
            "limitations": ["embedded_images_may_not_be_exported"],
        }
        return result, source_record, document_id
    if precomputed_identity is None:
        source_record, document_id = _source_record(identity_source)
    else:
        source_record, document_id = precomputed_identity
    if Path(source).suffix.lower() == ".pdf":
        if type(ocr_provider) in {RapidOcrProvider, NullOcrProvider}:
            settings = getattr(ocr_provider, "settings", OcrSettings(mode=ocr_mode, engine="none"))
            result = _run_provider_worker({
                "adapter": "pdf_inspector",
                "source": source,
                "document_id": document_id,
                "mode": mode,
                "asset_dir": str(asset_dir) if asset_dir is not None else "",
                "ocr_mode": ocr_mode,
                "ocr_settings": asdict(settings),
                "pdf_images": pdf_image_settings or dict(DEFAULT_CONFIG["pdf_images"]),
            })
        else:
            result = PdfInspectorAdapter(ocr_provider, ocr_mode=ocr_mode).extract(
                source, document_id, mode, asset_dir
            )
    else:
        suffix = Path(source).suffix.lower()
        if local_adapter == "anydoc" and suffix in ANYDOC_SUFFIXES:
            result = _run_provider_worker({
                "adapter": "anydoc",
                "source": source,
                "document_id": document_id,
                "mode": mode,
                "asset_dir": str(asset_dir) if asset_dir is not None else "",
            })
        elif local_adapter == "markitdown" or suffix not in ANYDOC_SUFFIXES:
            result = _run_provider_worker({
                "adapter": "markitdown",
                "source": source,
                "document_id": document_id,
                "mode": mode,
                "asset_dir": str(asset_dir) if asset_dir is not None else "",
            })
        else:
            raise PipelineError(f"Unsupported local document adapter: {local_adapter}")
    return result, source_record, document_id


def _enrich_office_images(
    extracted: dict[str, Any],
    document_id: str,
    normalization: str,
    asset_dir: Path | None,
    settings: OcrSettings,
) -> None:
    """Add opt-in, provenance-linked OCR paragraphs after resolved Office images."""
    if asset_dir is None or extracted.get("adapter", {}).get("name") not in {"anydoc", "markitdown"}:
        return
    assets = extracted.get("assets", [])
    resolved_asset_ids = {
        item.get("asset_id") for item in extracted.get("content", []) if item.get("type") == "image"
    }
    candidates = [item for item in assets if item.get("asset_id") in resolved_asset_ids][:100]
    if not candidates:
        return
    ocr_settings = asdict(settings)
    ocr_settings.update({"mode": "auto", "engine": "rapidocr"})
    request_assets = [
        {"asset_id": item["asset_id"], "path": str(asset_dir / Path(item["path"]).name)}
        for item in candidates
    ]
    unit_id = extracted["source_units"][0]["id"]
    try:
        response = _run_provider_worker({
            "adapter": "image_ocr",
            "ocr_settings": ocr_settings,
            "assets": request_assets,
        })
    except PipelineError as exc:
        warning = {
            "code": "office_image_ocr_unavailable",
            "message": f"Office image OCR enrichment was unavailable ({type(exc).__name__})",
            "content_loss": False,
            "source_unit": unit_id,
        }
        extracted.setdefault("warnings", []).append(warning)
        extracted["source_units"][0].setdefault("warnings", []).append(warning)
        extracted["source_units"][0]["status"] = "warning"
        return
    by_asset = {
        item["asset_id"]: item for item in response.get("items", [])
        if isinstance(item, dict) and item.get("asset_id")
    }
    enriched_content: list[dict[str, Any]] = []
    new_relationships: list[dict[str, Any]] = []
    ocr_occurrence = 0
    for node in extracted.get("content", []):
        enriched_content.append(node)
        if node.get("type") != "image":
            continue
        item = by_asset.get(node.get("asset_id"))
        text = str((item or {}).get("text") or "").strip()
        if not text:
            continue
        ocr_occurrence += 1
        locator = {
            "source_unit_id": unit_id,
            "asset_id": node["asset_id"],
            "content_node_id": node["id"],
            "enrichment": "image_ocr",
            "occurrence": ocr_occurrence,
            "engine": item.get("engine"),
            "engine_version": item.get("engine_version"),
            "confidence": item.get("confidence"),
        }
        node_id = stable_id("node", document_id, locator, "paragraph", len(enriched_content) + 1)
        enriched_content.append({
            "id": node_id,
            "type": "paragraph",
            "source_locator": locator,
            "raw_text": text,
            "text": text,
            "normalized_text": convert_chinese(text, normalization),
        })
        new_relationships.append({
            "type": "image_ocr_text",
            "source_unit_id": unit_id,
            "asset_id": node["asset_id"],
            "image_content_node_id": node["id"],
            "content_node_id": node_id,
        })
    extracted["content"] = enriched_content
    extracted.setdefault("relationships", []).extend(new_relationships)
    if len(assets) > len(candidates):
        warning = {
            "code": "office_image_ocr_budget_reached",
            "message": f"Office image OCR enrichment was limited to {len(candidates)} resolved assets",
            "content_loss": False,
            "source_unit": unit_id,
        }
        extracted.setdefault("warnings", []).append(warning)
        extracted["source_units"][0].setdefault("warnings", []).append(warning)
        extracted["source_units"][0]["status"] = "warning"


def _build_document(
    source: str,
    timestamp: str,
    normalization: str,
    output_mode: str,
    asset_dir: Path | None,
    identity_source: str | None = None,
    ocr_provider=None,
    ocr_mode: str = "off",
    local_adapter: str = "anydoc",
    enrich_images: bool = False,
    ocr_settings: OcrSettings | None = None,
    precomputed_identity: tuple[dict[str, Any], str] | None = None,
    pdf_image_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity_source = identity_source or source
    extracted, source_record, document_id = _extract(
        source,
        normalization,
        asset_dir,
        identity_source,
        ocr_provider,
        ocr_mode,
        local_adapter,
        precomputed_identity,
        pdf_image_settings,
    )
    if enrich_images:
        _enrich_office_images(
            extracted,
            document_id,
            normalization,
            asset_dir,
            ocr_settings or OcrSettings(),
        )
    if is_url(identity_source):
        title = extracted.get("title") or _source_stem(identity_source)
        if title == "untitled":
            title = _source_stem(identity_source)
        title = convert_chinese(title, normalization)
    else:
        title = _source_stem(identity_source)
    warnings = extracted.get("warnings", [])
    return {
        "schema_version": "1.0",
        "source": source_record,
        "document": {
            "document_id": document_id,
            "title": title,
            "conversion_timestamp": timestamp,
            "language_normalization": normalization,
        },
        "adapter": extracted["adapter"],
        "source_units": extracted.get("source_units", []),
        "content": extracted.get("content", []),
        "tables": extracted.get("tables", []),
        "assets": extracted.get("assets", []),
        "relationships": extracted.get("relationships", []),
        "quality": {"status": quality_from_warnings(warnings), "warnings": warnings},
        "outputs": {"mode": output_mode, "markdown": {"path": "pending", "sha256": "0" * 64}, "assets": []},
    }


def _new_owned_dir(parent: Path, prefix: str) -> np.OwnedEntry:
    try:
        return np.create_owned_dir(parent, prefix)
    except RuntimeError as exc:
        raise PipelineError(str(exc)) from exc


def _new_owned_file(parent: Path, prefix: str, suffix: str = "") -> np.OwnedEntry:
    try:
        return np.create_owned_file(parent, prefix, suffix)
    except RuntimeError as exc:
        raise PipelineError(str(exc)) from exc


def _copy_local_source_to_bundle(source: str, stage: Path) -> tuple[Path, np.EntryIdentity, tuple[dict[str, Any], str]]:
    """Archive source bytes before conversion and bind canonical identity to them."""
    source_path = np.logical(source)
    destination = stage / "src" / source_path.name
    try:
        snapshot = acquire_source_snapshot(
            source_path,
            destination,
            # Preserve the established test/compatibility seam while the locked
            # original handle prevents source replacement or writes on Windows.
            compatibility_copier=np.copy_file,
        )
    except SharedConversionError as exc:
        raise PipelineError(str(exc)) from exc
    return destination, snapshot.snapshot_identity, snapshot.canonical_identity()


def _verify_archived_source(
    archived: Path,
    identity: np.EntryIdentity,
    expected_sha256: str,
) -> None:
    if not identity.matches(archived):
        raise PipelineError("Archived bundle source was replaced during conversion")
    actual = sha256_file(archived)
    if actual != expected_sha256:
        raise PipelineError(
            "Bundle source copy hash mismatch after conversion: "
            f"expected {expected_sha256}, got {actual}"
        )


def _publish_owned(
    stage: np.OwnedEntry,
    target: Path,
    overwrite: bool,
) -> None:
    """Publish through the shared minimal overwrite implementation."""
    try:
        shared_publish_owned(stage, target, overwrite)
    except SharedOutputCollision as exc:
        raise OutputCollision(str(exc)) from exc
    except SharedConversionError as exc:
        raise PipelineError(str(exc)) from exc


def _publish_directory(
    stage: Path,
    target: Path,
    overwrite: bool,
    stage_owner: np.OwnedEntry | None = None,
) -> None:
    identity = np.EntryIdentity.capture(stage)
    owner = stage_owner or np.OwnedEntry(
        identity.path, identity.st_dev, identity.st_ino, identity.st_mode
    )
    _publish_owned(owner, target, overwrite)


def _write_markdown_file(markdown: str, target: Path, overwrite: bool) -> None:
    target = np.logical(target)
    np.mkdir(target.parent, parents=True, exist_ok=True)
    stage = _new_owned_file(target.parent, ".mc-stage-", ".md")
    np.write_text(stage.path, markdown, encoding="utf-8", newline="\n")
    if not stage.matches(stage.path):
        raise PipelineError(f"Owned Markdown stage identity changed: {stage.path}")
    _publish_owned(stage, target, overwrite)


def _stage_markdown_bundle_artifacts(
    args,
    stage: Path,
    target_stem: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    """Render and validate one complete Markdown bundle inside an owned stage."""
    stage_started = time.monotonic()
    markdown_name = f"{target_stem}.md"
    json_name = f"{target_stem}.json"
    markdown = render_markdown(document, not args.no_frontmatter, "bundle")
    markdown_path = stage / markdown_name
    np.write_text(markdown_path, markdown, encoding="utf-8", newline="\n")
    document["outputs"] = {
        "mode": "bundle",
        "markdown": {"path": markdown_name, "sha256": sha256_file(markdown_path)},
        "assets": [{"path": item["path"], "sha256": item["sha256"]} for item in document["assets"]],
    }
    validate_canonical(document, stage)
    json_path = stage / json_name
    np.write_text(
        json_path,
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    persisted = json.loads(np.read_text(json_path, encoding="utf-8"))
    validate_canonical(persisted, stage, validate_schema=False)
    _complete_envelope_stage(stage)
    if document.get("adapter", {}).get("name") == "pdf-inspector":
        print(f"[PDF publication preparation] {time.monotonic() - stage_started:.6f} seconds", file=sys.stderr)
    return document


def _emit_markdown_bundle_core(
    args,
    *,
    operational_source: str,
    logical_source: str,
    stage: Path,
    target_stem: str,
    precomputed_identity: tuple[dict[str, Any], str],
    verify_snapshot,
) -> dict[str, Any]:
    """Authoritative staged bundle emitter shared by standalone and router."""
    local_adapter = getattr(args, "local_adapter", getattr(args, "document_adapter", "anydoc"))
    ocr_settings = getattr(args, "ocr_settings", OcrSettings(mode="off", engine="none"))
    ocr_provider = getattr(args, "ocr_provider", None)
    asset_dir = stage / "assets" / "images"
    document = _build_document(
        operational_source,
        args.timestamp,
        args.language_normalization,
        "bundle",
        Path(np.native(asset_dir)),
        identity_source=logical_source,
        ocr_provider=ocr_provider,
        ocr_mode=ocr_settings.mode,
        local_adapter=local_adapter,
        enrich_images=getattr(args, "enrich_images", False),
        ocr_settings=ocr_settings,
        precomputed_identity=precomputed_identity,
        pdf_image_settings=getattr(args, "pdf_image_settings", None),
    )
    verify_snapshot()
    return _stage_markdown_bundle_artifacts(args, stage, target_stem, document)


def emit_markdown_bundle(
    args,
    snapshot: SourceSnapshot,
    stage: Path,
    target_stem: str,
) -> dict[str, Any]:
    """Emit a local Markdown bundle from a pre-acquired source snapshot.

    This is the router integration seam.  It writes no publication state and
    does not reacquire or reinterpret the logical source identity.
    """
    return _emit_markdown_bundle_core(
        args,
        operational_source=np.native(snapshot.physical_path),
        logical_source=str(snapshot.logical_path),
        stage=stage,
        target_stem=target_stem,
        precomputed_identity=snapshot.canonical_identity(),
        verify_snapshot=snapshot.verify,
    )


def convert_one(args, source: str, relative_path: Path | None = None) -> tuple[Path, str, list[dict[str, Any]]]:
    local_adapter = getattr(args, "local_adapter", getattr(args, "document_adapter", "anydoc"))
    if local_adapter not in {"anydoc", "markitdown"}:
        raise PipelineError(f"Unsupported local document adapter: {local_adapter}")
    if not is_url(source) and Path(source).suffix.lower() == ".doc" and local_adapter == "markitdown":
        raise PipelineError(
            "MarkItDown cannot safely convert legacy .doc files; use the default AnyDoc adapter "
            "or first convert the file to .docx in a trusted desktop environment"
        )
    target = _preflight_target(resolve_target(args, source, relative_path), source, args.overwrite, args.rename)
    if target.mode == "bundle":
        validate_markdown_bundle_stem(target.stem)
    ocr_settings = getattr(args, "ocr_settings", OcrSettings(mode="off", engine="none"))
    ocr_provider = getattr(args, "ocr_provider", None)
    if target.mode == "bundle":
        np.mkdir(target.path.parent, parents=True, exist_ok=True)
        stage_owner = _new_owned_dir(target.path.parent, ".mc-stage-")
        stage = stage_owner.path
        if is_url(source):
            asset_dir = stage / "assets" / "images"
            document = _build_document(
                source, args.timestamp, args.language_normalization, "bundle",
                Path(np.native(asset_dir)),
                identity_source=source,
                ocr_provider=ocr_provider,
                ocr_mode=ocr_settings.mode,
                local_adapter=local_adapter,
                enrich_images=getattr(args, "enrich_images", False),
                ocr_settings=ocr_settings,
            )
            document = _stage_markdown_bundle_artifacts(args, stage, target.stem, document)
        else:
            archived_source, archived_identity, precomputed_identity = _copy_local_source_to_bundle(
                source, stage
            )
            document = _emit_markdown_bundle_core(
                args,
                operational_source=np.native(archived_source),
                logical_source=source,
                stage=stage,
                target_stem=target.stem,
                precomputed_identity=precomputed_identity,
                verify_snapshot=lambda: _verify_archived_source(
                    archived_source, archived_identity, precomputed_identity[0]["sha256"]
                ),
            )
        if not stage_owner.matches(stage):
            raise PipelineError(f"Owned bundle stage identity changed: {stage}")
        _publish_directory(stage, target.path, args.overwrite, stage_owner)
    else:
        precomputed_identity = None
        operational_source = source
        if not is_url(source):
            precomputed_identity = _source_record(source)
            operational_source = np.native(source)
        document = _build_document(
            operational_source, args.timestamp, args.language_normalization, "markdown", None,
            identity_source=source,
            ocr_provider=ocr_provider,
            ocr_mode=ocr_settings.mode,
            local_adapter=local_adapter,
            enrich_images=getattr(args, "enrich_images", False),
            ocr_settings=ocr_settings,
            precomputed_identity=precomputed_identity,
        )
        markdown = render_markdown(document, not args.no_frontmatter, "markdown")
        document["outputs"] = {
            "mode": "markdown",
            "markdown": {"path": target.path.name, "sha256": sha256_bytes(markdown.encode("utf-8"))},
            "assets": [],
        }
        validate_canonical(document, validate_schema=False)
        _write_markdown_file(markdown, target.path, args.overwrite)
    return target.path, document["quality"]["status"], document["quality"]["warnings"]


def _validate_types(types: list[str] | None) -> None:
    if not types:
        return
    normalized = {value.lower() if value.startswith(".") else f".{value.lower()}" for value in types}
    if not normalized.intersection(SUPPORTED_EXTENSIONS):
        die(f"Unsupported file type in --types: {', '.join(sorted(normalized))}")


def run_batch(args) -> int:
    types = [value.strip() for value in args.types.split(",") if value.strip()] or None
    _validate_types(types)
    root = np.logical(args.input_dir)
    output_root = _batch_root(args)
    files = collect_files(args.input_dir, not args.no_recursive, types, output_root)
    converted = failed = skipped = 0
    for source in files:
        relative = np.logical(source).relative_to(root)
        try:
            path, status, warnings = convert_one(args, source, relative)
            label = "PARTIAL" if status == "partial" else "WARN" if status == "complete_with_warnings" else "OK"
            counts = Counter(str(item.get("code") or "unknown_warning") for item in warnings)
            warning_summary = ", ".join(f"{code}x{counts[code]}" for code in sorted(counts))
            suffix = f" ({warning_summary})" if warning_summary else ""
            print(f"[{label}] {relative.as_posix()} -> {path}{suffix}")
            converted += 1
        except OutputCollision as exc:
            print(f"[SKIP] {relative.as_posix()} — {exc}", file=sys.stderr)
            skipped += 1
        except (PipelineError, CanonicalValidationError, OSError, ValueError) as exc:
            print(f"[FAIL] {relative.as_posix()} — {exc}", file=sys.stderr)
            failed += 1
        except Exception as exc:  # adapter boundary
            print(f"[FAIL] {relative.as_posix()} — {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
    print(f"[BATCH] {converted} converted, {failed} failed, {skipped} skipped")
    return 1 if failed else 2 if skipped else 0


def show_version() -> None:
    print(f"markdown-conversion v{VERSION}")
    print("Dependencies:")
    for import_name, install_name, scope in DEPS:
        distribution_name = re.split(r"[<>=!~]", install_name, maxsplit=1)[0]
        try:
            __import__(import_name)
            try:
                version = importlib.metadata.version(distribution_name)
            except importlib.metadata.PackageNotFoundError:
                version = "installed"
        except ImportError:
            version = "NOT INSTALLED"
        print(f"  {install_name}: {version} [{scope}]")
    print(f"  firecrawl-anydoc: {anydoc_version()} [route-specific; behavioral capability check]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified PDF/Office canonical document conversion pipeline")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--runtime-preflight-json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--required-suffix", action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--input")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-mode", choices=["bundle", "markdown"], default=None)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument(
        "--bundle-name-mode",
        choices=["stem", "source-basename"],
        default="stem",
        help="Name local bundle directories and representations from the source stem or full basename",
    )
    parser.add_argument("--language-normalization", choices=["simplified", "preserve", "traditional"], default="simplified")
    parser.add_argument("--no-frontmatter", action="store_true")
    parser.add_argument(
        "--enrich-images",
        action="store_true",
        help="OCR resolved embedded Office images and insert provenance-linked text (bundle mode only)",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--rename", action="store_true")
    parser.add_argument("--timestamp", default="")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--types", default="")
    parser.add_argument(
        "--local-adapter", "--document-adapter", "--local-document-adapter", dest="local_adapter",
        choices=["anydoc", "markitdown"], default="anydoc",
        help="Adapter for AnyDoc-eligible local formats (default: anydoc); URLs/PDFs keep their existing routes",
    )
    parser.add_argument(
        "--ocr",
        choices=["off", "auto", "force"],
        default=None,
        help="PDF OCR policy; defaults to config pdf_ocr.mode (auto)",
    )
    parser.add_argument("--pdf-images", choices=["auto", "objects", "off"], default=None,
                        help="PDF bundle images: complete regions/page previews, legacy objects, or off (default: auto)")
    parser.add_argument("--pdf-image-timeout", type=float, default=None,
                        help="Hard time budget for optional PDF images, separate from body/OCR (default: 1000 seconds)")
    parser.add_argument("--ocr-engine", choices=["rapidocr"], default=None)
    parser.add_argument("--ocr-language", default=None)
    parser.add_argument("--ocr-dpi", type=float, default=None)
    parser.add_argument("--ocr-max-long-edge", type=int, default=None)
    parser.add_argument("--ocr-min-confidence", type=float, default=None)
    return parser


def _runtime_preflight(args: argparse.Namespace) -> int:
    def emit(status: str, scope: str, code: str) -> int:
        print(json.dumps({
            "schema_version": 1, "status": status, "scope": scope, "code": code,
        }, sort_keys=True, separators=(",", ":")))
        return 0 if status == "ok" else 75 if scope == "python_environment" else 1

    try:
        load_config(args.config)
    except Exception:
        return emit("error", "config", "conversion_config_invalid")
    suffixes = {str(value).casefold() for value in args.required_suffix}
    if not suffixes or any(not value.startswith(".") for value in suffixes):
        return emit("error", "protocol", "runtime_preflight_suffix_invalid")
    try:
        if ".pdf" in suffixes:
            __import__("pypdf")
        if suffixes & ANYDOC_SUFFIXES:
            anydoc_capability_check()
    except (ImportError, RuntimeError):
        return emit("error", "python_environment", "conversion_python_dependency_unavailable")
    return emit("ok", "ready", "runtime_ready")


def main() -> int:
    args = build_parser().parse_args()
    if args.runtime_preflight_json:
        return _runtime_preflight(args)
    if args.version:
        load_config(args.config)
        show_version()
        return 0
    precheck(args)
    if args.output_mode != "bundle":
        return _run(args)
    try:
        with core.operation(skill_entrypoint=Path(__file__).absolute(), skill_id="markdown-conversion"):
            return _run(args)
    except core.CoreAdapterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _run(args) -> int:
    config = load_config(args.config)
    args.ocr_settings = resolve_ocr_settings(args, config)
    args.pdf_image_settings = resolve_pdf_image_settings(args, config)
    args.ocr_provider = create_ocr_provider(args.ocr_settings)
    args.timestamp = resolve_timestamp(args.timestamp)
    if args.input_dir:
        return run_batch(args)
    try:
        path, status, warnings = convert_one(args, args.input)
    except OutputCollision as exc:
        print(f"ERROR: {exc}\nRe-run with --overwrite or --rename.", file=sys.stderr)
        return 2
    except (PipelineError, CanonicalValidationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    label = "PARTIAL" if status == "partial" else "WARN" if status == "complete_with_warnings" else "OK"
    print(f"[{label}] Converted {args.input} -> {path} ({len(warnings)} warnings)")
    if warnings:
        for warning in warnings:
            print(f"  - {warning['code']}: {warning['message']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
