from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from pypdf import PdfWriter


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "file-conversion" / "scripts" / "pipeline.py"
MARKDOWN_SCRIPT = ROOT / "skills" / "markdown-conversion" / "scripts" / "pipeline.py"
SHARED = ROOT / "skills" / "_shared" / "scripts"
MARKDOWN_SCRIPTS = MARKDOWN_SCRIPT.parent
CONFIG = ROOT / "tests" / "file-conversion" / "fixtures" / "test_config.json"
MARKDOWN_CONFIG = ROOT / "tests" / "markdown-conversion" / "fixtures" / "test_config.json"
LO = Path(r"C:\Program Files\LibreOffice\program\soffice.com")

for value in (str(SHARED), str(MARKDOWN_SCRIPTS)):
    if value not in sys.path:
        sys.path.insert(0, value)


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pipeline = _module("file_conversion_pipeline_tests", SCRIPT)
import conversion_runtime
import libreoffice_pdf


def _cli(*args: object, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(CONFIG), *(str(item) for item in args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _source(tmp_path: Path, suffix: str = ".docx") -> Path:
    fixture = ROOT / "tests" / "markdown-conversion" / "fixtures" / "anydoc" / "text.docx"
    path = tmp_path / f"source{suffix}"
    path.write_bytes(fixture.read_bytes())
    return path


def _valid_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def _args(source: Path, output: Path, *extra: str):
    args = pipeline.build_parser().parse_args(
        ["--input", str(source), "--output-dir", str(output), *extra]
    )
    pipeline.precheck(args)
    args.timestamp = "2026-08-24T12:00:00+08:00"
    args.ocr_settings = pipeline.markdown_pipeline.OcrSettings(mode="off", engine="none")
    args.ocr_provider = None
    args.output_mode = "bundle"
    return args


class FakeEngine:
    def __init__(self, pdf: Path, *, fail: str = ""):
        self.pdf = pdf
        self.fail = fail
        self.settings = libreoffice_pdf.PdfConversionSettings()

    def convert(self, snapshot, destination, workspace):
        if self.fail:
            raise libreoffice_pdf.LibreOfficeError(self.fail)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.pdf, destination)
        return {"sha256": "a" * 64, "size_bytes": Path(destination).stat().st_size, "pages": 1}


def _fake_emitter(args, snapshot, stage, stem, *, status="complete", warnings=None):
    (Path(stage) / f"{stem}.md").write_text("Body\n", encoding="utf-8", newline="\n")
    (Path(stage) / f"{stem}.json").write_text("{}\n", encoding="utf-8", newline="\n")
    return {"quality": {"status": status, "warnings": warnings or []}}


def test_skill_frontmatter_and_project_versions():
    skill = (ROOT / "skills" / "file-conversion" / "SKILL.md").read_text(encoding="utf-8")
    assert "name: file-conversion" in skill and "version: 2.0.0" in skill
    assert json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"] == "7.0.0"
    assert json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"] == "7.0.0"
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"


@pytest.mark.parametrize("value", ["", "markdown,pdf", "pdf,md", "PDF,MARKDOWN"])
def test_formats_defaults_and_normalize_to_exact_pair(value):
    assert pipeline._normalize_formats(value) == ("markdown", "pdf")


@pytest.mark.parametrize("value", ["markdown", "pdf", "markdown,pdf,html", "html"])
def test_formats_singletons_and_unknowns_give_specialized_guidance(value):
    with pytest.raises(pipeline.PipelineError, match="specialized|markdown-conversion"):
        pipeline._normalize_formats(value)


def test_single_output_path_is_rejected_and_batch_alias_is_accepted(tmp_path):
    source = _source(tmp_path)
    result = _cli("--input", source, "--output-path", tmp_path / "one")
    assert result.returncode == 1 and "bundle-only" in result.stderr
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    _source(inputs)
    args = pipeline.build_parser().parse_args(["--input-dir", str(inputs), "--output-path", str(tmp_path / "out")])
    pipeline.precheck(args)
    assert args.output_dir == str(tmp_path / "out") and not args.output_path


@pytest.mark.parametrize("value", ["https://example.com/a.docx", "ftp://example.com/a.xlsx"])
def test_urls_are_rejected(value):
    result = _cli("--input", value)
    assert result.returncode == 1 and "local" in result.stderr.lower()


@pytest.mark.parametrize(
    ("name", "error_type", "message"),
    [
        ("ReCoRd.docx", pipeline.markdown_pipeline.PipelineError, "record.json"),
        (".CoRtEx-item.docx", pipeline.knowledge_unit.KnowledgeUnitError, "reserved Cortex name"),
    ],
)
def test_router_rejects_cortex_collisions_before_engine_or_write(
    tmp_path, monkeypatch, name, error_type, message
):
    source = _source(tmp_path)
    renamed = source.with_name(name)
    source.rename(renamed)
    output = tmp_path / "out"
    args = _args(renamed, output)
    monkeypatch.setattr(
        pipeline,
        "_engine",
        lambda *args, **kwargs: pytest.fail("unsafe bundle reached engine creation"),
    )
    monkeypatch.setattr(
        pipeline.np,
        "mkdir",
        lambda *args, **kwargs: pytest.fail("unsafe bundle reached mkdir"),
    )

    with pytest.raises(error_type, match=message):
        pipeline.convert_one(args, str(renamed), pipeline.load_config(CONFIG))

    assert not output.exists()


def test_timestamp_validation_and_alias_parsing(tmp_path):
    source = _source(tmp_path)
    result = _cli("--input", source, "--timestamp", "2026-08-24T12:00:00")
    assert result.returncode == 1 and "timezone" in result.stderr
    for alias in ("--local-adapter", "--document-adapter", "--local-document-adapter"):
        args = pipeline.build_parser().parse_args(["--input", str(source), alias, "markitdown"])
        assert args.local_adapter == "markitdown"


def test_bundle_name_mode_defaults_to_legacy_stem_and_preserves_nested_unicode(tmp_path):
    inputs = tmp_path / "inputs"
    (inputs / "nested").mkdir(parents=True)
    source = _source(inputs / "nested")
    source = source.rename(source.with_name("報告.final.docx"))
    output = tmp_path / "out"

    default_args = pipeline.build_parser().parse_args(
        ["--input", str(source), "--output-dir", str(output)]
    )
    assert default_args.bundle_name_mode == "stem"
    assert pipeline.resolve_target(default_args, str(source)).path == output / "報告.final"

    basename_args = pipeline.build_parser().parse_args(
        [
            "--input-dir", str(inputs), "--output-dir", str(output),
            "--bundle-name-mode", "source-basename",
        ]
    )
    target = pipeline.resolve_target(
        basename_args,
        str(source),
        source.relative_to(inputs),
    )
    assert target.path == output / "nested" / "報告.final.docx"
    assert target.stem == "報告.final.docx"


def test_config_is_superset_and_preserves_unknown_keys(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"unknown": {"keep": True}, "pdf_ocr": {"mode": "force"}, "pdf_conversion": {"timeout_seconds": 7}}), encoding="utf-8")
    loaded = pipeline.load_config(config)
    assert loaded["unknown"] == {"keep": True}
    assert loaded["pdf_ocr"]["mode"] == "force"
    assert loaded["pdf_ocr"]["dpi"] == 300.0
    assert loaded["pdf_conversion"]["timeout_seconds"] == 7
    assert loaded["pdf_conversion"]["validation"]["max_pdf_bytes"] == 1073741824


