#!/usr/bin/env python3
"""Local canonical Markdown + native PDF bundle router."""
from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn


_SCRIPTS = Path(__file__).resolve().parent
_SKILLS = _SCRIPTS.parents[1]
_SHARED = _SKILLS / "_shared" / "scripts"
_MARKDOWN = _SKILLS / "markdown-conversion" / "scripts"
for _path in (str(_SHARED), str(_MARKDOWN)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

import native_paths as np  # noqa: E402
from conversion_runtime import (  # noqa: E402
    ConversionError,
    OutputCollision,
    acquire_source_snapshot,
    cleanup_owned,
    new_owned_dir,
    publish_owned,
)
import knowledge_unit  # noqa: E402
from libreoffice_pdf import (  # noqa: E402
    DEFAULT_PDF_CONVERSION,
    SUPPORTED_SUFFIXES,
    LibreOfficeError,
    LibreOfficePdfEngine,
    PdfConversionSettings,
    classify_suffix,
    merge_pdf_conversion_config,
    resolve_libreoffice,
    validate_pdf,
    verify_validated_pdf,
)


def _load_markdown_pipeline():
    name = "file_processing_markdown_pipeline"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    path = _MARKDOWN / "pipeline.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load markdown-conversion pipeline")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


markdown_pipeline = _load_markdown_pipeline()

VERSION = "2.0.0"
CONFIG_PATH = _SCRIPTS / "config.json"
DEFAULT_CONFIG: dict[str, object] = {
    "pdf_ocr": dict(markdown_pipeline.DEFAULT_CONFIG["pdf_ocr"]),
    "pdf_conversion": DEFAULT_PDF_CONVERSION,
}
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


class PipelineError(ConversionError):
    pass


@dataclass(frozen=True)
class Target:
    path: Path
    stem: str


def die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def _is_url(value: str) -> bool:
    return bool(_URL_RE.match(value.strip()))


