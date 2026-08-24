#!/usr/bin/env python3
"""Unified PDF/Office-to-canonical/Markdown pipeline (v6)."""
from __future__ import annotations

import os as _bootstrap_os
import sys as _bootstrap_sys

_SCRIPTS_DIR = _bootstrap_os.path.dirname(_bootstrap_os.path.realpath(__file__))
if _SCRIPTS_DIR not in _bootstrap_sys.path:
    _bootstrap_sys.path.insert(0, _SCRIPTS_DIR)
_SHARED_SCRIPTS_DIR = _bootstrap_os.path.realpath(
    _bootstrap_os.path.join(_SCRIPTS_DIR, "..", "..", "_shared", "scripts")
)
if _SHARED_SCRIPTS_DIR not in _bootstrap_sys.path:
    _bootstrap_sys.path.insert(0, _SHARED_SCRIPTS_DIR)
if __name__ == "__main__":
    for _stream in (_bootstrap_sys.stdout, _bootstrap_sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
del _bootstrap_os, _bootstrap_sys

import argparse
from collections import Counter
import datetime
import importlib.metadata
import json
import mimetypes
import os
import re
import shutil  # compatibility seam; native_paths performs the actual copy
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

import native_paths as np
from conversion_runtime import ConversionError as SharedConversionError
from conversion_runtime import SourceSnapshot, acquire_source_snapshot

from adapters import (
    ANYDOC_FORMAT_BY_EXTENSION,
    ANYDOC_SUFFIXES,
    AnyDocAdapter,
    MarkItDownAdapter,
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


VERSION = "6.5.2"
DEFAULT_CONFIG: dict[str, Any] = {
    "pdf_ocr": {
        "mode": "auto",
        "engine": "rapidocr",
        "language": "ch",
        "dpi": 300.0,
        "max_long_edge": 4096,
        "min_confidence": 0.5,
    }
}
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
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
PROVIDER_TIMEOUT_SECONDS = 180.0
PROVIDER_WORKER = Path(__file__).with_name("provider_worker.py")


class PipelineError(RuntimeError):
    pass


class OutputCollision(PipelineError):
    pass


_OWNED_ENTRIES: dict[str, np.OwnedEntry] = {}


def _owned_key(path: Path) -> str:
    return os.path.normcase(str(np.logical(path)))


def _run_provider_worker(request: dict[str, Any], timeout: float = PROVIDER_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run native parsing/OCR outside the publishing process with a hard deadline."""
    with tempfile.TemporaryDirectory(prefix=".conversion-worker-") as directory:
        root = Path(directory)
        request_path = root / "request.json"
        result_path = root / "result.json"
        np.write_text(request_path, json.dumps(request, ensure_ascii=False), encoding="utf-8")
        environment = dict(os.environ)
        environment.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
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
        if not np.is_file(result_path):
            raise PipelineError(f"Native conversion worker exited {completed.returncode} without a result")
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
        return result


@dataclass(frozen=True)
class Target:
    mode: str
    path: Path
    stem: str


def die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


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


def load_config() -> dict[str, Any]:
    if not np.exists(CONFIG_PATH):
        np.write_text(CONFIG_PATH, json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        value = json.loads(np.read_text(CONFIG_PATH, encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: config.json parse error ({exc}), using defaults", file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    result = dict(DEFAULT_CONFIG)
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(DEFAULT_CONFIG.get(key), dict):
            result[key] = {**DEFAULT_CONFIG[key], **item}
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
        base = root / parent / stem
    else:
        root = np.logical(args.output_dir) if args.output_dir else (
            np.logical(Path.cwd()) if is_url(source) else np.logical(source).parent
        )
        base = root / stem
    path = base if args.output_mode == "bundle" else base.parent / f"{stem}.md"
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
        entry = np.create_owned_dir(parent, prefix)
    except RuntimeError as exc:
        raise PipelineError(str(exc)) from exc
    _OWNED_ENTRIES[_owned_key(entry.path)] = entry
    return entry


def _new_owned_file(parent: Path, prefix: str, suffix: str = "") -> np.OwnedEntry:
    try:
        entry = np.create_owned_file(parent, prefix, suffix)
    except RuntimeError as exc:
        raise PipelineError(str(exc)) from exc
    _OWNED_ENTRIES[_owned_key(entry.path)] = entry
    return entry


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


def _cleanup_owned(entry: np.OwnedEntry, *, warning: str | None = None) -> bool:
    try:
        np.remove_owned(entry)
        _OWNED_ENTRIES.pop(_owned_key(entry.path), None)
        return True
    except OSError as exc:
        if warning is not None:
            print(f"Warning: {warning} {entry.path}: {exc}", file=sys.stderr)
        return False


def _remove_path(path: Path) -> None:
    """Compatibility seam: remove only an entry created by this invocation."""
    entry = _OWNED_ENTRIES.get(_owned_key(path))
    if entry is None:
        raise OSError(f"Refusing to remove an entry not owned by this invocation: {path}")
    np.remove_owned(entry)
    _OWNED_ENTRIES.pop(_owned_key(path), None)


def _recovery_error(target: Path, backup: np.OwnedEntry, detail: str) -> PipelineError:
    return PipelineError(
        f"{detail}; retained exact recovery backup at {backup.path} for target {target}"
    )


def _finish_backup_after_commit(target: Path, backup: np.OwnedEntry) -> None:
    try:
        _remove_path(backup.path)
    except OSError as exc:
        print(
            f"Warning: published {target}, but could not remove backup {backup.path}: {exc}",
            file=sys.stderr,
        )
        # The commit is confirmed.  Cleanup remains deliberately non-fatal and
        # the exact path printed above is the only recovery surface.
        return


def _restore_old_target(
    target: Path,
    backup: np.OwnedEntry,
    payload: Path,
    old_identity: np.EntryIdentity,
) -> bool:
    if np.exists(target) or not old_identity.matches(payload):
        return False
    try:
        np.rename_no_replace(payload, target)
    except Exception:
        # A wrapper or kernel boundary may report failure after the move.  The
        # state, not the exception alone, decides whether restoration happened.
        pass
    return old_identity.matches(target) and not np.exists(payload)


def _publish_owned(
    stage: np.OwnedEntry,
    target: Path,
    overwrite: bool,
    *,
    allow_stage_cleanup: bool,
) -> None:
    """Publish one owned file/directory with exact, identity-aware rollback."""
    target = np.logical(target)
    if not stage.matches(stage.path):
        raise PipelineError(f"Owned stage identity changed before publication: {stage.path}")
    old_identity = np.EntryIdentity.capture(target) if np.exists(target) else None
    if old_identity is not None and not overwrite:
        raise OutputCollision(f"Output already exists: {target}")

    if old_identity is None:
        try:
            np.rename_no_replace(stage.path, target)
        except FileExistsError as exc:
            if allow_stage_cleanup and stage.matches(stage.path):
                _cleanup_owned(stage, warning="could not remove exact late-collision stage")
            raise OutputCollision(f"Output already exists: {target}") from exc
        except Exception:
            if stage.matches(target) and not np.exists(stage.path):
                _OWNED_ENTRIES.pop(_owned_key(stage.path), None)
                return
            raise
        if not stage.matches(target) or np.exists(stage.path):
            raise PipelineError(f"Could not verify published target identity: {target}")
        _OWNED_ENTRIES.pop(_owned_key(stage.path), None)
        return

    backup = _new_owned_dir(target.parent, ".mc-backup-")
    payload = backup.path / "original"
    try:
        try:
            np.rename_no_replace(target, payload)
        except Exception as move_error:
            if old_identity.matches(payload) and not np.exists(target):
                restored = _restore_old_target(target, backup, payload, old_identity)
                if restored:
                    _cleanup_owned(
                        backup,
                        warning=f"restored {target}, but could not remove empty backup",
                    )
                    raise move_error
                raise _recovery_error(target, backup, "Old target move failed after displacement") from move_error
            if old_identity.matches(target) and not np.exists(payload):
                _cleanup_owned(
                    backup,
                    warning=f"target {target} remained in place, but could not remove empty backup",
                )
                raise move_error
            raise _recovery_error(target, backup, "Old target move entered an indeterminate state") from move_error

        if not old_identity.matches(payload) or np.exists(target):
            raise _recovery_error(target, backup, "Moved old target identity could not be verified")

        try:
            np.rename_no_replace(stage.path, target)
        except Exception as commit_error:
            if stage.matches(target) and not np.exists(stage.path):
                _OWNED_ENTRIES.pop(_owned_key(stage.path), None)
                _finish_backup_after_commit(target, backup)
                return
            restored = _restore_old_target(target, backup, payload, old_identity)
            if restored:
                _cleanup_owned(
                    backup,
                    warning=f"restored {target}, but could not remove empty backup",
                )
                if allow_stage_cleanup and stage.matches(stage.path):
                    _cleanup_owned(
                        stage,
                        warning="could not remove exact failed publication stage",
                    )
                raise commit_error
            raise _recovery_error(target, backup, "New target commit failed and exact restoration was not verified") from commit_error

        if not stage.matches(target) or np.exists(stage.path):
            raise _recovery_error(target, backup, "New target commit identity could not be verified")
        _OWNED_ENTRIES.pop(_owned_key(stage.path), None)
        if not old_identity.matches(payload):
            raise _recovery_error(target, backup, "Committed target has an unverifiable recovery payload")
        _finish_backup_after_commit(target, backup)
    except Exception:
        # No broad rollback and no parent/prefix sweep.  The state-specific
        # branches above decide whether an exact owned stage is safe to remove.
        raise


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
    _publish_owned(owner, target, overwrite, allow_stage_cleanup=stage_owner is not None)


def _write_markdown_file(markdown: str, target: Path, overwrite: bool) -> None:
    target = np.logical(target)
    np.mkdir(target.parent, parents=True, exist_ok=True)
    stage = _new_owned_file(target.parent, ".mc-stage-", ".md")
    try:
        np.write_text(stage.path, markdown, encoding="utf-8", newline="\n")
        if not stage.matches(stage.path):
            raise PipelineError(f"Owned Markdown stage identity changed: {stage.path}")
        _publish_owned(stage, target, overwrite, allow_stage_cleanup=True)
    finally:
        if stage.matches(stage.path):
            _cleanup_owned(stage, warning="could not remove exact failed Markdown stage")


def _stage_markdown_bundle_artifacts(
    args,
    stage: Path,
    target_stem: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    """Render and validate one complete Markdown bundle inside an owned stage."""
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
    ocr_settings = getattr(args, "ocr_settings", OcrSettings(mode="off", engine="none"))
    ocr_provider = getattr(args, "ocr_provider", None)
    if target.mode == "bundle":
        np.mkdir(target.path.parent, parents=True, exist_ok=True)
        stage_owner = _new_owned_dir(target.path.parent, ".mc-stage-")
        stage = stage_owner.path
        try:
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
        except Exception:
            if stage_owner.matches(stage):
                _cleanup_owned(
                    stage_owner,
                    warning="could not remove exact failed bundle stage",
                )
            raise
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
    parser.add_argument("--config", default="")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-mode", choices=["bundle", "markdown"], default=None)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--output-path", default="")
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
    parser.add_argument("--ocr-engine", choices=["rapidocr"], default=None)
    parser.add_argument("--ocr-language", default=None)
    parser.add_argument("--ocr-dpi", type=float, default=None)
    parser.add_argument("--ocr-max-long-edge", type=int, default=None)
    parser.add_argument("--ocr-min-confidence", type=float, default=None)
    return parser


def main() -> int:
    global CONFIG_PATH
    args = build_parser().parse_args()
    if args.version:
        show_version()
        return 0
    if args.config:
        CONFIG_PATH = str(np.logical(args.config))
    config = load_config()
    args.ocr_settings = resolve_ocr_settings(args, config)
    args.ocr_provider = create_ocr_provider(args.ocr_settings)
    precheck(args)
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