def test_timestamp_is_resolved_once_for_batch(monkeypatch, tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    _source(inputs)
    calls = []
    monkeypatch.setattr(pipeline.markdown_pipeline, "resolve_timestamp", lambda value: calls.append(value) or "fixed")
    monkeypatch.setattr(pipeline, "run_batch", lambda args, config: 0)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--config", str(CONFIG), "--input-dir", str(inputs)])
    assert pipeline.main() == 0
    assert calls == [""]


def test_partial_markdown_plus_valid_pdf_publishes_flat_bundle(tmp_path, monkeypatch):
    source = _source(tmp_path)
    out = tmp_path / "out"
    args = _args(source, out)
    warning = {"code": "loss", "message": "loss", "content_loss": True}
    monkeypatch.setattr(
        pipeline.markdown_pipeline,
        "emit_markdown_bundle",
        lambda a, s, stage, stem: _fake_emitter(a, s, stage, stem, status="partial", warnings=[warning]),
    )
    target, status, warnings = pipeline.convert_one(
        args, str(source), pipeline.load_config(CONFIG), engine=FakeEngine(_valid_pdf(tmp_path / "valid.pdf"))
    )
    assert status == "partial" and warnings == [warning]
    assert {path.name for path in target.iterdir()} == {"AGENTS.md", "CLAUDE.md", "assets", "source.md", "source.json", "source.pdf", "src"}
    assert (target / "assets/.keep").read_bytes() == b""


def test_pdf_hard_failure_retains_stage_and_preserves_overwrite_target(tmp_path, monkeypatch):
    source = _source(tmp_path)
    out = tmp_path / "out"
    target = out / "source"
    target.mkdir(parents=True)
    (target / "old").write_text("old", encoding="utf-8")
    args = _args(source, out, "--overwrite")
    monkeypatch.setattr(pipeline.markdown_pipeline, "emit_markdown_bundle", _fake_emitter)
    with pytest.raises(libreoffice_pdf.LibreOfficeError, match="invalid PDF"):
        pipeline.convert_one(
            args, str(source), pipeline.load_config(CONFIG), engine=FakeEngine(_valid_pdf(tmp_path / "valid.pdf"), fail="invalid PDF")
    )
    assert (target / "old").read_text(encoding="utf-8") == "old"
    assert len(list(out.glob(".fc-stage-*"))) == 1


def test_final_publish_failure_retains_router_stage_and_target(tmp_path, monkeypatch):
    source = _source(tmp_path)
    out = tmp_path / "out"
    target = out / "source"
    target.mkdir(parents=True)
    (target / "old").write_text("old", encoding="utf-8")
    args = _args(source, out, "--overwrite")
    monkeypatch.setattr(pipeline.markdown_pipeline, "emit_markdown_bundle", _fake_emitter)
    monkeypatch.setattr(
        conversion_runtime,
        "_remove_overwrite_target",
        lambda target_path: (_ for _ in ()).throw(OSError("injected target removal failure")),
    )

    with pytest.raises(pipeline.ConversionError, match="retained owned stage") as exc_info:
        pipeline.convert_one(
            args,
            str(source),
            pipeline.load_config(CONFIG),
            engine=FakeEngine(_valid_pdf(tmp_path / "valid.pdf")),
        )

    stages = list(out.glob(".fc-stage-*"))
    assert len(stages) == 1
    assert str(stages[0]) in str(exc_info.value)
    assert "Manual next step" in str(exc_info.value)
    assert (target / "old").read_text(encoding="utf-8") == "old"


def test_collision_rename_changes_directory_and_all_stems(tmp_path, monkeypatch):
    source = _source(tmp_path)
    out = tmp_path / "out"
    (out / "source").mkdir(parents=True)
    args = _args(source, out, "--rename")
    monkeypatch.setattr(pipeline.markdown_pipeline, "emit_markdown_bundle", _fake_emitter)
    target, _, _ = pipeline.convert_one(
        args, str(source), pipeline.load_config(CONFIG), engine=FakeEngine(_valid_pdf(tmp_path / "valid.pdf"))
    )
    assert target.name == "source_1"
    assert (target / "source_1.md").is_file()
    assert (target / "source_1.json").is_file()
    assert (target / "source_1.pdf").is_file()
    assert (target / "src" / "source.docx").is_file()


def test_source_basename_mode_keeps_same_stem_inputs_distinct(tmp_path, monkeypatch):
    docx = _source(tmp_path).rename(tmp_path / "report.docx")
    pdf = _valid_pdf(tmp_path / "report.pdf")
    provider_pdf = _valid_pdf(tmp_path / "provider.pdf")
    output = tmp_path / "out"
    monkeypatch.setattr(pipeline.markdown_pipeline, "emit_markdown_bundle", _fake_emitter)

    targets = []
    for source in (docx, pdf):
        args = _args(source, output, "--bundle-name-mode", "source-basename")
        target, _, _ = pipeline.convert_one(
            args,
            str(source),
            pipeline.load_config(CONFIG),
            engine=FakeEngine(provider_pdf),
        )
        targets.append(target)

    assert targets == [output / "report.docx", output / "report.pdf"]
    for source, target in zip((docx, pdf), targets, strict=True):
        basename = source.name
        assert (target / f"{basename}.md").is_file()
        assert (target / f"{basename}.json").is_file()
        assert (target / f"{basename}.pdf").is_file()
        assert (target / "src" / basename).read_bytes() == source.read_bytes()


def test_batch_collection_layout_and_exit_precedence(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    (inputs / "nested").mkdir(parents=True)
    _source(inputs)
    _source(inputs / "nested")
    (inputs / "ignored.txt").write_text("x", encoding="utf-8")
    args = pipeline.build_parser().parse_args(["--input-dir", str(inputs), "--output-dir", str(tmp_path / "out"), "--types", "docx"])
    pipeline.precheck(args)
    files = pipeline.collect_files(args)
    assert [Path(item).relative_to(inputs).as_posix() for item in files] == ["nested/source.docx", "source.docx"]


def test_real_router_bundle_matches_standalone_markdown_bytes(tmp_path):
    assert LO.is_file(), "Required installed LibreOffice is missing"
    source = _source(tmp_path)
    router_root = tmp_path / "router"
    markdown_root = tmp_path / "markdown"
    timestamp = "2026-08-24T12:34:56+08:00"
    router = _cli(
        "--input", source,
        "--output-dir", router_root,
        "--timestamp", timestamp,
        "--language-normalization", "preserve",
        "--ocr", "off",
        timeout=240,
    )
    assert router.returncode == 0, router.stderr
    standalone = subprocess.run(
        [
            sys.executable, str(MARKDOWN_SCRIPT), "--config", str(MARKDOWN_CONFIG),
            "--input", str(source), "--output-dir", str(markdown_root),
            "--timestamp", timestamp, "--language-normalization", "preserve", "--ocr", "off",
        ],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180,
    )
    assert standalone.returncode == 0, standalone.stderr
    routed = router_root / "source"
    original = markdown_root / "source"
    assert (routed / "source.pdf").is_file()
    relative_files = sorted(path.relative_to(original) for path in original.rglob("*") if path.is_file())
    assert relative_files
    for relative in relative_files:
        assert (routed / relative).read_bytes() == (original / relative).read_bytes(), relative
    canonical = json.loads((routed / "source.json").read_text(encoding="utf-8"))
    assert canonical["source"]["locator"] == str(source.resolve())
    assert canonical["source"]["sha256"]


def test_real_no_frontmatter_and_pdf_are_both_present(tmp_path):
    source = _source(tmp_path)
    result = _cli(
        "--input", source, "--output-dir", tmp_path / "out", "--no-frontmatter",
        "--timestamp", "2026-08-24", timeout=240,
    )
    assert result.returncode == 0, result.stderr
    bundle = tmp_path / "out" / "source"
    assert not (bundle / "source.md").read_text(encoding="utf-8").startswith("---")
    assert (bundle / "source.pdf").is_file()