def load_config(path: Path | None = None) -> dict[str, object]:
    selected = path or CONFIG_PATH
    if not np.exists(selected):
        np.write_text(selected, json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        raw: dict[str, object] = {}
    else:
        try:
            loaded = json.loads(np.read_text(selected, encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Warning: config.json parse error ({exc}), using defaults", file=sys.stderr)
            loaded = {}
        if not isinstance(loaded, dict):
            die("config root must be an object")
        raw = loaded
    result = dict(raw)
    ocr = raw.get("pdf_ocr", {})
    if not isinstance(ocr, dict):
        die("config pdf_ocr must be an object")
    result["pdf_ocr"] = {**DEFAULT_CONFIG["pdf_ocr"], **ocr}  # type: ignore[dict-item]
    try:
        return merge_pdf_conversion_config(result)
    except LibreOfficeError as exc:
        die(str(exc))


def _normalize_formats(value: str) -> tuple[str, str]:
    raw = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not raw:
        return ("markdown", "pdf")
    aliases = {"md": "markdown", "markdown": "markdown", "pdf": "pdf"}
    try:
        normalized = {aliases[item] for item in raw}
    except KeyError:
        normalized = set()
    if normalized != {"markdown", "pdf"}:
        raise PipelineError(
            "--formats must resolve to exactly markdown,pdf; use "
            "/file-processing:markdown-conversion for Markdown-only output or "
            "/file-processing:pdf-conversion for PDF-only output"
        )
    return ("markdown", "pdf")


def _normalize_types(value: str) -> set[str] | None:
    raw = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not raw:
        return None
    normalized = {item if item.startswith(".") else f".{item}" for item in raw}
    invalid = normalized - SUPPORTED_SUFFIXES
    if invalid:
        raise PipelineError(f"Unsupported file type in --types: {', '.join(sorted(invalid))}")
    return normalized


def _batch_root(args) -> Path:
    return np.logical(args.output_dir) if args.output_dir else np.logical(args.input_dir) / "_converted"


def _validate_batch_root(input_root: Path, output_root: Path) -> None:
    if np.paths_equal(input_root, output_root):
        raise PipelineError("Batch output root must not equal the input root")
    if np.is_within(input_root, output_root):
        raise PipelineError("Batch output root must not be an ancestor of the input root")


def precheck(args) -> None:
    if bool(args.input) == bool(args.input_dir):
        die("Exactly one of --input or --input-dir is required")
    if args.overwrite and args.rename:
        die("--overwrite and --rename are mutually exclusive")
    if args.output_path and args.output_dir:
        die("--output-path and --output-dir are mutually exclusive")
    if args.input and args.output_path:
        die("file-conversion is bundle-only; --output-path is available only as a deprecated batch alias")
    if args.input_dir and args.output_path:
        args.output_dir = args.output_path
        args.output_path = ""
        print("Warning: batch --output-path is deprecated; use --output-dir", file=sys.stderr)
    try:
        args.formats_normalized = _normalize_formats(args.formats)
        _normalize_types(args.types)
    except ConversionError as exc:
        die(str(exc))
    selected = args.input or args.input_dir
    if _is_url(selected):
        die("URLs are not supported; file-conversion accepts local files/directories only")
    if args.input:
        if not np.is_file(args.input):
            die(f"File not found: {args.input}")
        try:
            classify_suffix(args.input)
        except LibreOfficeError as exc:
            die(str(exc))
        if Path(args.input).suffix.lower() == ".doc" and args.local_adapter == "markitdown":
            die(
                "MarkItDown cannot safely convert legacy .doc files; use the default AnyDoc adapter "
                "or first convert the file to .docx in a trusted desktop environment"
            )
    else:
        if not np.is_dir(args.input_dir):
            die(f"Directory not found: {args.input_dir}")
        try:
            _validate_batch_root(np.logical(args.input_dir), _batch_root(args))
        except ConversionError as exc:
            die(str(exc))


def collect_files(args) -> list[str]:
    root = np.logical(args.input_dir)
    output_root = _batch_root(args)
    types = _normalize_types(args.types)
    files: list[str] = []
    for path in np.walk_files(root, not args.no_recursive):
        if np.is_within(path, output_root):
            continue
        if types is not None and path.suffix.lower() not in types:
            continue
        if path.suffix.lower() in SUPPORTED_SUFFIXES:
            files.append(str(path))
    return sorted(files)


def resolve_target(args, source: str, relative_path: Path | None = None) -> Target:
    source_path = np.logical(source)
    stem = source_path.stem or "untitled"
    if args.input_dir:
        relative_path = relative_path or Path(source_path.name)
        root = _batch_root(args)
        parent = root / relative_path.parent
        path = knowledge_unit.bundle_target(parent, stem, boundary=root)
    else:
        root = np.logical(args.output_dir) if args.output_dir else source_path.parent
        path = knowledge_unit.bundle_target(root, stem, boundary=root)
    return Target(path, stem)


def _renamed_target(target: Target) -> Target:
    if not np.exists(target.path):
        return target
    for index in range(1, 10000):
        stem = f"{target.stem}_{index}"
        path = target.path.with_name(stem)
        if not np.exists(path):
            return Target(path, stem)
    raise PipelineError(f"Could not find an available renamed target for {target.path}")


def _preflight_target(target: Target, source: str, overwrite: bool, rename: bool) -> Target:
    if np.is_within(source, target.path):
        raise PipelineError("Refusing bundle output that contains the local source")
    if np.exists(target.path):
        if rename:
            return _renamed_target(target)
        if not overwrite:
            raise OutputCollision(f"Output already exists: {target.path}")
    return target


def _engine(args, config: dict[str, object]) -> LibreOfficePdfEngine:
    settings = PdfConversionSettings.from_config(config)
    engine = LibreOfficePdfEngine(settings)
    return engine.with_cli_path(args.libreoffice_path) if args.libreoffice_path else engine


def convert_one(
    args,
    source: str,
    config: dict[str, object],
    relative_path: Path | None = None,
    engine: LibreOfficePdfEngine | None = None,
) -> tuple[Path, str, list[dict[str, object]]]:
    if Path(source).suffix.lower() == ".doc" and args.local_adapter == "markitdown":
        raise PipelineError(
            "MarkItDown cannot safely convert legacy .doc files; use the default AnyDoc adapter "
            "or first convert the file to .docx in a trusted desktop environment"
        )
    target = _preflight_target(resolve_target(args, source, relative_path), source, args.overwrite, args.rename)
    markdown_pipeline.validate_markdown_bundle_stem(target.stem)
    converter = engine or _engine(args, config)
    if Path(source).suffix.lower() != ".pdf" and isinstance(converter, LibreOfficePdfEngine):
        _ = converter.executable
    np.mkdir(target.path.parent, parents=True, exist_ok=True)
    stage = new_owned_dir(target.path.parent, ".fc-stage-")
    work = new_owned_dir(Path(tempfile.gettempdir()), ".fcw-")
    try:
        snapshot = acquire_source_snapshot(source, stage.path / "src" / np.logical(source).name)
        document = markdown_pipeline.emit_markdown_bundle(args, snapshot, stage.path, target.stem)
        pdf_name = f"{target.stem}.pdf"
        converter.convert(snapshot, stage.path / pdf_name, work.path / "libreoffice")
        expected = validate_pdf(stage.path / pdf_name, converter.settings.validation)
        snapshot.verify()
        try:
            knowledge_unit.finalize_owned_stage(stage.path)
        except knowledge_unit.KnowledgeUnitError as exc:
            raise PipelineError(str(exc)) from exc
        if not stage.matches(stage.path):
            raise PipelineError(f"Owned router stage identity changed: {stage.path}")
        publish_owned(
            stage,
            target.path,
            args.overwrite,
            verify_payload=lambda root: (
                knowledge_unit.validate(root),
                verify_validated_pdf(root / pdf_name, converter.settings.validation, expected),
            ),
        )
        return target.path, document["quality"]["status"], document["quality"]["warnings"]
    finally:
        if stage.matches(stage.path):
            cleanup_owned(stage, warning="could not remove failed file-conversion stage")
        if work.matches(work.path):
            cleanup_owned(work, warning="could not remove private file-conversion workspace")


def run_batch(args, config: dict[str, object]) -> int:
    root = np.logical(args.input_dir)
    files = collect_files(args)
    converted = failed = skipped = 0
    engine: LibreOfficePdfEngine | None = None
    for source in files:
        relative = np.logical(source).relative_to(root)
        try:
            if Path(source).suffix.lower() != ".pdf" and engine is None:
                engine = _engine(args, config)
            path, status, warnings = convert_one(args, source, config, relative, engine)
            label = "PARTIAL" if status == "partial" else "WARN" if status == "complete_with_warnings" else "OK"
            counts = Counter(str(item.get("code") or "unknown_warning") for item in warnings)
            summary = ", ".join(f"{code}x{counts[code]}" for code in sorted(counts))
            suffix = f" ({summary})" if summary else ""
            print(f"[{label}] {relative.as_posix()} -> {path}{suffix}")
            converted += 1
        except OutputCollision as exc:
            print(f"[SKIP] {relative.as_posix()} — {exc}", file=sys.stderr)
            skipped += 1
        except Exception as exc:
            print(f"[FAIL] {relative.as_posix()} — {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
    print(f"[BATCH] {converted} converted, {failed} failed, {skipped} skipped")
    return 1 if failed else 2 if skipped else 0


def show_version(config: dict[str, object], cli_path: str) -> None:
    print(f"file-conversion v{VERSION}")
    print(f"markdown-conversion v{markdown_pipeline.VERSION}")
    raw = config.get("pdf_conversion", {})
    config_path = str(raw.get("libreoffice_path") or "") if isinstance(raw, dict) else ""
    try:
        path, version = resolve_libreoffice(cli_path, config_path)
        print(f"LibreOffice: {version} ({path})")
    except LibreOfficeError as exc:
        print(f"LibreOffice: NOT AVAILABLE ({exc})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create one canonical Markdown + native PDF bundle per local input")
    parser.add_argument("--config", default="")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--formats", default="")
    parser.add_argument("--language-normalization", choices=["simplified", "preserve", "traditional"], default="simplified")
    parser.add_argument("--no-frontmatter", action="store_true")
    parser.add_argument("--enrich-images", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--rename", action="store_true")
    parser.add_argument("--timestamp", default="")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--types", default="")
    parser.add_argument(
        "--local-adapter", "--document-adapter", "--local-document-adapter",
        dest="local_adapter", choices=["anydoc", "markitdown"], default="anydoc",
    )
    parser.add_argument("--ocr", choices=["off", "auto", "force"], default=None)
    parser.add_argument("--ocr-engine", choices=["rapidocr"], default=None)
    parser.add_argument("--ocr-language", default=None)
    parser.add_argument("--ocr-dpi", type=float, default=None)
    parser.add_argument("--ocr-max-long-edge", type=int, default=None)
    parser.add_argument("--ocr-min-confidence", type=float, default=None)
    parser.add_argument("--libreoffice-path", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(np.logical(args.config) if args.config else None)
    if args.version:
        show_version(config, args.libreoffice_path)
        return 0
    precheck(args)
    # Resolve once for the whole invocation, before any per-item work.
    args.timestamp = markdown_pipeline.resolve_timestamp(args.timestamp)
    args.ocr_settings = markdown_pipeline.resolve_ocr_settings(args, config)
    args.ocr_provider = markdown_pipeline.create_ocr_provider(args.ocr_settings)
    args.output_mode = "bundle"
    if args.input_dir:
        return run_batch(args, config)
    try:
        path, status, warnings = convert_one(args, args.input, config)
    except OutputCollision as exc:
        print(f"ERROR: {exc}\nRe-run with --overwrite or --rename.", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    label = "PARTIAL" if status == "partial" else "WARN" if status == "complete_with_warnings" else "OK"
    print(f"[{label}] Converted {args.input} -> {path} ({len(warnings)} warnings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
