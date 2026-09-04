#!/usr/bin/env python3
"""Local canonical Markdown + native PDF bundle router."""
from __future__ import annotations

import hashlib as _bootstrap_hashlib
import importlib.util as _bootstrap_importlib
import os as _bootstrap_os
import stat as _bootstrap_stat
import sys as _bootstrap_sys

_BOOTSTRAP_SKILL_ID = 'file-conversion'
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
    carrier_files=('native_paths.py', 'conversion_runtime.py', 'libreoffice_pdf.py', 'pdf_validation_worker.py'),
    sibling_files=(('file-conversion', 'scripts/anti_entropy_core_adapter.py'), ('markdown-conversion', 'SKILL.md'), ('markdown-conversion', 'scripts/pipeline.py')),
    import_siblings=('markdown-conversion',),
)

import argparse
from collections import Counter
import copy
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn


_SKILLS = _RUNTIME_LAYOUT.skills_root
_MARKDOWN = _SKILLS / "markdown-conversion" / "scripts"

import native_paths as np  # noqa: E402
import anti_entropy_core_adapter as core  # noqa: E402
import conversion_runtime as _conversion_runtime  # noqa: E402
import libreoffice_pdf as _libreoffice_pdf  # noqa: E402
import knowledge_unit  # noqa: E402

_RUNTIME_LAYOUT.verify_module(
    np, label="native_paths", expected=_RUNTIME_LAYOUT.carrier_scripts / "native_paths.py"
)
_RUNTIME_LAYOUT.verify_module(
    _conversion_runtime,
    label="conversion_runtime",
    expected=_RUNTIME_LAYOUT.carrier_scripts / "conversion_runtime.py",
)
_RUNTIME_LAYOUT.verify_module(
    _libreoffice_pdf,
    label="libreoffice_pdf",
    expected=_RUNTIME_LAYOUT.carrier_scripts / "libreoffice_pdf.py",
)
_RUNTIME_LAYOUT.verify_module(core, label="anti_entropy_core_adapter")
_RUNTIME_LAYOUT.verify_module(
    knowledge_unit,
    label="knowledge_unit",
    expected=_MARKDOWN / "knowledge_unit.py",
)

from conversion_runtime import (  # noqa: E402
    ConversionError,
    OutputCollision,
    acquire_source_snapshot,
    cleanup_owned,
    new_owned_dir,
    publish_owned,
)
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
    path = _RUNTIME_LAYOUT.require_file(_MARKDOWN / "pipeline.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load markdown-conversion pipeline")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


markdown_pipeline = _load_markdown_pipeline()
_RUNTIME_LAYOUT.verify_module(
    markdown_pipeline,
    label="markdown-conversion pipeline",
    expected=_MARKDOWN / "pipeline.py",
)

VERSION = "2.0.2"
DEFAULT_CONFIG: dict[str, object] = {
    "pdf_ocr": dict(markdown_pipeline.DEFAULT_CONFIG["pdf_ocr"]),
    "pdf_images": dict(markdown_pipeline.DEFAULT_CONFIG["pdf_images"]),
    "pdf_conversion": DEFAULT_PDF_CONVERSION,
}
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


class PipelineError(ConversionError):
    pass


def _complete_envelope_stage(stage: Path) -> None:
    try:
        core.stage_complete(stage)
    except core.CoreAdapterError as exc:
        raise PipelineError(str(exc)) from exc


def _validate_envelope(root: Path) -> None:
    try:
        core.validate(root)
    except core.CoreAdapterError as exc:
        raise PipelineError(str(exc)) from exc


@dataclass(frozen=True)
class Target:
    path: Path
    stem: str


def die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def _reject_non_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-JSON numeric constant {value}")


def _is_url(value: str) -> bool:
    return bool(_URL_RE.match(value.strip()))


def _read_explicit_config(path: Path) -> dict[str, object]:
    try:
        selected = np.logical(path)
        info = np.lstat(selected)
    except (OSError, TypeError, ValueError) as exc:
        die(f"Could not access config path: {exc}")
    if stat.S_ISLNK(info.st_mode) or np.is_reparse(info) or not stat.S_ISREG(info.st_mode):
        die(f"Config path is not an ordinary regular non-link/reparse file: {selected}")
    try:
        raw = json.loads(
            np.read_text(selected, encoding="utf-8"),
            parse_constant=_reject_non_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        die(f"Could not read config as strict UTF-8 JSON: {exc}")
    if not isinstance(raw, dict):
        die("config root must be an object")
    return raw


def load_config(path: Path | None = None) -> dict[str, object]:
    if path is None:
        return copy.deepcopy(DEFAULT_CONFIG)
    raw = _read_explicit_config(path)
    result = dict(raw)
    ocr = raw.get("pdf_ocr", {})
    if not isinstance(ocr, dict):
        die("config pdf_ocr must be an object")
    result["pdf_ocr"] = {**DEFAULT_CONFIG["pdf_ocr"], **ocr}  # type: ignore[dict-item]
    images = raw.get("pdf_images", {})
    if not isinstance(images, dict):
        die("config pdf_images must be an object")
    result["pdf_images"] = {**DEFAULT_CONFIG["pdf_images"], **images}
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
    mode = getattr(args, "bundle_name_mode", "stem")
    if mode not in {"stem", "source-basename"}:
        raise PipelineError(f"Unsupported bundle name mode: {mode}")
    stem = (source_path.name if mode == "source-basename" else source_path.stem) or "untitled"
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
        _complete_envelope_stage(stage.path)
        if not stage.matches(stage.path):
            raise PipelineError(f"Owned router stage identity changed: {stage.path}")
        publish_owned(
            stage,
            target.path,
            args.overwrite,
            verify_payload=lambda root: (
                _validate_envelope(root),
                verify_validated_pdf(root / pdf_name, converter.settings.validation, expected),
            ),
        )
        return target.path, document["quality"]["status"], document["quality"]["warnings"]
    finally:
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
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--formats", default="")
    parser.add_argument(
        "--bundle-name-mode",
        choices=["stem", "source-basename"],
        default="stem",
        help="Name bundle directories and representations from the source stem or full basename",
    )
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
    parser.add_argument("--pdf-images", choices=["auto", "objects", "off"], default=None)
    parser.add_argument("--pdf-image-timeout", type=float, default=None)
    parser.add_argument("--ocr-engine", choices=["rapidocr"], default=None)
    parser.add_argument("--ocr-language", default=None)
    parser.add_argument("--ocr-dpi", type=float, default=None)
    parser.add_argument("--ocr-max-long-edge", type=int, default=None)
    parser.add_argument("--ocr-min-confidence", type=float, default=None)
    parser.add_argument("--libreoffice-path", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.version:
        config = load_config(args.config)
        show_version(config, args.libreoffice_path)
        return 0
    precheck(args)
    try:
        with core.operation(skill_entrypoint=Path(__file__).absolute(), skill_id="file-conversion"):
            return _run(args)
    except core.CoreAdapterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _run(args) -> int:
    config = load_config(args.config)
    # Resolve once for the whole invocation, before any per-item work.
    args.timestamp = markdown_pipeline.resolve_timestamp(args.timestamp)
    args.ocr_settings = markdown_pipeline.resolve_ocr_settings(args, config)
    args.pdf_image_settings = markdown_pipeline.resolve_pdf_image_settings(args, config)
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
