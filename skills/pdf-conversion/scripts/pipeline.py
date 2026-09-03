#!/usr/bin/env python3
"""Local PDF/Office-to-native-PDF conversion pipeline."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn


_SCRIPTS = Path(__file__).resolve().parent
_SHARED = _SCRIPTS.parents[1] / "_shared" / "scripts"
_MARKDOWN = _SCRIPTS.parents[1] / "markdown-conversion" / "scripts"
for _path in (str(_SHARED), str(_MARKDOWN), str(_SCRIPTS)):
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)
if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

import native_paths as np  # noqa: E402
import anti_entropy_core_adapter as core  # noqa: E402
from conversion_runtime import (  # noqa: E402
    ConversionError,
    OutputCollision,
    acquire_source_snapshot,
    cleanup_owned,
    new_owned_dir,
    new_owned_file,
    publish_owned,
    validate_target_not_source,
)
import knowledge_unit  # noqa: E402
from libreoffice_pdf import (  # noqa: E402
    DEFAULT_PDF_CONVERSION,
    EXPLICITLY_UNSUPPORTED_SUFFIXES,
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


VERSION = "2.0.1"
CONFIG_PATH = _SCRIPTS / "config.json"
DEFAULT_CONFIG: dict[str, object] = {"pdf_conversion": DEFAULT_PDF_CONVERSION}
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
    mode: str
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
        return merge_pdf_conversion_config({})
    try:
        raw = json.loads(np.read_text(selected, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: config.json parse error ({exc}), using defaults", file=sys.stderr)
        return merge_pdf_conversion_config({})
    if not isinstance(raw, dict):
        die("config root must be an object")
    return merge_pdf_conversion_config(raw)


def _normalize_mode(args) -> None:
    if args.output_path and args.output_dir:
        die("--output-path and --output-dir are mutually exclusive")
    if args.input_dir and args.output_path:
        args.output_dir = args.output_path
        args.output_path = ""
        print("Warning: batch --output-path is deprecated; use --output-dir", file=sys.stderr)
    if args.input and args.output_path:
        if args.output_mode == "bundle":
            die("--output-path conflicts with explicit --output-mode bundle")
        if Path(args.output_path).suffix.lower() != ".pdf":
            die("--output-path must end in .pdf")
        args.output_mode = "pdf"
    elif args.output_mode is None:
        args.output_mode = "bundle"
    if args.output_path and args.output_mode != "pdf":
        die("--output-path is valid only for single-file PDF output")


def _batch_root(args) -> Path:
    return np.logical(args.output_dir) if args.output_dir else np.logical(args.input_dir) / "_converted"


def _validate_batch_root(input_root: Path, output_root: Path) -> None:
    if np.paths_equal(input_root, output_root):
        raise PipelineError("Batch output root must not equal the input root")
    if np.is_within(input_root, output_root):
        raise PipelineError("Batch output root must not be an ancestor of the input root")


def _validate_suffix(path: str) -> None:
    classify_suffix(path)


def _normalize_types(value: str) -> set[str] | None:
    values = [item.strip().lower() for item in value.split(",") if item.strip()]
    if not values:
        return None
    normalized = {item if item.startswith(".") else f".{item}" for item in values}
    invalid = normalized - SUPPORTED_SUFFIXES
    if invalid:
        detail = ", ".join(sorted(invalid))
        raise PipelineError(f"Unsupported file type in --types: {detail}")
    return normalized


def precheck(args) -> None:
    if bool(args.input) == bool(args.input_dir):
        die("Exactly one of --input or --input-dir is required")
    if args.overwrite and args.rename:
        die("--overwrite and --rename are mutually exclusive")
    _normalize_mode(args)
    if args.input:
        if _is_url(args.input):
            die("URLs are not supported; pdf-conversion accepts local files only")
        if not np.is_file(args.input):
            die(f"File not found: {args.input}")
        try:
            _validate_suffix(args.input)
        except LibreOfficeError as exc:
            die(str(exc))
    else:
        if _is_url(args.input_dir):
            die("URLs are not supported; pdf-conversion accepts local directories only")
        if not np.is_dir(args.input_dir):
            die(f"Directory not found: {args.input_dir}")
        try:
            _validate_batch_root(np.logical(args.input_dir), _batch_root(args))
            _normalize_types(args.types)
        except ConversionError as exc:
            die(str(exc))


def collect_files(args) -> list[str]:
    root = np.logical(args.input_dir)
    output_root = _batch_root(args)
    types = _normalize_types(args.types)
    result: list[str] = []
    for path in np.walk_files(root, not args.no_recursive):
        if np.is_within(path, output_root):
            continue
        suffix = path.suffix.lower()
        if types is not None and suffix not in types:
            continue
        if suffix in SUPPORTED_SUFFIXES:
            result.append(str(path))
    return sorted(result)


def resolve_target(args, source: str, relative_path: Path | None = None) -> Target:
    source_path = np.logical(source)
    stem = source_path.stem or "untitled"
    if args.input and args.output_path:
        target_path = np.logical(args.output_path)
        return Target("pdf", target_path, target_path.stem)
    if args.input_dir:
        relative_path = relative_path or Path(source_path.name)
        root = _batch_root(args)
        bundle_parent = root / relative_path.parent
    else:
        root = np.logical(args.output_dir) if args.output_dir else source_path.parent
        bundle_parent = root
    if args.output_mode == "bundle":
        path = knowledge_unit.bundle_target(
            bundle_parent,
            stem,
            boundary=root if args.input_dir else bundle_parent,
        )
        return Target("bundle", path, stem)
    return Target("pdf", (bundle_parent / stem).with_suffix(".pdf"), stem)


def _renamed_target(target: Target) -> Target:
    if not np.exists(target.path):
        return target
    for index in range(1, 10000):
        stem = f"{target.stem}_{index}"
        path = target.path.with_name(stem if target.mode == "bundle" else f"{stem}.pdf")
        if not np.exists(path):
            return Target(target.mode, path, stem)
    raise PipelineError(f"Could not find an available renamed target for {target.path}")


def _preflight_target(target: Target, source: str, overwrite: bool, rename: bool) -> Target:
    source_path = np.logical(source)
    if target.mode == "pdf":
        validate_target_not_source(target.path, source_path)
    elif np.is_within(source_path, target.path):
        raise PipelineError("Refusing bundle output that contains the local source")
    if np.exists(target.path):
        if target.mode == "pdf":
            validate_target_not_source(target.path, source_path)
        if rename:
            return _renamed_target(target)
        if not overwrite:
            raise OutputCollision(f"Output already exists: {target.path}")
    return target


def _engine(args, config: dict[str, object]) -> LibreOfficePdfEngine:
    settings = PdfConversionSettings.from_config(config)
    engine = LibreOfficePdfEngine(settings)
    if args.libreoffice_path:
        engine = engine.with_cli_path(args.libreoffice_path)
    return engine


def convert_one(
    args,
    source: str,
    config: dict[str, object],
    relative_path: Path | None = None,
    engine: LibreOfficePdfEngine | None = None,
) -> tuple[Path, dict[str, object]]:
    target = _preflight_target(resolve_target(args, source, relative_path), source, args.overwrite, args.rename)
    converter = engine or _engine(args, config)
    if Path(source).suffix.lower() != ".pdf" and isinstance(converter, LibreOfficePdfEngine):
        _ = converter.executable
    np.mkdir(target.path.parent, parents=True, exist_ok=True)
    stage = new_owned_dir(target.path.parent, ".pc-stage-") if target.mode == "bundle" else None
    # LibreOffice creates deeply nested profile state and still has MAX_PATH-
    # sensitive components on Windows.  Keep its owned per-item root short.
    work = new_owned_dir(Path(tempfile.gettempdir()), ".pcw-")
    try:
        snapshot_path = (
            stage.path / "src" / np.logical(source).name
            if stage is not None
            else work.path / "snapshot" / np.logical(source).name
        )
        snapshot = acquire_source_snapshot(source, snapshot_path)
        if target.mode == "bundle":
            assert stage is not None
            pdf_path = stage.path / f"{target.stem}.pdf"
            result = converter.convert(snapshot, pdf_path, work.path / "libreoffice")
            expected = validate_pdf(pdf_path, converter.settings.validation)
            snapshot.verify()
            _complete_envelope_stage(stage.path)
            publish_owned(
                stage,
                target.path,
                args.overwrite,
                verify_payload=lambda root: (
                    _validate_envelope(root),
                    verify_validated_pdf(
                        root / f"{target.stem}.pdf", converter.settings.validation, expected
                    ),
                ),
            )
        else:
            generated = work.path / "generated" / f"{target.stem}.pdf"
            result = converter.convert(snapshot, generated, work.path / "libreoffice")
            output_stage = new_owned_file(target.path.parent, ".pc-stage-", ".pdf")
            # Replace the zero-byte exclusive stage through its still-owned handle.
            with np.open_file(generated, "rb") as incoming, np.open_file(output_stage.path, "wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            expected = validate_pdf(output_stage.path, converter.settings.validation)
            publish_owned(
                output_stage,
                target.path,
                args.overwrite,
                verify_payload=lambda root: verify_validated_pdf(
                    root, converter.settings.validation, expected
                ),
            )
        return target.path, result
    finally:
        if work.matches(work.path):
            cleanup_owned(work, warning="could not remove private PDF conversion workspace")


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
            path, result = convert_one(args, source, config, relative, engine)
            print(f"[OK] {relative.as_posix()} -> {path} ({result.get('pages')} pages)")
            converted += 1
        except OutputCollision as exc:
            print(f"[SKIP] {relative.as_posix()} — {exc}", file=sys.stderr)
            skipped += 1
        except Exception as exc:
            print(f"[FAIL] {relative.as_posix()} — {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
    print(f"[BATCH] {converted} converted, {failed} failed, {skipped} skipped")
    return 1 if failed else 2 if skipped else 0


def show_version(config: dict[str, object], cli_path: str = "") -> None:
    print(f"pdf-conversion v{VERSION}")
    raw = config.get("pdf_conversion", {})
    config_path = str(raw.get("libreoffice_path") or "") if isinstance(raw, dict) else ""
    try:
        path, version = resolve_libreoffice(cli_path, config_path)
        print(f"LibreOffice: {version} ({path})")
    except LibreOfficeError as exc:
        print(f"LibreOffice: NOT AVAILABLE ({exc})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert local PDF and Office files to native multipage PDF")
    parser.add_argument("--config", default="")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--input-dir")
    parser.add_argument("--output-mode", choices=["bundle", "pdf"], default=None)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--output-path", default="")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--rename", action="store_true")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--types", default="")
    parser.add_argument("--libreoffice-path", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.version:
        config = load_config(np.logical(args.config) if args.config else None)
        show_version(config, args.libreoffice_path)
        return 0
    precheck(args)
    if args.output_mode != "bundle":
        return _run(args)
    try:
        with core.operation(skill_entrypoint=Path(__file__).absolute(), skill_id="pdf-conversion"):
            return _run(args)
    except core.CoreAdapterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _run(args) -> int:
    config = load_config(np.logical(args.config) if args.config else None)
    if args.input_dir:
        return run_batch(args, config)
    try:
        path, result = convert_one(args, args.input, config)
    except OutputCollision as exc:
        print(f"ERROR: {exc}\nRe-run with --overwrite or --rename.", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] Converted {args.input} -> {path} ({result.get('pages')} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
