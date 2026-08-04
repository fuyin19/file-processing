"""
Tests for pipeline.py (v5.0.0 — deterministic draft frontmatter architecture).

Run from project root: pytest scripts/test_pipeline.py -v

Architecture note:
  The pipeline now accepts source documents (PDF, DOCX, TXT, etc.) as --input.
  Helper functions (fix_encoding, convert_chinese, inject_frontmatter, write_to_vault)
  are tested directly. Integration tests use real temp files.
"""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import pytest
import sys

# Path to the pipeline module
_PIPELINE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'markdown-conversion', 'scripts')
sys.path.insert(0, os.path.normpath(_PIPELINE_DIR))

SCRIPT = [sys.executable, os.path.normpath(os.path.join(_PIPELINE_DIR, 'pipeline.py'))]

# Test config — avoids touching the real config.json
_TEST_CONFIG = os.path.normpath(os.path.join(os.path.dirname(__file__), 'fixtures', 'test_config.json'))
CONFIG_ARG = ['--config', _TEST_CONFIG]

# --- Pure function tests -------------------------------------------------------

def test_fix_encoding_utf8_passthrough():
    """UTF-8 bytes should decode and re-encode cleanly."""
    from pipeline import fix_encoding
    text = fix_encoding(b'Hello world')
    assert text == 'Hello world'


def test_fix_encoding_gbk_detected_and_converted():
    """GBK-encoded Chinese bytes should be detected and decoded as UTF-8."""
    from pipeline import fix_encoding
    gbk_bytes = '你好世界'.encode('gbk')
    text = fix_encoding(gbk_bytes)
    assert isinstance(text, str)
    assert '你' in text  # Chinese chars present


def test_mojibake_detected_and_rejected():
    """Known mojibake patterns should fail the canonical gate."""
    from pipeline import CanonicalValidationError, MOJIBAKE_PATTERNS, fix_encoding
    with pytest.raises(CanonicalValidationError):
        fix_encoding(f'Hello {MOJIBAKE_PATTERNS[0]} world'.encode('utf-8'))


def test_convert_chinese_tc_to_simplified():
    """Traditional Chinese 愛 → Simplified 爱."""
    from pipeline import convert_chinese
    result = convert_chinese('愛')
    assert result == '爱'


def test_convert_chinese_yu_no_false_positive():
    """於 is valid in both TC and SC; must not false-positive on stability check."""
    from pipeline import convert_chinese
    # Should not raise — 於 is stable after two-pass
    result = convert_chinese('這份報告於三月完成')
    assert '於' in result or '于' in result  # either is valid SC


def test_convert_chinese_simplified_unchanged():
    """Pure Simplified Chinese should come out unchanged."""
    from pipeline import convert_chinese
    result = convert_chinese('欢迎')
    assert result == '欢迎'


def test_convert_chinese_mixed_all_converted():
    """Mixed TC+SC: 歡迎 (TC) → 欢迎 (SC)."""
    from pipeline import convert_chinese
    result = convert_chinese('歡迎')
    assert result == '欢迎'
    assert '歡' not in result


def _frontmatter_lines(document):
    return document.split('---\n', 2)[1].splitlines()


def test_inject_frontmatter_is_exact_five_field_draft():
    """Default frontmatter has exactly the required fields in stable order."""
    from pipeline import inject_frontmatter
    result = inject_frontmatter('Hello', '/path/to/file.pdf', '2026-03-23')
    assert result.startswith('---')
    assert _frontmatter_lines(result) == [
        'type: ""',
        'title: "file"',
        'description: ""',
        'tags: []',
        'timestamp: "2026-03-23"',
    ]
    assert 'resource:' not in result
    assert 'Hello' in result


def test_inject_frontmatter_prefers_first_h1_for_title():
    from pipeline import inject_frontmatter
    result = inject_frontmatter('# Report Title\n\nBody', '/path/to/file.pdf', '2026-03-23T10:00:00')
    assert 'title: "Report Title"' in result


def test_inject_frontmatter_ignores_fenced_code_and_trims_closing_hashes():
    from pipeline import inject_frontmatter
    body = '```markdown\n# Not a heading\n```\n\n# Actual Title ###\n'
    result = inject_frontmatter(body, '/path/to/file.pdf', '2026-03-23')
    assert 'title: "Actual Title"' in result


def test_inject_frontmatter_uses_source_stem_without_h1():
    from pipeline import inject_frontmatter
    result = inject_frontmatter('Body only', '/path/to/quarterly.report.pdf', '2026-03-23')
    assert 'title: "quarterly.report"' in result


# --- Vault write tests (pure function) ---------------------------------------

def test_write_to_vault_creates_file(tmp_path):
    from pipeline import write_to_vault
    out = tmp_path / 'note.md'
    result = write_to_vault('# Hello', str(out), False, False)
    assert os.path.exists(result)
    assert out.read_text(encoding='utf-8') == '# Hello'


def test_write_to_vault_exists_exits_2(tmp_path):
    from pipeline import write_to_vault
    out = tmp_path / 'note.md'
    out.write_text('existing', encoding='utf-8')
    with pytest.raises(SystemExit) as exc_info:
        write_to_vault('new', str(out), False, False)
    assert exc_info.value.code == 2


def test_write_to_vault_overwrite_replaces(tmp_path):
    from pipeline import write_to_vault
    out = tmp_path / 'note.md'
    out.write_text('old', encoding='utf-8')
    result = write_to_vault('new', str(out), True, False)
    assert out.read_text(encoding='utf-8') == 'new'


def test_write_to_vault_rename_creates_dated_copy(tmp_path):
    import re
    from pipeline import write_to_vault
    out = tmp_path / 'note.md'
    out.write_text('old', encoding='utf-8')
    result = write_to_vault('new', str(out), False, True)
    # original still exists unchanged
    assert out.read_text(encoding='utf-8') == 'old'
    # renamed file created
    assert result != str(out)
    files = list(tmp_path.iterdir())
    assert len(files) == 2


# --- Integration tests (real files, full pipeline) -----------------------------

def _run_pipeline(input_path, extra_args=None, output_path=None):
    """Run pipeline.py with a real file. Returns (returncode, stdout, stderr)."""
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix='.md')
        os.close(fd)
        os.unlink(output_path)
    args = SCRIPT + CONFIG_ARG + [
        '--input', input_path,
        '--output-path', output_path,
    ]
    if extra_args:
        args += extra_args
    result = subprocess.run(args, capture_output=True)
    return result.returncode, result.stdout.decode('utf-8', errors='replace'), result.stderr.decode('utf-8', errors='replace'), output_path


def test_full_pipeline_creates_vault_file(tmp_path):
    """Full pipeline: source file → pipeline → vault file with frontmatter."""
    # Create a simple text file (markitdown supports .txt)
    src = tmp_path / 'source.txt'
    src.write_text('Hello world', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, out_str, err, _ = _run_pipeline(str(src), output_path=str(out))
    assert code == 0, err
    assert out.exists()
    content = out.read_text(encoding='utf-8')
    assert content.startswith('---')
    assert _frontmatter_lines(content)[:4] == [
        'type: ""',
        'title: "source"',
        'description: ""',
        'tags: []',
    ]
    assert 'resource:' not in content
    assert 'Hello world' in content


def test_full_pipeline_no_frontmatter_flag(tmp_path):
    """--no-frontmatter should skip frontmatter injection."""
    src = tmp_path / 'source.txt'
    src.write_text('Content here', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, _, err, _ = _run_pipeline(str(src), output_path=str(out), extra_args=['--no-frontmatter'])
    assert code == 0, err
    content = out.read_text(encoding='utf-8')
    assert not content.startswith('---')


def test_full_pipeline_preserves_chinese(tmp_path):
    """Chinese content should survive the pipeline unchanged (encoding handled by markitdown)."""
    src = tmp_path / 'chinese.txt'
    src.write_text('你好世界', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, _, err, _ = _run_pipeline(str(src), output_path=str(out))
    assert code == 0, err
    content = out.read_text(encoding='utf-8')
    assert '你好世界' in content


def test_success_message_printed_to_stdout(tmp_path):
    """On success, stdout should contain [OK] message."""
    src = tmp_path / 'source.txt'
    src.write_text('content', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, stdout, err, _ = _run_pipeline(str(src), output_path=str(out))
    assert code == 0, err
    assert '[OK]' in stdout or 'Converted' in stdout


@pytest.mark.parametrize('timestamp', [
    '2026-07-22',
    '2026-07-22T14:05:06+08:00',
    '2026-07-22T14:05:06.123456-04:30',
    '2026-07-22T06:05:06Z',
])
def test_timestamp_override_is_validated_and_preserved_exactly(tmp_path, timestamp):
    src = tmp_path / 'source.txt'
    src.write_text('Body', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, _, err, _ = _run_pipeline(
        str(src), output_path=str(out), extra_args=['--timestamp', timestamp]
    )

    assert code == 0, err
    assert f'timestamp: "{timestamp}"' in out.read_text(encoding='utf-8')


@pytest.mark.parametrize('timestamp', [
    '2026-07-22T14:05:06',
    '2026-07-22 14:05:06+08:00',
    '2026-07-22T14:05:06+0800',
    '2026-07-22T14:05:06z',
    '2026/07/22',
    'not-a-time',
])
def test_timestamp_override_rejects_naive_datetime_and_non_iso_values(tmp_path, timestamp):
    src = tmp_path / 'source.txt'
    src.write_text('Body', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, _, err, _ = _run_pipeline(
        str(src), output_path=str(out), extra_args=['--timestamp', timestamp]
    )

    assert code == 1
    assert '--timestamp' in err
    assert not out.exists()


def test_default_timestamp_is_timezone_aware(tmp_path):
    src = tmp_path / 'source.txt'
    src.write_text('Body', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, _, err, _ = _run_pipeline(str(src), output_path=str(out))

    assert code == 0, err
    timestamp_line = _frontmatter_lines(out.read_text(encoding='utf-8'))[-1]
    value = timestamp_line.removeprefix('timestamp: "').removesuffix('"')
    parsed = __import__('datetime').datetime.fromisoformat(value.replace('Z', '+00:00'))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() is not None


# --- Config loading tests -------------------------------------------------------

import json


def test_load_config_creates_default_when_missing(tmp_path, monkeypatch):
    """When config.json doesn't exist, create it with defaults and return it."""
    from pipeline import load_config, DEFAULT_CONFIG
    config_path = tmp_path / "config.json"
    monkeypatch.setattr("pipeline.CONFIG_PATH", str(config_path))
    cfg = load_config()
    assert cfg == DEFAULT_CONFIG
    assert config_path.exists()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == DEFAULT_CONFIG


def test_load_config_reads_existing(tmp_path, monkeypatch):
    """When config.json exists, read and return it."""
    from pipeline import load_config
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"custom_key": "custom-value"}), encoding="utf-8")
    monkeypatch.setattr("pipeline.CONFIG_PATH", str(config_path))
    cfg = load_config()
    assert cfg["custom_key"] == "custom-value"


def test_load_config_preserves_extra_keys(tmp_path, monkeypatch):
    """Config with extra keys should preserve them after merging with defaults."""
    from pipeline import load_config
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"custom_key": "custom-value"}), encoding="utf-8")
    monkeypatch.setattr("pipeline.CONFIG_PATH", str(config_path))
    cfg = load_config()
    assert cfg["custom_key"] == "custom-value"


def test_pdf_ocr_config_merges_defaults_and_cli_takes_precedence():
    from pipeline import DEFAULT_CONFIG, build_parser, resolve_ocr_settings

    parser = build_parser()
    assert resolve_ocr_settings(parser.parse_args([]), DEFAULT_CONFIG).mode == 'auto'
    config = {
        'pdf_ocr': {
            'mode': 'auto',
            'engine': 'rapidocr',
            'language': 'ch',
            'dpi': 240,
            'max_long_edge': 3500,
            'min_confidence': 0.62,
        }
    }
    configured = resolve_ocr_settings(parser.parse_args([]), config)
    assert configured.mode == 'auto'
    assert configured.dpi == 240
    assert configured.min_confidence == pytest.approx(0.62)

    overridden = resolve_ocr_settings(
        parser.parse_args([
            '--ocr', 'force', '--ocr-dpi', '300',
            '--ocr-min-confidence', '0.75',
        ]),
        config,
    )
    assert overridden.mode == 'force'
    assert overridden.dpi == 300
    assert overridden.min_confidence == pytest.approx(0.75)
    assert overridden.max_long_edge == 3500


def test_pdf_ocr_config_rejects_invalid_threshold(capsys):
    from pipeline import build_parser, resolve_ocr_settings

    with pytest.raises(SystemExit) as exc_info:
        resolve_ocr_settings(
            build_parser().parse_args([]),
            {'pdf_ocr': {'mode': 'auto', 'min_confidence': 1.5}},
        )

    assert exc_info.value.code == 1
    assert 'min_confidence' in capsys.readouterr().err


def test_load_config_invalid_json_uses_defaults(tmp_path, monkeypatch):
    """Malformed config.json should warn and use defaults."""
    from pipeline import load_config, DEFAULT_CONFIG
    config_path = tmp_path / "config.json"
    config_path.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr("pipeline.CONFIG_PATH", str(config_path))
    cfg = load_config()
    assert cfg == DEFAULT_CONFIG


# --- Default output path tests -------------------------------------------------


def _args_ns(**kwargs):
    """Build a minimal argparse.Namespace for resolve_output_path."""
    from argparse import Namespace
    base = {
        'input': None, 'input_dir': None, 'output_path': '', 'output_dir': '',
        'output_mode': None,
    }
    base.update(kwargs)
    return Namespace(**base)


@pytest.mark.parametrize('stem', [
    '0. Table of contents',
    '3. MB Rule 9.03(1)(b) Initial listing fee',
    '10. FF004 - Lynk Pharmaceuticals Co. Ltd',
])
def test_resolve_target_preserves_dotted_logical_stem(tmp_path, stem):
    from pipeline import resolve_target

    src = tmp_path / f'{stem}.txt'
    src.write_text('Body', encoding='utf-8')
    output = tmp_path / 'out'

    markdown = resolve_target(
        _args_ns(input=str(src), output_dir=str(output), output_mode='markdown'),
        str(src),
    )
    assert markdown.path == output / f'{stem}.md'
    assert markdown.stem == stem

    bundle = resolve_target(
        _args_ns(input=str(src), output_dir=str(output), output_mode='bundle'),
        str(src),
    )
    assert bundle.path == output / stem
    assert bundle.stem == stem


def test_resolve_output_path_single_file_defaults_to_sibling_bundle(tmp_path):
    from pipeline import resolve_output_path
    src = tmp_path / 'doc.pdf'
    src.write_text('x', encoding='utf-8')
    out = resolve_output_path(_args_ns(input=str(src)))
    assert out == os.path.join(str(tmp_path), 'doc')


def test_resolve_output_path_url_to_cwd_bundle():
    from pipeline import resolve_output_path
    out = resolve_output_path(_args_ns(input='https://example.com/page.html'))
    assert not out.endswith('.md')
    assert out.endswith('page')


def test_resolve_output_path_batch_uses_converted_directory(tmp_path):
    from pipeline import resolve_output_path
    d = tmp_path / 'srcs'
    d.mkdir()
    assert resolve_output_path(_args_ns(input_dir=str(d))) == str(d / '_converted')


# --- Batch mode tests -----------------------------------------------------------

def _run_batch(input_dir, output_dir, extra_args=None):
    """Run pipeline.py in batch mode. Returns (returncode, stdout, stderr)."""
    args = SCRIPT + CONFIG_ARG + [
        '--input-dir', input_dir,
        '--output-path', output_dir,
    ]
    if extra_args:
        args += extra_args
    result = subprocess.run(args, capture_output=True)
    return result.returncode, result.stdout.decode('utf-8', errors='replace'), result.stderr.decode('utf-8', errors='replace')


def test_batch_converts_multiple_files(tmp_path):
    """Batch mode should convert all supported files in a directory."""
    src_dir = tmp_path / 'input'
    src_dir.mkdir()
    (src_dir / 'a.txt').write_text('![Chart](chart.png)\n\nFile A', encoding='utf-8')
    (src_dir / 'b.txt').write_text('File B', encoding='utf-8')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    timestamp = '2026-07-22T14:05:06+08:00'
    code, stdout, stderr = _run_batch(
        str(src_dir), str(out_dir), extra_args=['--timestamp', timestamp]
    )
    assert code == 0, stderr
    assert (out_dir / 'a' / 'a.json').exists()
    assert (out_dir / 'a' / 'a.md').exists()
    assert (out_dir / 'b' / 'b.md').exists()
    first = (out_dir / 'a' / 'a.md').read_text(encoding='utf-8')
    assert 'File A' in first
    assert 'File B' in (out_dir / 'b' / 'b.md').read_text(encoding='utf-8')
    assert _frontmatter_lines(first) == [
        'type: ""',
        'title: "a"',
        'description: ""',
        'tags: []',
        f'timestamp: "{timestamp}"',
    ]
    assert 'chart.png' not in first
    assert '![' not in first
    assert 'Chart' in first
    assert 'resource:' not in first


def test_batch_mirrors_subdirectory_structure(tmp_path):
    """Batch mode should mirror subdirectory structure in output."""
    src_dir = tmp_path / 'input'
    sub = src_dir / 'sub'
    sub.mkdir(parents=True)
    (src_dir / 'top.txt').write_text('Top', encoding='utf-8')
    (sub / 'nested.txt').write_text('Nested', encoding='utf-8')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir))
    assert code == 0, stderr
    assert (out_dir / 'top' / 'top.md').exists()
    assert (out_dir / 'sub' / 'nested' / 'nested.md').exists()


def test_batch_no_recursive_flattens(tmp_path):
    """--no-recursive should only convert top-level files."""
    src_dir = tmp_path / 'input'
    sub = src_dir / 'sub'
    sub.mkdir(parents=True)
    (src_dir / 'top.txt').write_text('Top', encoding='utf-8')
    (sub / 'nested.txt').write_text('Nested', encoding='utf-8')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir), ['--no-recursive'])
    assert code == 0, stderr
    assert (out_dir / 'top' / 'top.md').exists()
    assert not (out_dir / 'sub' / 'nested' / 'nested.md').exists()


def test_batch_types_filter(tmp_path):
    """--types should filter to specified extensions only."""
    src_dir = tmp_path / 'input'
    src_dir.mkdir()
    (src_dir / 'keep.txt').write_text('Keep', encoding='utf-8')
    (src_dir / 'skip.html').write_text('<p>Skip</p>', encoding='utf-8')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir), ['--types', 'txt'])
    assert code == 0, stderr
    assert (out_dir / 'keep' / 'keep.md').exists()
    assert not (out_dir / 'skip').exists()


def test_batch_invalid_types_exits_1(tmp_path):
    """--types with unsupported extension should error."""
    src_dir = tmp_path / 'input'
    src_dir.mkdir()
    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir), ['--types', 'xyz'])
    assert code == 1
    assert 'Unsupported' in stderr


def test_batch_continues_on_error(tmp_path):
    """Batch should continue converting when one file fails."""
    src_dir = tmp_path / 'input'
    src_dir.mkdir()
    (src_dir / 'good.txt').write_text('Good', encoding='utf-8')
    # Invalid PDF forces the native adapter to fail while the text file succeeds.
    (src_dir / 'bad.pdf').write_bytes(b'not-a-pdf')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir), ['--types', 'txt,pdf'])
    # The invalid PDF fails but the TXT is still published; aggregate exit is failure.
    assert code == 1, stderr
    assert (out_dir / 'good' / 'good.md').exists()


def test_batch_empty_directory(tmp_path):
    """Batch on empty directory should succeed with 0 converted."""
    src_dir = tmp_path / 'input'
    src_dir.mkdir()
    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir))
    assert code == 0
    assert '0 converted' in stdout or '[BATCH]' in stdout


def test_batch_input_dir_not_found(tmp_path):
    """Non-existent input-dir should error."""
    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(tmp_path / 'nonexistent'), str(out_dir))
    assert code == 1
    assert 'not found' in stderr.lower() or 'Directory' in stderr


def test_batch_file_exists_skip(tmp_path):
    """Existing output files should be skipped (not fatal) in batch mode."""
    src_dir = tmp_path / 'input'
    src_dir.mkdir()
    (src_dir / 'file.txt').write_text('New content', encoding='utf-8')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()
    (out_dir / 'file').mkdir()
    (out_dir / 'file' / 'file.md').write_text('Old content', encoding='utf-8')

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir))
    assert code == 2
    assert 'skipped' in stdout.lower() or 'skipped' in stderr.lower()


def test_batch_summary_output(tmp_path):
    """Batch should print summary with converted/failed/skipped counts."""
    src_dir = tmp_path / 'input'
    src_dir.mkdir()
    (src_dir / 'a.txt').write_text('A', encoding='utf-8')
    (src_dir / 'b.txt').write_text('B', encoding='utf-8')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir))
    assert code == 0
    assert '[BATCH]' in stdout
    assert '2 converted' in stdout


# --- Precheck tests ----------------------------------------------------------------


def _run_pipeline_cli(*extra_args):
    """Run pipeline.py with given args. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        SCRIPT + CONFIG_ARG + list(extra_args),
        capture_output=True,
    )
    return result.returncode, result.stdout.decode('utf-8', errors='replace'), result.stderr.decode('utf-8', errors='replace')


def test_precheck_file_not_found(tmp_path):
    """--input with non-existent file should exit 1."""
    code, stdout, stderr = _run_pipeline_cli(
        '--input', str(tmp_path / 'nonexistent.pdf'),
        '--output-path', str(tmp_path / 'out.md'),
    )
    assert code == 1
    assert 'File not found' in stderr or 'not found' in stderr.lower()


def test_precheck_directory_not_found(tmp_path):
    """--input-dir with non-existent directory should exit 1."""
    code, stdout, stderr = _run_pipeline_cli(
        '--input-dir', str(tmp_path / 'nonexistent-dir'),
        '--output-path', str(tmp_path / 'output'),
    )
    assert code == 1
    assert 'Directory not found' in stderr or 'not found' in stderr.lower()


def test_precheck_mutual_exclusivity(tmp_path):
    """Both --input and --input-dir should exit 1."""
    src = tmp_path / 'test.txt'
    src.write_text('hello', encoding='utf-8')
    code, stdout, stderr = _run_pipeline_cli(
        '--input', str(src),
        '--input-dir', str(tmp_path),
        '--output-path', str(tmp_path / 'out.md'),
    )
    assert code == 1
    assert 'exactly one' in stderr.lower()


def test_precheck_neither_input_nor_dir(tmp_path):
    """No --input or --input-dir should exit 1."""
    code, stdout, stderr = _run_pipeline_cli(
        '--output-path', str(tmp_path / 'out.md'),
    )
    assert code == 1
    assert 'required' in stderr.lower()


# --- --version and --config tests ------------------------------------------------


def test_version_flag():
    """--version should print version and dependency status, then exit 0."""
    result = subprocess.run(SCRIPT + ['--version'], capture_output=True)
    assert result.returncode == 0
    stdout = result.stdout.decode('utf-8')
    assert 'markdown-conversion v' in stdout
    assert 'Dependencies:' in stdout
    # Should show pip install names (not import names)
    assert 'opencc-python-reimplemented' in stdout
    assert 'markdown-conversion v6.2.0' in stdout
    assert 'rapidocr:' in stdout
    assert 'onnxruntime:' in stdout
    assert 'ruamel.yaml:' not in stdout
    assert 'cortex:' not in stdout


def test_config_flag_uses_alternate_config(tmp_path):
    """--config should load config from the specified path."""
    cfg = tmp_path / 'custom.json'
    cfg.write_text('{"custom_key": "custom-value"}', encoding='utf-8')
    result = subprocess.run(
        SCRIPT + ['--config', str(cfg), '--version'],
        capture_output=True,
    )
    assert result.returncode == 0
    # Config was loaded without error (would fail if path was ignored)


# --- URL utility tests ---------------------------------------------------------


def test_is_url_http():
    """HTTP URLs should be detected."""
    from pipeline import is_url
    assert is_url('http://example.com/file.pdf')
    assert is_url('HTTP://EXAMPLE.COM/FILE.PDF')


def test_is_url_https():
    """HTTPS URLs should be detected."""
    from pipeline import is_url
    assert is_url('https://example.com/file.pdf')


def test_is_url_file_path():
    """Local file paths should not be detected as URLs."""
    from pipeline import is_url
    assert not is_url('/local/path/file.pdf')
    assert not is_url('C:\\Users\\doc.pdf')
    assert not is_url('./relative.pdf')


def test_is_url_empty():
    """Empty string should not be detected as URL."""
    from pipeline import is_url
    assert not is_url('')


def test_url_to_slug_basic():
    """Basic URL should produce a clean slug."""
    from pipeline import url_to_slug
    slug = url_to_slug('https://example.com/docs/report.pdf')
    assert 'example' in slug
    assert 'report' in slug
    assert '://' not in slug
    assert '/' not in slug


def test_url_to_slug_root():
    """Root URL should fall back to domain."""
    from pipeline import url_to_slug
    slug = url_to_slug('https://example.com/')
    assert 'example' in slug


def test_url_to_slug_query_string():
    """Query strings should be excluded from slug."""
    from pipeline import url_to_slug
    slug = url_to_slug('https://example.com/page?id=1&name=test')
    assert '?' not in slug
    assert 'page' in slug


def test_url_to_slug_encoded():
    """URL-encoded characters should be decoded."""
    from pipeline import url_to_slug
    slug = url_to_slug('https://example.com/my%20document.pdf')
    assert 'my' in slug
    assert 'document' in slug


def test_url_to_slug_truncation():
    """Very long URLs should be truncated."""
    from pipeline import url_to_slug
    long_url = 'https://example.com/' + 'a' * 200 + '.pdf'
    slug = url_to_slug(long_url)
    assert len(slug) <= 120


def test_url_to_slug_empty_fallback():
    """URL with minimal content should still produce a slug."""
    from pipeline import url_to_slug
    slug = url_to_slug('https://example.com')
    assert slug  # not empty


# --- Image stripping tests ----------------------------------------------------


def test_strip_images_removes_md_image():
    """Markdown image syntax should be removed."""
    from pipeline import strip_images
    result = strip_images('text ![alt](image.jpg) more')
    assert '![alt](image.jpg)' not in result
    assert 'text' in result
    assert 'more' in result


def test_strip_images_removes_orphaned_filename():
    """Standalone image filename lines should be removed."""
    from pipeline import strip_images
    result = strip_images('before\nphoto.jpg\nafter')
    assert 'photo.jpg' not in result
    assert 'before' in result
    assert 'after' in result


def test_strip_images_preserves_normal_text():
    """Non-image text should be unchanged."""
    from pipeline import strip_images
    text = 'Hello world\nThis is a paragraph.\nAnother line.'
    assert strip_images(text) == text


def test_strip_images_multiple_images():
    """Multiple images on one line should all be removed."""
    from pipeline import strip_images
    result = strip_images('![a](x.jpg) text ![b](y.png)')
    assert '![' not in result
    assert 'text' in result


def test_strip_images_collapses_blank_lines():
    """Multiple blank lines from removal should be collapsed."""
    from pipeline import strip_images
    result = strip_images('a\n\n\n\nb')
    assert '\n\n\n' not in result


def test_strip_images_orphaned_various_extensions():
    """Various image extensions on their own line should be stripped."""
    from pipeline import strip_images
    result = strip_images('before\nimage.png\nmiddle\nphoto.gif\nafter')
    assert 'image.png' not in result
    assert 'photo.gif' not in result
    assert 'before' in result
    assert 'after' in result


# --- URL precheck integration tests --------------------------------------------


def test_precheck_url_accepted(tmp_path):
    """A URL as --input should not trigger 'File not found' precheck."""
    code, stdout, stderr = _run_pipeline_cli(
        '--input', 'https://example.com/test.html',
        '--output-path', str(tmp_path / 'out.md'),
    )
    # Precheck passed — failure is from conversion (network), not "File not found"
    assert 'File not found' not in stderr
    # It may fail with HTTP error or succeed — both are fine for this test
    # The important thing is precheck didn't reject the URL as a missing file


def test_precheck_url_not_accepted_with_input_dir(tmp_path):
    """--input-dir should not accept URLs."""
    code, stdout, stderr = _run_pipeline_cli(
        '--input-dir', 'https://example.com/folder',
        '--output-path', str(tmp_path / 'output'),
    )
    # This should fail because input-dir expects a local directory
    # (os.path.isdir on a URL will fail)
    assert code == 1


# --- Removed runtime-coupling options ------------------------------------------


def test_removed_flags_are_not_advertised():
    result = subprocess.run(SCRIPT + ['--help'], capture_output=True)
    assert result.returncode == 0
    stdout = result.stdout.decode('utf-8', errors='replace')
    for option in (
        '--keep-images', '--okf', '--workspace', '--okf-run-dir',
        '--accept-partial', '--source', '--converted-at',
    ):
        assert option not in stdout


@pytest.mark.parametrize('option', [
    '--keep-images', '--okf', '--workspace', '--okf-run-dir', '--accept-partial',
    '--source', '--converted-at',
])
def test_removed_flags_are_rejected(tmp_path, option):
    src = tmp_path / 'source.txt'
    src.write_text('Hello', encoding='utf-8')
    out = tmp_path / 'final.md'
    extra = [option]
    if option in {'--workspace', '--okf-run-dir', '--source', '--converted-at'}:
        extra.append(str(tmp_path / 'value'))
    result = subprocess.run(
        SCRIPT + CONFIG_ARG + ['--input', str(src), '--output-path', str(out), *extra],
        capture_output=True,
    )
    assert result.returncode == 2
    assert not out.exists()
    assert 'unrecognized arguments' in result.stderr.decode('utf-8', errors='replace').lower()


@pytest.mark.parametrize('option', [
    '--keep-images', '--okf', '--workspace', '--okf-run-dir', '--accept-partial',
    '--source', '--converted-at',
])
def test_removed_flags_are_rejected_even_with_version(option):
    args = ['--version', option]
    if option in {'--workspace', '--okf-run-dir', '--source', '--converted-at'}:
        args.append('value')
    result = subprocess.run(SCRIPT + args, capture_output=True)
    assert result.returncode == 2
    assert 'unrecognized arguments' in result.stderr.decode('utf-8', errors='replace').lower()


def test_markdown_only_replaces_images_with_caption_text(tmp_path):
    src = tmp_path / 'images.txt'
    src.write_text('# Title\n\n![Chart](chart.png)\n\nSome text.\n\n![Graph](graph.jpg)\n\nMore text.', encoding='utf-8')
    out = tmp_path / 'out.md'

    code, stdout, stderr, _ = _run_pipeline(
        str(src), output_path=str(out),
    )
    assert code == 0, stderr
    content = out.read_text(encoding='utf-8')
    assert '![' not in content
    assert 'chart.png' not in content
    assert 'Chart' in content
    assert 'Graph' in content
    assert 'Some text.' in content
    assert 'More text.' in content


# --- Additional unit tests ------------------------------------------------------


def test_url_to_slug_chinese():
    """URL with Chinese characters should preserve them in slug."""
    from pipeline import url_to_slug
    slug = url_to_slug('https://example.com/文档/报告.pdf')
    assert 'example' in slug
    # Chinese chars should be preserved
    assert len(slug) > 0


def test_url_to_slug_auth():
    """URL with auth info should strip credentials from slug."""
    from pipeline import url_to_slug
    slug = url_to_slug('https://user:pass@example.com/docs/report.pdf')
    assert 'user' not in slug
    assert 'pass' not in slug
    assert 'example' in slug


def test_strip_images_protects_code_blocks():
    from pipeline import strip_images
    raw = '```\n![inside code](img.jpg)\n```\n'
    result = strip_images(raw)
    assert '![inside code](img.jpg)' in result


def test_strip_images_preserves_links():
    """Regular markdown links (not images) should be preserved."""
    from pipeline import strip_images
    raw = 'Check out [this link](https://example.com) for more info.'
    assert strip_images(raw) == raw


# --- v6 canonical bundle and adapter acceptance tests -------------------------


def _run_bundle(source, output_dir, extra_args=None):
    args = SCRIPT + CONFIG_ARG + ['--input', str(source), '--output-dir', str(output_dir)]
    if extra_args:
        args += list(extra_args)
    result = subprocess.run(args, capture_output=True)
    bundle = Path(output_dir) / Path(source).stem
    return (
        result.returncode,
        result.stdout.decode('utf-8', errors='replace'),
        result.stderr.decode('utf-8', errors='replace'),
        bundle,
    )


def _load_bundle(bundle):
    return json.loads((bundle / f'{bundle.name}.json').read_text(encoding='utf-8'))


def test_default_bundle_contains_canonical_json_and_markdown(tmp_path):
    src = tmp_path / 'report.txt'
    src.write_text('# Report\n\nBody', encoding='utf-8')
    code, stdout, stderr, bundle = _run_bundle(src, tmp_path / 'out')
    assert code == 0, stderr
    assert sorted(path.name for path in bundle.iterdir()) == ['report.json', 'report.md']
    data = _load_bundle(bundle)
    assert data['schema_version'] == '1.0'
    assert data['outputs']['mode'] == 'bundle'
    assert data['document']['document_id'] == f"sha256:{data['source']['sha256']}"
    assert data['quality']['status'] == 'complete'


def test_single_file_without_output_flags_uses_sibling_bundle(tmp_path):
    src = tmp_path / 'sibling.txt'
    src.write_text('Body', encoding='utf-8')
    result = subprocess.run(SCRIPT + CONFIG_ARG + ['--input', str(src)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert (tmp_path / 'sibling' / 'sibling.json').exists()
    assert (tmp_path / 'sibling' / 'sibling.md').exists()


def test_output_path_rejects_explicit_bundle_mode(tmp_path):
    src = tmp_path / 'source.txt'
    src.write_text('Body', encoding='utf-8')
    target = tmp_path / 'target.md'
    result = subprocess.run(
        SCRIPT + CONFIG_ARG + [
            '--input', str(src), '--output-mode', 'bundle', '--output-path', str(target),
        ],
        capture_output=True,
    )
    assert result.returncode == 1
    assert '--output-path is valid only' in result.stderr.decode(errors='replace')
    assert not target.exists()


def test_markdown_only_emits_exactly_one_file_and_no_dead_image_links(tmp_path):
    src = tmp_path / 'images.txt'
    src.write_text('![Caption](missing.png)\n\nBody\n\n![](silent.png)', encoding='utf-8')
    target = tmp_path / 'clean.md'
    result = subprocess.run(
        SCRIPT + CONFIG_ARG + ['--input', str(src), '--output-path', str(target)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert sorted(path.name for path in tmp_path.iterdir()) == ['clean.md', 'images.txt']
    markdown = target.read_text(encoding='utf-8')
    assert 'Caption' in markdown
    assert 'missing.png' not in markdown
    assert 'silent.png' not in markdown
    assert '![' not in markdown


def test_traditional_raw_text_is_preserved_while_markdown_is_simplified(tmp_path):
    src = tmp_path / 'traditional.txt'
    src.write_text('繁體中文與軟體', encoding='utf-8')
    code, _, stderr, bundle = _run_bundle(src, tmp_path / 'out')
    assert code == 0, stderr
    data = _load_bundle(bundle)
    node = data['content'][0]
    assert node['raw_text'] == '繁體中文與軟體'
    assert node['text'] == '繁體中文與軟體'
    assert node['normalized_text'] == '繁体中文与软体'
    assert '繁体中文与软体' in (bundle / 'traditional.md').read_text(encoding='utf-8')


def test_language_normalization_protects_code_url_path_hash_and_formula():
    from canonical import convert_chinese
    digest = 'a' * 64
    value = f'軟體 `繁體` https://example.com/繁體 C:\\繁體\\file sha256:{digest} $變數$'
    converted = convert_chinese(value, 'simplified')
    assert converted.startswith('软体 ')
    assert '`繁體`' in converted
    assert 'https://example.com/繁體' in converted
    assert 'C:\\繁體\\file' in converted
    assert f'sha256:{digest}' in converted
    assert '$變數$' in converted


def test_same_source_produces_stable_document_and_node_ids(tmp_path):
    src = tmp_path / 'stable.txt'
    src.write_text('# Stable\n\nSame bytes', encoding='utf-8')
    first = _run_bundle(src, tmp_path / 'first')[3]
    second = _run_bundle(src, tmp_path / 'second')[3]
    left, right = _load_bundle(first), _load_bundle(second)
    assert left['document']['document_id'] == right['document']['document_id']
    assert [node['id'] for node in left['content']] == [node['id'] for node in right['content']]


def test_content_is_authoritative_order_for_table_reference(tmp_path):
    src = tmp_path / 'table.md'
    src.write_text('Before\n\n| Name | Value |\n| --- | --- |\n| A | 1 |\n\nAfter', encoding='utf-8')
    code, _, stderr, bundle = _run_bundle(src, tmp_path / 'out')
    assert code == 0, stderr
    data = _load_bundle(bundle)
    types = [node['type'] for node in data['content']]
    assert types == ['paragraph', 'table', 'paragraph']
    table_node = data['content'][1]
    assert table_node['table_id'] == data['tables'][0]['table_id']
    assert data['tables'][0]['rows'][1][1]['value'] == 1


def test_semantic_validator_rejects_dangling_reference(tmp_path):
    from canonical import CanonicalValidationError, validate_canonical
    src = tmp_path / 'source.txt'
    src.write_text('Body', encoding='utf-8')
    bundle = _run_bundle(src, tmp_path / 'out')[3]
    data = _load_bundle(bundle)
    data['content'][0].update({'type': 'table', 'table_id': 'table-0000000000000000'})
    with pytest.raises(CanonicalValidationError, match='dangling table'):
        validate_canonical(data, bundle)


def test_semantic_validator_rejects_dangling_locator_span(tmp_path):
    from canonical import CanonicalValidationError, validate_canonical

    src = tmp_path / 'source.txt'
    src.write_text('Body', encoding='utf-8')
    bundle = _run_bundle(src, tmp_path / 'out')[3]
    data = _load_bundle(bundle)
    data['content'][0]['source_locator']['spans'] = [{
        'page': 2,
        'source_unit_id': 'unit-0000000000000000',
        'bbox': [0, 0, 10, 10],
    }]

    with pytest.raises(CanonicalValidationError, match='dangling source-unit span'):
        validate_canonical(data, bundle)


def test_semantic_validator_rejects_asset_path_escape_and_hash_mismatch(tmp_path):
    from canonical import CanonicalValidationError, stable_id, validate_canonical
    src = tmp_path / 'source.txt'
    src.write_text('Body', encoding='utf-8')
    bundle = _run_bundle(src, tmp_path / 'out')[3]
    data = _load_bundle(bundle)
    document_id = data['document']['document_id']
    locator = {'source_unit_id': data['source_units'][0]['id'], 'index': 1}
    asset_id = stable_id('asset', document_id, locator, 'image', 1)
    data['assets'].append({
        'asset_id': asset_id, 'type': 'image', 'path': '../escape.png',
        'sha256': '0' * 64, 'media_type': 'image/png', 'source_locator': locator,
        'alt': '', 'caption': '',
    })
    data['content'].append({
        'id': stable_id('node', document_id, locator, 'image', 99), 'type': 'image',
        'source_locator': locator, 'asset_id': asset_id,
    })
    with pytest.raises(CanonicalValidationError, match='escapes bundle'):
        validate_canonical(data, bundle)
    asset_path = bundle / 'assets' / 'images' / 'actual.png'
    asset_path.parent.mkdir(parents=True)
    asset_path.write_bytes(b'actual')
    data['assets'][0]['path'] = 'assets/images/actual.png'
    with pytest.raises(CanonicalValidationError, match='hash mismatch'):
        validate_canonical(data, bundle)


def test_quality_status_classification():
    from canonical import quality_from_warnings
    assert quality_from_warnings([]) == 'complete'
    assert quality_from_warnings([{'content_loss': False}]) == 'complete_with_warnings'
    assert quality_from_warnings([{'content_loss': True}]) == 'partial'


def test_semantic_validator_rejects_quality_and_output_manifest_mismatch(tmp_path):
    from canonical import CanonicalValidationError, validate_canonical
    src = tmp_path / 'source.txt'
    src.write_text('Body', encoding='utf-8')
    bundle = _run_bundle(src, tmp_path / 'out')[3]
    data = _load_bundle(bundle)
    data['quality']['status'] = 'partial'
    with pytest.raises(CanonicalValidationError, match='quality status'):
        validate_canonical(data, bundle)
    data = _load_bundle(bundle)
    data['outputs']['markdown']['sha256'] = '0' * 64
    with pytest.raises(CanonicalValidationError, match='Markdown hash mismatch'):
        validate_canonical(data, bundle)


def test_no_frontmatter_keeps_json_metadata(tmp_path):
    src = tmp_path / 'plain.txt'
    src.write_text('Body', encoding='utf-8')
    code, _, stderr, bundle = _run_bundle(src, tmp_path / 'out', ['--no-frontmatter', '--timestamp', '2026-08-02'])
    assert code == 0, stderr
    assert not (bundle / 'plain.md').read_text(encoding='utf-8').startswith('---')
    assert _load_bundle(bundle)['document']['conversion_timestamp'] == '2026-08-02'


def test_bundle_rename_uses_deterministic_suffix_for_folder_and_files(tmp_path):
    src = tmp_path / 'report.txt'
    src.write_text('Body', encoding='utf-8')
    output = tmp_path / 'out'
    assert _run_bundle(src, output)[0] == 0
    code, _, stderr, _ = _run_bundle(src, output, ['--rename'])
    assert code == 0, stderr
    renamed = output / 'report_1'
    assert (renamed / 'report_1.json').exists()
    assert (renamed / 'report_1.md').exists()


def test_collision_rename_preserves_dotted_logical_stem_in_both_modes(tmp_path):
    stem = '10. FF004 - Lynk Pharmaceuticals Co. Ltd'
    src = tmp_path / f'{stem}.txt'
    src.write_text('Body', encoding='utf-8')

    bundle_output = tmp_path / 'bundles'
    assert _run_bundle(src, bundle_output)[0] == 0
    code, _, stderr, _ = _run_bundle(src, bundle_output, ['--rename'])
    assert code == 0, stderr
    renamed_bundle = bundle_output / f'{stem}_1'
    assert (renamed_bundle / f'{stem}_1.json').exists()
    assert (renamed_bundle / f'{stem}_1.md').exists()

    markdown_output = tmp_path / 'markdown'
    markdown_args = SCRIPT + CONFIG_ARG + [
        '--input', str(src),
        '--output-dir', str(markdown_output),
        '--output-mode', 'markdown',
    ]
    first = subprocess.run(markdown_args, capture_output=True)
    assert first.returncode == 0, first.stderr.decode(errors='replace')
    renamed = subprocess.run(markdown_args + ['--rename'], capture_output=True)
    assert renamed.returncode == 0, renamed.stderr.decode(errors='replace')
    assert (markdown_output / f'{stem}.md').exists()
    assert (markdown_output / f'{stem}_1.md').exists()


def test_default_batch_uses_converted_and_does_not_reprocess_outputs(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    (source / 'one.txt').write_text('One', encoding='utf-8')
    args = SCRIPT + CONFIG_ARG + ['--input-dir', str(source)]
    first = subprocess.run(args, capture_output=True)
    assert first.returncode == 0, first.stderr.decode(errors='replace')
    assert (source / '_converted' / 'one' / 'one.json').exists()
    second = subprocess.run(args, capture_output=True)
    assert second.returncode == 2
    assert not (source / '_converted' / '_converted').exists()


def test_collision_is_detected_before_adapter_or_normalizer(tmp_path, monkeypatch):
    from argparse import Namespace
    import pipeline
    src = tmp_path / 'early.txt'
    src.write_text('Body', encoding='utf-8')
    (tmp_path / 'early').mkdir()
    args = Namespace(
        input=str(src), input_dir=None, output_path='', output_dir='', output_mode='bundle',
        overwrite=False, rename=False, timestamp='2026-08-02', language_normalization='simplified',
        no_frontmatter=False,
    )
    monkeypatch.setattr(pipeline, '_build_document', lambda *a, **k: pytest.fail('adapter was called'))
    with pytest.raises(pipeline.OutputCollision):
        pipeline.convert_one(args, str(src))


def test_markitdown_instance_is_reused():
    import adapters
    adapters._MARKITDOWN = None
    assert adapters.get_markitdown() is adapters.get_markitdown()


def test_opencc_runtime_uses_one_conversion_pass(monkeypatch):
    import canonical
    calls = []

    class FakeConverter:
        def convert(self, value):
            calls.append(value)
            return value

    monkeypatch.setattr(canonical, '_opencc_converter', lambda profile: FakeConverter())
    canonical.convert_chinese('內容', 'simplified')
    assert len(calls) == 1


def test_many_canonical_records_share_one_opencc_pass(monkeypatch):
    import canonical
    calls = []

    def fake_convert(value, mode):
        calls.append((value, mode))
        return value

    monkeypatch.setattr(canonical, 'convert_chinese', fake_convert)
    nodes = [
        {'type': 'paragraph', 'text': f'內容 {index}', 'normalized_text': ''}
        for index in range(100)
    ]
    canonical.normalize_canonical_text(nodes, [], 'simplified')
    assert len(calls) == 1
    assert [node['normalized_text'] for node in nodes] == [f'內容 {index}' for index in range(100)]


def test_ooxml_feature_preflight_is_lightweight_and_nonblocking(tmp_path):
    import zipfile
    from adapters import inspect_ooxml_features
    package = tmp_path / 'features.docx'
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document><w:ins/><w:del/></w:document>')
        archive.writestr('word/comments.xml', '<w:comments/>')
        archive.writestr('word/media/image1.png', b'png')
    warnings = inspect_ooxml_features(package, 'unit-0000000000000000')
    codes = {item['code'] for item in warnings}
    assert codes == {
        'office_comments_not_preserved',
        'office_tracked_changes_not_preserved',
    }
    assert all(item['content_loss'] for item in warnings)


def test_extract_ooxml_images_deduplicates_asset_and_preserves_occurrences(tmp_path):
    import zipfile
    from ooxml_images import extract_ooxml_images

    source = tmp_path / 'reused.docx'
    document_xml = '''
        <w:document xmlns:w="urn:w" xmlns:wp="urn:wp" xmlns:a="urn:a"
                    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <w:body>
            <wp:inline><wp:docPr name="Logo" descr="Company logo"/><a:blip r:embed="rId1"/></wp:inline>
            <wp:inline><wp:docPr name="Logo" descr="Company logo"/><a:blip r:embed="rId1"/></wp:inline>
          </w:body>
        </w:document>
    '''
    relationships = '''
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="image" Target="media/image1.png"/>
        </Relationships>
    '''
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('word/document.xml', document_xml)
        archive.writestr('word/_rels/document.xml.rels', relationships)
        archive.writestr('word/media/image1.png', b'\x89PNG\r\n\x1a\nimage-data')

    assets, occurrences, warnings = extract_ooxml_images(
        source,
        'sha256:' + ('1' * 64),
        'unit-0000000000000000',
        tmp_path / 'assets' / 'images',
    )

    assert warnings == []
    assert len(assets) == 1
    assert occurrences == [assets[0]['asset_id'], assets[0]['asset_id']]
    assert assets[0]['alt'] == 'Company logo'
    assert assets[0]['path'].startswith('assets/images/')
    assert (tmp_path / Path(assets[0]['path'])).is_file()


def test_extract_ooxml_images_warns_without_creating_dangling_asset(tmp_path):
    import zipfile
    from ooxml_images import extract_ooxml_images

    source = tmp_path / 'missing.docx'
    document_xml = '''
        <w:document xmlns:w="urn:w" xmlns:a="urn:a"
                    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <w:body><a:blip r:embed="rId1"/></w:body>
        </w:document>
    '''
    relationships = '''
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="image" Target="media/missing.png"/>
        </Relationships>
    '''
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('word/document.xml', document_xml)
        archive.writestr('word/_rels/document.xml.rels', relationships)

    assets, occurrences, warnings = extract_ooxml_images(
        source,
        'sha256:' + ('2' * 64),
        'unit-0000000000000000',
        tmp_path / 'assets' / 'images',
    )

    assert assets == []
    assert occurrences == []
    assert [item['code'] for item in warnings] == ['office_image_target_missing']
    assert warnings[0]['content_loss'] is True
    assert not (tmp_path / 'assets').exists()


def test_extract_ooxml_images_rejects_unsupported_media_type(tmp_path):
    import zipfile
    from ooxml_images import extract_ooxml_images

    source = tmp_path / 'unsupported.docx'
    document_xml = '''
        <w:document xmlns:w="urn:w" xmlns:a="urn:a"
                    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <w:body><a:blip r:embed="rId1"/></w:body>
        </w:document>
    '''
    relationships = '''
        <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="image" Target="media/image1.bin"/>
        </Relationships>
    '''
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('word/document.xml', document_xml)
        archive.writestr('word/_rels/document.xml.rels', relationships)
        archive.writestr('word/media/image1.bin', b'not-a-supported-image')

    assets, occurrences, warnings = extract_ooxml_images(
        source,
        'sha256:' + ('3' * 64),
        'unit-0000000000000000',
        tmp_path / 'assets' / 'images',
    )

    assert assets == []
    assert occurrences == []
    assert [item['code'] for item in warnings] == ['office_image_media_type_unsupported']
    assert warnings[0]['content_loss'] is True
    assert not (tmp_path / 'assets').exists()


def _make_pdf(path, draw):
    from reportlab.pdfgen import canvas
    document = canvas.Canvas(str(path), pagesize=(612, 792))
    draw(document)
    document.save()


def _pdf_layout_line(text, y, font_size=10, left=50, right=300, cells=None):
    bbox = [left, y, right, y + 10]
    return {
        'text': text,
        'bbox': bbox,
        'layout_bbox': bbox,
        'font_size': font_size,
        'font_weight': 400,
        'cells': cells or [{
            'text': text,
            'bbox': bbox,
            'layout_bbox': bbox,
        }],
    }


def test_native_pdf_adapter_emits_page_locators_and_clean_markdown(tmp_path):
    pdf = tmp_path / 'native.pdf'

    def draw(c):
        c.setFont('Helvetica-Bold', 18)
        c.drawString(72, 740, 'Native PDF')
        c.setFont('Helvetica', 10)
        c.drawString(72, 710, 'First paragraph line')
        c.drawString(72, 698, 'continues here.')

    _make_pdf(pdf, draw)
    code, _, stderr, bundle = _run_bundle(pdf, tmp_path / 'out')
    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert data['adapter']['name'] == 'pdfium'
    assert data['source_units'][0]['locator']['page'] == 1
    assert all('bbox' in node['source_locator'] for node in data['content'])
    markdown = (bundle / 'native.md').read_text(encoding='utf-8')
    assert 'block:' not in markdown
    assert 'Native PDF' in markdown


def test_native_pdf_two_columns_are_not_interleaved(tmp_path):
    pdf = tmp_path / 'columns.pdf'

    def draw(c):
        c.setFont('Helvetica', 10)
        for index, y in enumerate((720, 700, 680), start=1):
            c.drawString(60, y, f'L{index} left column')
            c.drawString(340, y, f'R{index} right column')

    _make_pdf(pdf, draw)
    bundle = _run_bundle(pdf, tmp_path / 'out')[3]
    markdown = (bundle / 'columns.md').read_text(encoding='utf-8')
    positions = [markdown.index(label) for label in ('L1', 'L2', 'L3', 'R1', 'R2', 'R3')]
    assert positions == sorted(positions)
    data = _load_bundle(bundle)
    assert any(warning['code'] == 'multi_column_order_inferred' for warning in data['quality']['warnings'])


def test_pdf_line_joining_handles_cjk_hyphen_indent_and_list_continuation():
    from pdf_adapter import _classify_blocks, _join_lines
    assert _join_lines(['這是第一行', '接續內容']) == '這是第一行接續內容'
    assert _join_lines(['inter-', 'national']) == 'international'

    def line(text, left, bottom, top):
        return {
            'text': text, 'bbox': [left, bottom, left + 100, top],
            'layout_bbox': [left, bottom, left + 100, top],
            'font_size': 10, 'font_weight': 400,
            'cells': [{'text': text, 'bbox': [left, bottom, left + 100, top], 'layout_bbox': [left, bottom, left + 100, top]}],
        }

    indented = _classify_blocks([line('First paragraph', 50, 700, 710), line('New paragraph', 90, 688, 698)], 1)
    assert len(indented) == 2
    listed = _classify_blocks([line('- List item', 50, 700, 710), line('continued text', 70, 688, 698)], 1)
    assert len(listed) == 1 and listed[0]['type'] == 'list_item'


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('Revenue increased\nduring the period.', 'Revenue increased during the period.'),
        ('这是第一行\n接续内容。', '这是第一行接续内容。'),
        ('The inter-\nnational market grew.', 'The international market grew.'),
        ('First sentence.\nSecond sentence.', 'First sentence. Second sentence.'),
    ],
)
def test_pdf_join_lines_normalizes_pdfium_physical_breaks(source, expected):
    from pdf_adapter import _join_lines

    actual = _join_lines([source])

    assert actual == expected
    assert not any(marker in actual for marker in ('\x02', '\r', '\n'))


def test_pdf_physical_break_does_not_invent_hyphen_across_fragments():
    from pdf_adapter import _join_lines, _merge_fragments

    assert _join_lines(['Revenue\n', 'growth']) == 'Revenue growth'
    fragments = [
        {
            'text': 'Revenue\n', 'bbox': [0, 0, 42, 10],
            'layout_bbox': [0, 0, 42, 10], 'char_width': 6,
        },
        {
            'text': 'growth', 'bbox': [48, 0, 84, 10],
            'layout_bbox': [48, 0, 84, 10], 'char_width': 6,
        },
    ]

    assert _merge_fragments(fragments) == 'Revenue growth'


def test_pdf_unmapped_glyph_resolution_only_treats_hyphen_shape_as_hyphen():
    from pdf_adapter import _join_lines, _resolve_unmapped_glyphs

    fragment = {
        'text': 'Organi\x02', 'font_size': 10, 'text_angle': 0,
        'font_weight': 400, 'bbox': [0, 0, 40, 10], 'char_width': 5,
    }
    hyphen_characters = [
        *[{'text': character, 'bbox': [index * 5, 0, index * 5 + 4, 7]} for index, character in enumerate('Organi')],
        {'text': '\x02', 'bbox': [30, 3, 33, 3.7]},
    ]
    resolved, characters, unresolved = _resolve_unmapped_glyphs(fragment, hyphen_characters)

    assert resolved['text'] == 'Organi-'
    assert characters[-1]['text'] == '-'
    assert unresolved == 0
    assert _join_lines([resolved['text'], 'zation.']) == 'Organization.'

    square_characters = [*hyphen_characters[:-1], {'text': '\x02', 'bbox': [30, 0, 37, 7]}]
    resolved, characters, unresolved = _resolve_unmapped_glyphs(fragment, square_characters)
    assert resolved['text'] == 'Organi�'
    assert characters[-1]['text'] == '�'
    assert unresolved == 1


def test_pdf_dewrap_preserves_lexical_hyphen():
    from pdf_adapter import _join_lines

    assert _join_lines(['A state-of-', 'the-art system.']) == 'A state-of-the-art system.'
    assert _join_lines(['The inter-', 'national market grew.']) == 'The international market grew.'
    assert _join_lines(['A cost-', 'effective design.']) == 'A cost-effective design.'


def test_pdf_merge_fragments_removes_boundary_newline():
    from pdf_adapter import _merge_fragments

    fragments = [
        {
            'text': 'Hello\n', 'bbox': [0, 0, 30, 10],
            'layout_bbox': [0, 0, 30, 10], 'char_width': 6,
        },
        {
            'text': 'world', 'bbox': [36, 0, 66, 10],
            'layout_bbox': [36, 0, 66, 10], 'char_width': 6,
        },
    ]

    assert _merge_fragments(fragments) == 'Hello world'


def test_native_pdf_multiline_text_object_removes_physical_break_markers(tmp_path):
    pdf = tmp_path / 'physical-wrap.pdf'

    def draw(c):
        text = c.beginText(72, 720)
        text.setFont('Helvetica', 10)
        text.textLine('Revenue increased')
        text.textLine('during the period.')
        text.textLine('The inter-')
        text.textLine('national market grew.')
        c.drawText(text)

    _make_pdf(pdf, draw)
    code, _, stderr, bundle = _run_bundle(pdf, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    paragraphs = [node['text'] for node in data['content'] if node['type'] == 'paragraph']
    assert paragraphs == ['Revenue increased during the period. The international market grew.']
    assert '\x02' not in (bundle / 'physical-wrap.md').read_text(encoding='utf-8')


def test_pdf_dewrap_preserves_hard_paragraph_gap(tmp_path):
    source = tmp_path / 'hard-paragraph-gap.pdf'

    def draw(c):
        text = c.beginText(72, 720)
        text.setFont('Helvetica', 10)
        text.textLine('First paragraph continues')
        text.textLine('on its second line.')
        text.textLine('')
        text.textLine('Second paragraph starts here.')
        c.drawText(text)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    paragraphs = [
        node['text'] for node in _load_bundle(bundle)['content']
        if node['type'] == 'paragraph'
    ]
    assert paragraphs == [
        'First paragraph continues on its second line.',
        'Second paragraph starts here.',
    ]


def test_pdf_numbered_large_label_is_heading_before_list_detection():
    from pdf_adapter import _classify_blocks

    blocks = _classify_blocks(
        [
            _pdf_layout_line('1. Executive Summary', y=730, font_size=18),
            _pdf_layout_line('Body paragraph one.', y=700, font_size=10),
            _pdf_layout_line('Body paragraph two.', y=688, font_size=10),
        ],
        page_number=1,
    )

    assert blocks[0]['type'] == 'heading'
    assert blocks[0]['level'] == 1
    assert blocks[0]['text'] == '1. Executive Summary'

    ordinary_list = _classify_blocks(
        [
            _pdf_layout_line('1. First item', y=730, font_size=10),
            _pdf_layout_line('Ordinary body text.', y=700, font_size=10),
        ],
        page_number=1,
    )
    assert ordinary_list[0]['type'] == 'list_item'


def test_document_body_size_uses_character_weighted_typography():
    from pdf_adapter import _document_body_size

    cover = [[_pdf_layout_line('Large Cover Title', y=700, font_size=28)]]
    body = [[
        _pdf_layout_line('A sufficiently long body paragraph for weighting.', y=700, font_size=10),
        _pdf_layout_line('Another sufficiently long body paragraph.', y=680, font_size=10),
    ]]

    assert _document_body_size(cover + body) == pytest.approx(10.0)


def test_document_typography_falls_back_to_line_height_when_font_size_is_degenerate():
    from pdf_adapter import _classify_blocks

    heading = _pdf_layout_line('Executive Summary', y=730, font_size=1)
    heading['bbox'][3] = 752
    heading['layout_bbox'][3] = 752
    body_one = _pdf_layout_line('Ordinary body paragraph one.', y=700, font_size=1)
    body_two = _pdf_layout_line('Ordinary body paragraph two.', y=686, font_size=1)

    blocks = _classify_blocks(
        [heading, body_one, body_two],
        page_number=1,
        document_body_size=1,
        document_body_height=10,
    )

    assert blocks[0]['type'] == 'heading'
    assert blocks[0]['level'] == 1
    assert blocks[1]['type'] == 'paragraph'


def test_document_typography_height_fallback_ignores_subscripts_and_rotated_text():
    from pdf_adapter import _classify_blocks

    heading = _pdf_layout_line('Executive Summary', y=730, font_size=1)
    heading['bbox'][3] = heading['layout_bbox'][3] = 752
    subscript = _pdf_layout_line('2', y=718, font_size=0.7, left=280, right=285)
    rotated = _pdf_layout_line('CHART LABEL', y=620, font_size=1)
    rotated['bbox'][3] = rotated['layout_bbox'][3] = 700
    rotated['_layout_horizontal'] = False
    body = _pdf_layout_line('Ordinary body paragraph.', y=590, font_size=1)

    blocks = _classify_blocks(
        [heading, subscript, rotated, body],
        page_number=1,
        document_body_size=1,
        document_body_height=10,
        document_has_heading_size_signal=False,
    )

    assert blocks[0]['type'] == 'heading'
    rotated_block = next(block for block in blocks if 'CHART LABEL' in block['text'])
    assert rotated_block['type'] == 'paragraph'


def test_pdf_cjk_indent_does_not_force_independent_paragraphs_together():
    from pdf_adapter import _classify_blocks

    blocks = _classify_blocks(
        [
            _pdf_layout_line('第一段没有句号', y=700, left=50),
            _pdf_layout_line('第二段另起缩进', y=688, left=90),
        ],
        page_number=1,
        document_body_size=10,
    )

    assert [block['text'] for block in blocks] == ['第一段没有句号', '第二段另起缩进']


def test_native_pdf_full_width_title_precedes_two_columns(tmp_path):
    pdf = tmp_path / 'title-columns.pdf'

    def draw(c):
        c.setFont('Helvetica-Bold', 18)
        c.drawCentredString(306, 750, 'Annual Research Report')
        c.setFont('Helvetica', 11)
        c.drawCentredString(306, 730, 'Executive overview')
        c.setFont('Helvetica', 10)
        for index, y in enumerate((700, 680, 660, 640), start=1):
            c.drawString(60, y, f'L{index} left column')
            c.drawString(340, y, f'R{index} right column')

    _make_pdf(pdf, draw)
    code, _, stderr, bundle = _run_bundle(pdf, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert data['content'][0]['type'] == 'heading'
    assert data['content'][0]['normalized_text'] == 'Annual Research Report'
    assert data['content'][1]['normalized_text'] == 'Executive overview'
    ordered_text = '\n'.join(node.get('normalized_text', '') for node in data['content'])
    anchors = [
        'Annual Research Report', 'Executive overview',
        'L1 left column', 'L2 left column', 'L3 left column', 'L4 left column',
        'R1 right column', 'R2 right column', 'R3 right column', 'R4 right column',
    ]
    assert all(ordered_text.count(anchor) == 1 for anchor in anchors)
    assert [ordered_text.index(anchor) for anchor in anchors] == sorted(ordered_text.index(anchor) for anchor in anchors)
    assert any(item['code'] == 'multi_column_order_inferred' for item in data['quality']['warnings'])


def test_ocr_required_page_publishes_partial_bundle(tmp_path):
    pdf = tmp_path / 'partial.pdf'

    def draw(c):
        c.drawString(72, 720, 'Usable page')
        c.showPage()
        c.rect(72, 600, 200, 100, stroke=1, fill=0)

    _make_pdf(pdf, draw)
    code, stdout, stderr, bundle = _run_bundle(pdf, tmp_path / 'out')
    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert data['quality']['status'] == 'partial'
    assert data['source_units'][1]['status'] == 'ocr_required'
    assert '[PARTIAL]' in stdout


def test_pdf_without_any_usable_content_fails_without_publication(tmp_path):
    pdf = tmp_path / 'blank.pdf'
    _make_pdf(pdf, lambda c: c.showPage())
    code, _, stderr, bundle = _run_bundle(pdf, tmp_path / 'out')
    assert code == 1
    assert 'no usable content' in stderr
    assert not bundle.exists()


def test_pdf_embedded_image_is_published_and_referenced(tmp_path):
    from PIL import Image
    image = tmp_path / 'pixel.png'
    Image.new('RGB', (20, 20), 'red').save(image)
    pdf = tmp_path / 'picture.pdf'

    def draw(c):
        c.drawString(72, 720, 'Before image')
        c.drawImage(str(image), 72, 650, width=40, height=40)
        c.drawString(72, 620, 'After image')

    _make_pdf(pdf, draw)
    code, _, stderr, bundle = _run_bundle(pdf, tmp_path / 'out')
    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['assets']) == 1
    assert any(node['type'] == 'image' and node['asset_id'] == data['assets'][0]['asset_id'] for node in data['content'])
    assert [node['type'] for node in data['content']] == ['paragraph', 'image', 'paragraph']
    asset = bundle / data['assets'][0]['path']
    assert asset.exists()
    import hashlib
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == data['assets'][0]['sha256']


def test_table_renderer_uses_null_confidence_without_inventing_headers():
    from canonical import render_table
    table = {
        'confidence': None,
        'rows': [
            [{'raw_text': 'A', 'text': 'A', 'normalized_text': 'A', 'rowspan': 1, 'colspan': 1}],
            [
                {'raw_text': 'B', 'text': 'B', 'normalized_text': 'B', 'rowspan': 1, 'colspan': 1},
                {'raw_text': '2', 'text': '2', 'normalized_text': '2', 'rowspan': 1, 'colspan': 1},
            ],
        ],
    }
    rendered = render_table(table)
    assert rendered == 'A\nB | 2'
    assert 'header' not in rendered.lower()


def test_bundle_replace_failure_rolls_back_previous_target(tmp_path, monkeypatch):
    import pipeline
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'old.txt').write_text('old', encoding='utf-8')
    stage = tmp_path / 'stage'
    stage.mkdir()
    (stage / 'new.txt').write_text('new', encoding='utf-8')
    real_replace = os.replace
    calls = []

    def flaky(source, destination):
        calls.append((Path(source), Path(destination)))
        if len(calls) == 2:
            raise OSError('simulated replace failure')
        return real_replace(source, destination)

    monkeypatch.setattr(pipeline.os, 'replace', flaky)
    with pytest.raises(OSError, match='simulated'):
        pipeline._publish_directory(stage, target, True)
    assert (target / 'old.txt').read_text(encoding='utf-8') == 'old'
    assert not (target / 'new.txt').exists()


def test_bundle_overwrite_replaces_regular_file_target(tmp_path):
    import pipeline
    target = tmp_path / 'target'
    target.write_text('old file', encoding='utf-8')
    stage = tmp_path / 'stage'
    stage.mkdir()
    (stage / 'new.txt').write_text('new', encoding='utf-8')

    pipeline._publish_directory(stage, target, True)

    assert target.is_dir()
    assert (target / 'new.txt').read_text(encoding='utf-8') == 'new'
    assert not list(tmp_path.glob('.target.backup-*'))


def test_bundle_backup_cleanup_failure_is_nonfatal_after_commit(tmp_path, monkeypatch, capsys):
    import pipeline
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'old.txt').write_text('old', encoding='utf-8')
    stage = tmp_path / 'stage'
    stage.mkdir()
    (stage / 'new.txt').write_text('new', encoding='utf-8')
    real_remove_path = pipeline._remove_path

    def fail_backup_cleanup(path):
        if '.target.backup-' in Path(path).name:
            raise PermissionError('simulated cleanup failure')
        return real_remove_path(path)

    monkeypatch.setattr(pipeline, '_remove_path', fail_backup_cleanup)

    pipeline._publish_directory(stage, target, True)

    assert (target / 'new.txt').read_text(encoding='utf-8') == 'new'
    assert not (target / 'old.txt').exists()
    backups = list(tmp_path.glob('.target.backup-*'))
    assert len(backups) == 1
    assert (backups[0] / 'old.txt').read_text(encoding='utf-8') == 'old'
    stderr = capsys.readouterr().err
    assert 'published' in stderr
    assert 'could not remove backup' in stderr


def _assert_single_office_bundle_image(bundle):
    import hashlib

    data = _load_bundle(bundle)
    assert len(data['assets']) == 1
    asset = data['assets'][0]
    assert asset['type'] == 'image'
    assert asset['path'].startswith('assets/images/')
    assert set(asset) >= {
        'asset_id', 'type', 'path', 'sha256', 'media_type',
        'source_locator', 'alt', 'caption',
    }
    published = bundle / Path(asset['path'])
    assert published.is_file()
    assert hashlib.sha256(published.read_bytes()).hexdigest() == asset['sha256']
    assert data['outputs']['assets'] == [{'path': asset['path'], 'sha256': asset['sha256']}]
    assert any(node['type'] == 'image' and node['asset_id'] == asset['asset_id'] for node in data['content'])
    markdown = (bundle / f'{bundle.name}.md').read_text(encoding='utf-8')
    assert f']({asset["path"]})' in markdown
    assert 'data:image/' not in markdown
    return data, asset, markdown


def test_docx_bundle_exports_embedded_image_to_json_and_markdown(tmp_path):
    from docx import Document
    from PIL import Image
    picture = tmp_path / 'office.png'
    Image.new('RGB', (16, 16), 'blue').save(picture)
    source = tmp_path / 'office.docx'
    document = Document()
    document.add_paragraph('Before Office image')
    document.add_picture(str(picture))
    document.add_paragraph('After Office image')
    document.save(source)
    code, stdout, stderr, bundle = _run_bundle(source, tmp_path / 'out')
    assert code == 0, stderr
    data, _, markdown = _assert_single_office_bundle_image(bundle)
    assert data['quality']['status'] == 'complete'
    assert not any(item['code'] == 'office_embedded_images_not_exported' for item in data['quality']['warnings'])
    significant = [
        node['type'] for node in data['content']
        if node['type'] != 'paragraph' or node.get('normalized_text', '').strip()
    ]
    assert significant == ['paragraph', 'image', 'paragraph']
    assert markdown.index('Before Office image') < markdown.index('](') < markdown.index('After Office image')
    assert '[PARTIAL]' not in stdout


def test_pptx_bundle_exports_embedded_image_to_json_and_markdown(tmp_path):
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    picture = tmp_path / 'slide.png'
    Image.new('RGB', (16, 16), 'green').save(picture)
    source = tmp_path / 'slides.pptx'
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1)).text = 'Slide text'
    slide.shapes.add_picture(str(picture), Inches(1), Inches(2))
    presentation.save(source)

    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')
    assert code == 0, stderr
    data, _, markdown = _assert_single_office_bundle_image(bundle)
    assert 'Slide text' in markdown
    assert data['quality']['status'] == 'complete'


def test_xlsx_bundle_exports_image_with_inferred_position_warning(tmp_path):
    from PIL import Image
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as SpreadsheetImage

    picture = tmp_path / 'sheet.png'
    Image.new('RGB', (16, 16), 'purple').save(picture)
    source = tmp_path / 'workbook.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet['A1'] = 'Sheet text'
    sheet.add_image(SpreadsheetImage(str(picture)), 'A3')
    workbook.save(source)

    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')
    assert code == 0, stderr
    data, _, markdown = _assert_single_office_bundle_image(bundle)
    assert 'Sheet text' in markdown
    assert data['quality']['status'] == 'complete_with_warnings'
    assert any(item['code'] == 'office_image_position_inferred' for item in data['quality']['warnings'])


def test_docx_markdown_only_intentionally_omits_image_assets_and_links(tmp_path):
    from docx import Document
    from PIL import Image

    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    picture = inputs / 'office.png'
    Image.new('RGB', (16, 16), 'blue').save(picture)
    source = inputs / 'office.docx'
    document = Document()
    document.add_paragraph('Before')
    document.add_picture(str(picture))
    document.add_paragraph('After')
    document.save(source)
    output = tmp_path / 'output' / 'clean.md'

    result = subprocess.run(
        SCRIPT + CONFIG_ARG + ['--input', str(source), '--output-path', str(output)],
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert sorted(path.name for path in output.parent.iterdir()) == ['clean.md']
    markdown = output.read_text(encoding='utf-8')
    assert 'Before' in markdown and 'After' in markdown
    assert 'assets/images/' not in markdown
    assert 'data:image/' not in markdown
    assert '[PARTIAL]' not in result.stdout.decode(errors='replace')


def test_docx_missing_embedded_image_publishes_text_without_dangling_link(tmp_path):
    import zipfile
    from docx import Document
    from PIL import Image

    picture = tmp_path / 'missing.png'
    Image.new('RGB', (16, 16), 'red').save(picture)
    source = tmp_path / 'broken-image.docx'
    document = Document()
    document.add_paragraph('Before missing image')
    document.add_picture(str(picture))
    document.add_paragraph('After missing image')
    document.save(source)
    rewritten = tmp_path / 'rewritten.docx'
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(rewritten, 'w') as replacement:
        for item in original.infolist():
            if '/media/' not in item.filename:
                replacement.writestr(item, original.read(item.filename))
    rewritten.replace(source)

    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert data['assets'] == []
    assert data['outputs']['assets'] == []
    assert data['quality']['status'] == 'partial'
    assert any(item['code'] == 'office_image_target_missing' for item in data['quality']['warnings'])
    markdown = (bundle / 'broken-image.md').read_text(encoding='utf-8')
    assert 'Before missing image' in markdown and 'After missing image' in markdown
    assert 'assets/images/' not in markdown
    assert 'data:image/' not in markdown


def test_pdf_fake_ocr_provider_contract_can_supply_nodes(tmp_path):
    from canonical import sha256_file
    from pdf_adapter import PdfAdapter
    source = tmp_path / 'ocr.pdf'

    def draw(c):
        c.rect(72, 600, 200, 100, stroke=1, fill=0)

    _make_pdf(source, draw)

    class FakeOcr:
        name = 'fake-ocr'

        def extract(self, page, page_number):
            return [{
                'type': 'paragraph', 'text': 'Recovered OCR text',
                'bbox': [72.0, 600.0, 272.0, 700.0],
                'layout_bbox': [72.0, 600.0, 272.0, 700.0],
            }]

    digest = sha256_file(source)
    result = PdfAdapter(FakeOcr()).extract(str(source), f'sha256:{digest}', 'simplified')
    assert result['content'][0]['normalized_text'] == 'Recovered OCR text'
    assert result['source_units'][0]['status'] == 'warning'
    assert any(item['code'] == 'ocr_applied' for item in result['warnings'])


def test_pdf_ocr_off_never_calls_provider(tmp_path):
    from canonical import sha256_file
    from pdf_adapter import PdfAdapter

    source = tmp_path / 'ocr-off.pdf'
    _make_pdf(source, lambda c: c.rect(72, 600, 200, 100, stroke=1, fill=0))

    class RecordingOcr:
        name = 'recording-ocr'

        def __init__(self):
            self.calls = []

        def extract(self, _page, page_number):
            self.calls.append(page_number)
            return []

    provider = RecordingOcr()
    digest = sha256_file(source)
    result = PdfAdapter(provider, ocr_mode='off').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    assert provider.calls == []
    assert result['source_units'][0]['status'] == 'ocr_required'


def test_pdf_ocr_auto_routing_ignores_small_logo_but_selects_dominant_scan():
    from pdf_adapter import _analyze_ocr_need

    healthy = _analyze_ocr_need(
        'auto',
        'A healthy born-digital paragraph with enough native text to trust.',
        [{'text': 'healthy'}],
        [[500, 740, 560, 780]],
        1,
        612,
        792,
        [],
    )
    scanned = _analyze_ocr_need(
        'auto',
        'Page 1',
        [{'text': 'Page 1'}],
        [[20, 20, 592, 772]],
        1,
        612,
        792,
        [],
    )

    assert healthy['should_run'] is False
    assert scanned['should_run'] is True
    assert 'sparse_text_with_dominant_image' in scanned['reasons']


def test_pdf_ocr_auto_skips_native_page_and_recovers_scan_page(tmp_path):
    from canonical import sha256_file
    from pdf_adapter import PdfAdapter

    source = tmp_path / 'mixed-ocr.pdf'

    def draw(c):
        c.drawString(72, 720, 'Native first page')
        c.showPage()
        c.rect(72, 600, 200, 100, stroke=1, fill=0)

    _make_pdf(source, draw)

    class RecordingOcr:
        name = 'recording-ocr'

        def __init__(self):
            self.calls = []

        def extract(self, _page, page_number):
            self.calls.append(page_number)
            return [{
                'text': 'Recovered second page',
                'bbox': [72.0, 600.0, 272.0, 700.0],
                'confidence': 0.96,
            }]

    provider = RecordingOcr()
    digest = sha256_file(source)
    result = PdfAdapter(provider, ocr_mode='auto').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    assert provider.calls == [2]
    text = '\n'.join(node.get('normalized_text', '') for node in result['content'])
    assert text.index('Native first page') < text.index('Recovered second page')
    assert result['source_units'][0]['status'] == 'complete'
    assert result['source_units'][1]['status'] == 'warning'
    assert any(item['code'] == 'ocr_applied' for item in result['warnings'])
    recovered = next(node for node in result['content'] if 'Recovered second page' in node.get('text', ''))
    assert recovered['source_locator']['extraction_method'] == 'ocr'
    assert recovered['source_locator']['ocr_provider'] == 'recording-ocr'
    assert recovered['source_locator']['ocr_confidence'] == pytest.approx(0.96)


def test_pdf_ocr_records_reproducibility_provenance_without_changing_stable_ids(tmp_path):
    from canonical import sha256_file
    from ocr_provider import OcrPageResult, OcrSettings, OcrSpan
    from pdf_adapter import PdfAdapter

    source = tmp_path / 'ocr-provenance.pdf'
    _make_pdf(source, lambda c: c.rect(72, 600, 200, 100, stroke=1, fill=0))

    class ProvenanceOcr:
        name = 'rapidocr'

        def __init__(self, version, dpi):
            self.version = version
            self.settings = OcrSettings(mode='auto', language='ch', dpi=dpi)

        def extract(self, _page, page_number):
            polygon = ((72.0, 600.0), (272.0, 600.0), (272.0, 625.0), (72.0, 625.0))
            return OcrPageResult(
                page_number=page_number,
                engine='rapidocr',
                engine_version=self.version,
                runtime='onnxruntime',
                runtime_version='1.20.1',
                model_profile='PP-OCRv6-small',
                language='ch',
                min_confidence=0.5,
                spans=(OcrSpan('Auditable OCR text', 0.98, polygon, (72.0, 600.0, 272.0, 625.0)),),
                requested_dpi=self.settings.dpi,
                effective_dpi=self.settings.dpi,
                raster_width=2448,
                raster_height=3168,
            )

    digest = sha256_file(source)
    first = PdfAdapter(ProvenanceOcr('3.9.2', 300)).extract(
        str(source), f'sha256:{digest}', 'preserve'
    )
    second = PdfAdapter(ProvenanceOcr('future-version', 144)).extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    provenance = first['source_units'][0]['locator']['ocr']
    assert provenance == {
        'provider': 'rapidocr',
        'version': '3.9.2',
        'runtime': 'onnxruntime',
        'runtime_version': '1.20.1',
        'model_profile': 'PP-OCRv6-small',
        'language': 'ch',
        'requested_dpi': 300.0,
        'effective_dpi': 300.0,
        'min_confidence': 0.5,
        'raster_width': 2448,
        'raster_height': 3168,
        'usable_characters': 16,
        'dropped_low_confidence': 0,
        'dropped_invalid': 0,
        'dropped_overlap': 0,
        'replaced_native': 0,
    }
    assert first['source_units'][0]['id'] == second['source_units'][0]['id']
    assert first['content'][0]['id'] == second['content'][0]['id']


def test_pdf_ocr_required_page_keeps_partial_status_for_trivial_recovery(tmp_path):
    from canonical import sha256_file
    from pdf_adapter import PdfAdapter

    source = tmp_path / 'trivial-ocr.pdf'
    _make_pdf(source, lambda c: c.rect(72, 600, 200, 100, stroke=1, fill=0))

    class TrivialOcr:
        name = 'trivial-ocr'

        def extract(self, _page, _page_number):
            return [{'text': 'X', 'bbox': [72, 600, 90, 630], 'confidence': 0.99}]

    digest = sha256_file(source)
    result = PdfAdapter(TrivialOcr(), ocr_mode='auto').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    assert result['source_units'][0]['status'] == 'ocr_required'
    assert any(item['code'] == 'ocr_incomplete_result' for item in result['warnings'])
    assert any(item['code'] == 'ocr_required' for item in result['warnings'])


def test_pdf_ocr_overlap_does_not_drop_semantically_different_contained_line():
    from pdf_adapter import _merge_native_ocr_fragments

    native = [{
        'text': 'Page 1', 'bbox': [10, 10, 50, 20],
        'font_size': 10, 'font_weight': 400, 'char_width': 5,
    }]
    ocr = [{
        'text': 'Material financial disclosure completely different',
        'bbox': [0, 0, 500, 40],
        'font_size': 10, 'font_weight': 400, 'char_width': 5,
        '_source_method': 'ocr', '_ocr_confidence': 0.98,
    }]

    merged, dropped_ocr, dropped_native = _merge_native_ocr_fragments(
        native, ocr, native_unusable=False
    )

    assert {item['text'] for item in merged} == {
        'Page 1', 'Material financial disclosure completely different'
    }
    assert dropped_ocr == 0
    assert dropped_native == 0


def test_pdf_ocr_force_deduplicates_native_overlap_and_keeps_spatial_order(tmp_path):
    from canonical import sha256_file
    from pdf_adapter import PdfAdapter

    source = tmp_path / 'force-ocr.pdf'
    _make_pdf(source, lambda c: c.drawString(72, 720, 'Native heading'))

    class RecordingOcr:
        name = 'recording-ocr'

        def __init__(self):
            self.calls = []

        def extract(self, _page, page_number):
            self.calls.append(page_number)
            return [
                {
                    'text': 'Native heading',
                    'bbox': [70.0, 715.0, 160.0, 735.0],
                    'confidence': 0.99,
                },
                {
                    'text': 'OCR-only lower line',
                    'bbox': [72.0, 650.0, 220.0, 670.0],
                    'confidence': 0.94,
                },
            ]

    provider = RecordingOcr()
    digest = sha256_file(source)
    result = PdfAdapter(provider, ocr_mode='force').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    assert provider.calls == [1]
    text = '\n'.join(node.get('normalized_text', '') for node in result['content'])
    assert text.count('Native heading') == 1
    assert text.count('OCR-only lower line') == 1
    assert text.index('Native heading') < text.index('OCR-only lower line')


def test_pdf_ocr_failure_is_normalized_and_preserves_other_pages(tmp_path):
    from canonical import sha256_file
    from ocr_provider import OcrProviderError
    from pdf_adapter import PdfAdapter

    source = tmp_path / 'failed-ocr.pdf'

    def draw(c):
        c.drawString(72, 720, 'Usable native page')
        c.showPage()
        c.rect(72, 600, 200, 100, stroke=1, fill=0)

    _make_pdf(source, draw)

    class FailingOcr:
        name = 'failing-ocr'

        def extract(self, _page, _page_number):
            raise OcrProviderError('controlled test failure')

    digest = sha256_file(source)
    result = PdfAdapter(FailingOcr(), ocr_mode='auto').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    text = '\n'.join(node.get('normalized_text', '') for node in result['content'])
    assert 'Usable native page' in text
    assert result['source_units'][1]['status'] == 'ocr_required'
    assert any(item['code'] == 'ocr_failed' for item in result['warnings'])
    assert any(item['code'] == 'ocr_required' for item in result['warnings'])


def test_rapidocr_provider_is_lazy_reused_and_maps_bitmap_to_pdf_coordinates():
    from types import SimpleNamespace
    from PIL import Image
    from ocr_provider import OcrSettings, RapidOcrProvider

    factory_calls = []
    inference_calls = []

    class Engine:
        def __call__(self, image, **kwargs):
            inference_calls.append((image.size, kwargs))
            return SimpleNamespace(
                boxes=[
                    [[10, 10], [190, 10], [190, 30], [10, 30]],
                    [[10, 40], [190, 40], [190, 60], [10, 60]],
                ],
                txts=['Mapped OCR line', 'Low confidence line'],
                scores=[0.93, 0.2],
                elapse=0.01,
            )

    def engine_factory():
        factory_calls.append(True)
        return Engine()

    class PositionConverter:
        def to_page(self, x, y):
            return x / 2, 100 - y / 2

    class Bitmap:
        def __init__(self):
            self.closed = False

        def get_posconv(self, _page):
            return PositionConverter()

        def to_pil(self):
            return Image.new('RGB', (400, 200), 'white')

        def close(self):
            self.closed = True

    class Page:
        def __init__(self):
            self.bitmaps = []

        def get_size(self):
            return 200, 100

        def render(self, **_kwargs):
            bitmap = Bitmap()
            self.bitmaps.append(bitmap)
            return bitmap

    provider = RapidOcrProvider(
        OcrSettings(mode='auto', dpi=144, max_long_edge=4096, min_confidence=0.5),
        engine_factory=engine_factory,
    )
    assert factory_calls == []

    page = Page()
    first = provider.extract(page, 1)
    second = provider.extract(page, 2)

    assert len(factory_calls) == 1
    assert len(inference_calls) == 2
    assert first.spans[0].bbox == pytest.approx((5.0, 85.0, 95.0, 95.0))
    assert first.spans[0].confidence == pytest.approx(0.93)
    assert first.dropped_low_confidence == 1
    assert first.language == 'ch'
    assert first.requested_dpi == 144
    assert first.min_confidence == pytest.approx(0.5)
    assert first.model_profile == 'PP-OCRv6-small'
    assert first.runtime == 'injected'
    assert second.page_number == 2
    assert all(bitmap.closed for bitmap in page.bitmaps)


def test_rapidocr_provider_caches_initialization_failure():
    from ocr_provider import OcrSettings, OcrUnavailableError, RapidOcrProvider

    calls = []

    def unavailable_factory():
        calls.append(True)
        raise OcrUnavailableError('backend unavailable')

    class Page:
        def get_size(self):
            return 200, 100

    provider = RapidOcrProvider(
        OcrSettings(mode='auto'), engine_factory=unavailable_factory
    )
    for _ in range(2):
        with pytest.raises(OcrUnavailableError, match='backend unavailable'):
            provider.extract(Page(), 1)

    assert len(calls) == 1


def test_pdf_rotation_is_normalized_while_source_orientation_is_recorded(tmp_path):
    source = tmp_path / 'rotated.pdf'

    def draw(c):
        c.saveState()
        c.translate(100, 100)
        c.rotate(90)
        c.drawString(0, 0, 'Rotated text')
        c.restoreState()

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')
    assert code == 0, stderr
    data = _load_bundle(bundle)
    locator = data['source_units'][0]['locator']
    assert locator['orientation_normalized'] is True
    assert locator['dominant_text_angle'] == 90
    assert 'Rotated text' in (bundle / 'rotated.md').read_text(encoding='utf-8')


def test_cross_page_paragraphs_merge_with_locator_span(tmp_path):
    from canonical import stable_id

    source = tmp_path / 'continued.pdf'

    def draw(c):
        c.drawString(72, 72, 'This paragraph continues')
        c.showPage()
        c.drawString(72, 740, 'on the next page.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')
    assert code == 0, stderr
    data = _load_bundle(bundle)
    paragraphs = [node for node in data['content'] if node['type'] == 'paragraph']
    assert len(paragraphs) == 1
    assert paragraphs[0]['text'] == 'This paragraph continues on the next page.'
    assert paragraphs[0]['source_locator']['continued_to_page'] == 2
    assert [span['page'] for span in paragraphs[0]['source_locator']['spans']] == [1, 2]
    assert paragraphs[0]['id'] == stable_id(
        'node', data['document']['document_id'], paragraphs[0]['source_locator'], 'paragraph', 1,
    )


@pytest.mark.parametrize('blocker', ['completed', 'colon', 'middle', 'other_column'])
def test_cross_page_paragraph_merge_respects_semantic_and_geometry_blockers(tmp_path, blocker):
    source = tmp_path / f'blocked-{blocker}.pdf'

    def draw(c):
        previous = {
            'completed': 'This sentence is complete.',
            'colon': 'Key findings:',
            'middle': 'This fragment continues',
            'other_column': 'This fragment continues',
        }[blocker]
        previous_y = 400 if blocker == 'middle' else 72
        c.drawString(72, previous_y, previous)
        c.showPage()
        current_x = 340 if blocker == 'other_column' else 72
        c.drawString(current_x, 740, 'on the next page.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    paragraphs = [node for node in _load_bundle(bundle)['content'] if node['type'] == 'paragraph']
    assert len(paragraphs) == 2
    assert all('continued_to_page' not in node['source_locator'] for node in paragraphs)


def test_cross_page_paragraph_merge_rejects_repeated_page_header(tmp_path):
    source = tmp_path / 'repeated-header.pdf'

    def draw(c):
        c.drawString(72, 740, 'confidential report')
        c.drawString(72, 72, 'This paragraph continues')
        c.showPage()
        c.drawString(72, 740, 'confidential report')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    paragraphs = [node for node in data['content'] if node['type'] == 'paragraph']
    boilerplate = [node for node in data['content'] if node['type'] == 'boilerplate']
    assert [node['text'] for node in paragraphs] == ['This paragraph continues']
    assert [node['text'] for node in boilerplate] == [
        'confidential report', 'confidential report',
    ]
    markdown = (bundle / 'repeated-header.md').read_text(encoding='utf-8')
    assert 'confidential report' not in markdown


def test_cross_page_paragraph_merge_skips_classified_running_chrome(tmp_path):
    source = tmp_path / 'continued-through-chrome.pdf'

    def draw(c):
        c.drawString(72, 760, 'CONFIDENTIAL REPORT')
        c.drawString(72, 72, 'This paragraph continues')
        c.drawString(72, 25, 'Acme Research')
        c.showPage()
        c.drawString(72, 760, 'CONFIDENTIAL REPORT')
        c.drawString(72, 730, 'on the next page without truncation.')
        c.drawString(72, 25, 'Acme Research')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    paragraphs = [node for node in data['content'] if node['type'] == 'paragraph']
    assert [node['text'] for node in paragraphs] == [
        'This paragraph continues on the next page without truncation.'
    ]
    assert [span['page'] for span in paragraphs[0]['source_locator']['spans']] == [1, 2]


def test_pdf_borderless_table_keeps_text_header_and_sparse_cells(tmp_path):
    source = tmp_path / 'borderless-table.pdf'

    def draw(c):
        rows = [
            ('Item', 'Region', 'Amount'),
            ('Widget', 'North', '1,200'),
            ('Service', '', '950'),
        ]
        for y, row in zip((710, 690, 670), rows):
            if row[0]:
                c.drawString(72, y, row[0])
            if row[1]:
                c.drawString(250, y, row[1])
            if row[2]:
                c.drawRightString(500, y, row[2])

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 1
    assert data['tables'][0]['raw_rows'] == [
        ['Item', 'Region', 'Amount'],
        ['Widget', 'North', '1,200'],
        ['Service', '', '950'],
    ]
    assert [node['type'] for node in data['content']] == ['table']


def test_pdf_word_grid_splits_single_text_object_rows_and_keeps_sparse_cells(tmp_path):
    source = tmp_path / 'single-object-table.pdf'

    def draw(c):
        c.setFont('Courier', 10)
        rows = [
            ('Item', 'Region', 'Amount'),
            ('Widget', 'North', '1,200'),
            ('Service', '', '950'),
        ]
        for y, row in zip((730, 710, 690), rows):
            c.drawString(72, y, f'{row[0]:<20}{row[1]:<15}{row[2]:>10}')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 1
    assert data['tables'][0]['raw_rows'] == [
        ['Item', 'Region', 'Amount'],
        ['Widget', 'North', '1,200'],
        ['Service', '', '950'],
    ]
    assert [node['type'] for node in data['content']] == ['table']


def test_pdf_four_column_sparse_row_is_not_folded_into_previous_row(tmp_path):
    source = tmp_path / 'four-column-sparse-row.pdf'

    def draw(c):
        rows = [
            (730, ('H1', 'H2', 'H3', 'H4')),
            (710, ('A1', 'A2', 'A3', '10')),
            (690, ('B1', '', '', '20')),
            (670, ('C1', 'C2', 'C3', '30')),
        ]
        for y, row in rows:
            for x, value in zip((72, 200, 330, 480), row):
                if value:
                    c.drawString(x, y, value)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    tables = _load_bundle(bundle)['tables']
    assert len(tables) == 1
    assert tables[0]['raw_rows'] == [
        ['H1', 'H2', 'H3', 'H4'],
        ['A1', 'A2', 'A3', '10'],
        ['B1', '', '', '20'],
        ['C1', 'C2', 'C3', '30'],
    ]


def test_pdf_header_only_column_does_not_disqualify_numeric_table(tmp_path):
    source = tmp_path / 'header-only-column.pdf'

    def draw(c):
        for x, value in zip((72, 180, 290, 390, 470), ('Metric', 'Prior', 'Current', 'Rate', 'Reason')):
            c.drawString(x, 730, value)
        for y, row in (
            (710, ('Revenue', '100', '120', '20% growth')),
            (690, ('Profit', '20', '30', '50% margin')),
        ):
            for x, value in zip((72, 180, 290, 470), row):
                c.drawString(x, y, value)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    table = _load_bundle(bundle)['tables'][0]
    assert table['raw_rows'] == [
        ['Metric', 'Prior', 'Current', 'Rate', 'Reason'],
        ['Revenue', '100', '120', '', '20% growth'],
        ['Profit', '20', '30', '', '50% margin'],
    ]


def test_pdf_pure_text_label_value_table_is_not_split_into_columns(tmp_path):
    source = tmp_path / 'label-value-table.pdf'

    def draw(c):
        for y, label, value in (
            (710, 'Employee Name', 'Michael Tran'),
            (690, 'Department', 'Client Services'),
            (670, 'Manager Approver', 'Laura Simmons'),
        ):
            c.drawString(72, y, label)
            c.drawString(250, y, value)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 1
    assert data['tables'][0]['raw_rows'] == [
        ['Employee Name', 'Michael Tran'],
        ['Department', 'Client Services'],
        ['Manager Approver', 'Laura Simmons'],
    ]
    assert [node['type'] for node in data['content']] == ['table']


def test_pdf_wrapped_table_line_can_continue_multiple_cells(tmp_path):
    source = tmp_path / 'multi-cell-wrap.pdf'

    def draw(c):
        for x, text in ((72, 'Date'), (180, 'Description'), (390, 'Category')):
            c.drawString(x, 730, text)
        c.drawRightString(520, 730, 'Amount')
        for x, text in ((72, 'January 5th,'), (180, 'Taxi from airport'), (390, 'Ground')):
            c.drawString(x, 710, text)
        c.drawRightString(520, 710, '$48.00')
        c.drawString(72, 698, '2026')
        c.drawString(390, 698, 'transportation')
        for x, text in ((72, 'January 6th, 2026'), (180, 'Client dinner'), (390, 'Meals')):
            c.drawString(x, 678, text)
        c.drawRightString(520, 678, '$186.20')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    table = _load_bundle(bundle)['tables'][0]
    assert table['raw_rows'][1] == [
        'January 5th, 2026', 'Taxi from airport', 'Ground transportation', '$48.00',
    ]


def test_pdf_patterned_tail_continuation_is_kept_but_total_row_is_separate(tmp_path):
    source = tmp_path / 'tail-continuation-and-total.pdf'

    def draw(c):
        for x, text in ((72, 'Date'), (180, 'Description'), (390, 'Category')):
            c.drawString(x, 730, text)
        c.drawRightString(520, 730, 'Amount')
        for y, date, description, category, amount in (
            (710, 'January 5th,', 'Taxi', 'Ground', '$48.00'),
            (678, 'January 6th,', 'Dinner', 'Meals', '$80.00'),
        ):
            c.drawString(72, y, date)
            c.drawString(180, y, description)
            c.drawString(390, y, category)
            c.drawRightString(520, y, amount)
            c.drawString(72, y - 12, '2026')
        c.drawString(180, 640, 'Total Reimbursement')
        c.drawRightString(520, 640, '$128.00')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    expense_table = max(data['tables'], key=lambda table: len(table['raw_rows'][0]))
    assert expense_table['raw_rows'] == [
        ['Date', 'Description', 'Category', 'Amount'],
        ['January 5th, 2026', 'Taxi', 'Ground', '$48.00'],
        ['January 6th, 2026', 'Dinner', 'Meals', '$80.00'],
    ]
    assert 'Total Reimbursement' not in str(expense_table['raw_rows'])
    assert any(
        table is not expense_table and 'Total Reimbursement' in str(table['raw_rows'])
        for table in data['tables']
    )


def test_pdf_wrapped_table_cell_and_multiple_tables_preserve_boundaries(tmp_path):
    source = tmp_path / 'wrapped-and-multiple.pdf'

    def draw(c):
        c.drawString(72, 730, 'Description')
        c.drawRightString(500, 730, 'Amount')
        c.drawString(72, 710, 'Advisory services for the')
        c.drawRightString(500, 710, '1,500')
        c.drawString(72, 698, 'year ended December')
        c.drawString(72, 678, 'Software')
        c.drawRightString(500, 678, '900')
        c.drawString(72, 630, 'Narrative between tables.')
        c.drawString(72, 580, 'Category')
        c.drawRightString(500, 580, 'Count')
        c.drawString(72, 560, 'Open')
        c.drawRightString(500, 560, '7')
        c.drawString(72, 540, 'Closed')
        c.drawRightString(500, 540, '3')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 2
    assert data['tables'][0]['raw_rows'] == [
        ['Description', 'Amount'],
        ['Advisory services for the year ended December', '1,500'],
        ['Software', '900'],
    ]
    assert data['tables'][1]['raw_rows'] == [
        ['Category', 'Count'],
        ['Open', '7'],
        ['Closed', '3'],
    ]
    assert [node['type'] for node in data['content']] == ['table', 'paragraph', 'table']
    assert data['content'][1]['text'] == 'Narrative between tables.'


def test_pdf_table_does_not_absorb_trailing_source_note(tmp_path):
    source = tmp_path / 'table-with-source-note.pdf'

    def draw(c):
        for y, left, right in (
            (730, 'Item', 'Amount'),
            (710, 'Alpha', '10'),
            (690, 'Beta', '20'),
        ):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)
        c.drawString(72, 678, 'Source: company filings')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert data['tables'][0]['raw_rows'][-1] == ['Beta', '20']
    assert [node['type'] for node in data['content']] == ['table', 'paragraph']
    assert data['content'][1]['text'] == 'Source: company filings'


def test_pdf_caption_between_compatible_rows_preserves_physical_order(tmp_path):
    source = tmp_path / 'table-caption-order.pdf'

    def draw(c):
        for y, left, right in (
            (730, 'Item', 'Amount'),
            (710, 'Alpha', '10'),
        ):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)
        c.drawString(72, 698, 'Note: unaudited interim values.')
        c.drawString(72, 678, 'Beta')
        c.drawRightString(500, 678, '20')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    content = data['content']
    assert [node['type'] for node in content] == ['table', 'paragraph', 'table']
    tables = {table['table_id']: table for table in data['tables']}
    assert tables[content[0]['table_id']]['raw_rows'] == [['Item', 'Amount'], ['Alpha', '10']]
    assert content[1]['text'] == 'Note: unaudited interim values.'
    assert tables[content[2]['table_id']]['raw_rows'] == [['Beta', '20']]


def test_pdf_adjacent_same_shape_tables_with_different_headers_remain_separate(tmp_path):
    source = tmp_path / 'adjacent-independent-tables.pdf'

    def draw(c):
        for y, left, right in (
            (740, 'Item', 'Amount'), (720, 'Alpha', '10'), (700, 'Beta', '20'),
            (670, 'Metric', 'Value'), (650, 'Open', '7'), (630, 'Closed', '3'),
        ):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    tables = _load_bundle(bundle)['tables']
    assert len(tables) == 2
    assert tables[0]['raw_rows'][0] == ['Item', 'Amount']
    assert tables[1]['raw_rows'][0] == ['Metric', 'Value']


def test_pdf_cross_page_table_stitches_and_deduplicates_header(tmp_path):
    from canonical import stable_id

    source = tmp_path / 'continued-table.pdf'

    def draw(c):
        for y, left, right in (
            (110, 'Item', 'Amount'),
            (90, 'Alpha', '10'),
            (70, 'Beta', '20'),
        ):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)
        c.showPage()
        for y, left, right in (
            (740, 'Item', 'Amount'),
            (720, 'Gamma', '30'),
            (700, 'Delta', '40'),
        ):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 1
    table = data['tables'][0]
    assert table['cross_page_continuation'] is True
    assert table['raw_rows'] == [
        ['Item', 'Amount'], ['Alpha', '10'], ['Beta', '20'],
        ['Gamma', '30'], ['Delta', '40'],
    ]
    assert [span['page'] for span in table['source_locator']['spans']] == [1, 2]
    assert data['content'][0]['source_locator'] == table['source_locator']
    assert table['table_id'] == stable_id(
        'table', data['document']['document_id'], table['source_locator'], 'table', 1,
    )
    assert data['content'][0]['id'] == stable_id(
        'node', data['document']['document_id'], table['source_locator'], 'table', 1,
    )


def test_pdf_cross_page_table_stitching_skips_running_chrome_nodes():
    from pdf_adapter import _merge_cross_page_tables

    def locator(page, box):
        return {
            'source_unit_id': f'unit-{page}',
            'page': page,
            'bbox': box,
            'layout_bbox': box,
            'page_width': 612,
            'page_height': 792,
            'layout_column': 'single',
            'column_ranges': [[50, 250], [300, 550]],
            'table_detection': 'vector_grid',
            'vector_rule_count': 6,
        }

    previous_locator = locator(1, [50, 0, 550, 45])
    current_locator = locator(2, [50, 747, 550, 792])
    tables = [
        {
            'table_id': 'table-1',
            'source_locator': previous_locator,
            'raw_rows': [['Name', 'Amount'], ['Alpha', '1']],
            'rows': [['Name', 'Amount'], ['Alpha', '1']],
        },
        {
            'table_id': 'table-2',
            'source_locator': current_locator,
            'raw_rows': [['Name', 'Amount'], ['Beta', '2']],
            'rows': [['Name', 'Amount'], ['Beta', '2']],
        },
    ]
    content = [
        {'type': 'table', 'table_id': 'table-1', 'source_locator': previous_locator},
        {'type': 'boilerplate', 'text': 'Confidential', 'source_locator': {'page': 1}},
        {'type': 'page_label', 'text': '1', 'source_locator': {'page': 1}},
        {'type': 'boilerplate', 'text': 'Confidential', 'source_locator': {'page': 2}},
        {'type': 'table', 'table_id': 'table-2', 'source_locator': current_locator},
    ]

    _merge_cross_page_tables(content, tables)

    assert len(tables) == 1
    assert tables[0]['raw_rows'] == [
        ['Name', 'Amount'], ['Alpha', '1'], ['Beta', '2']
    ]
    assert tables[0]['cross_page_continuation'] is True
    assert [node['type'] for node in content] == [
        'table', 'boilerplate', 'page_label', 'boilerplate'
    ]
    assert [span['page'] for span in tables[0]['source_locator']['spans']] == [1, 2]
    assert all(
        span['table_detection'] == 'vector_grid'
        for span in tables[0]['source_locator']['spans']
    )


def test_pdf_same_shape_tables_away_from_page_edges_remain_independent(tmp_path):
    source = tmp_path / 'independent-tables.pdf'

    def draw(c):
        for y, left, right in ((500, 'Item', 'Amount'), (480, 'Alpha', '10')):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)
        c.showPage()
        for y, left, right in ((740, 'Item', 'Amount'), (720, 'Beta', '20')):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 2
    assert all(not table.get('cross_page_continuation', False) for table in data['tables'])


def test_pdf_boundary_tables_without_repeated_header_remain_independent(tmp_path):
    source = tmp_path / 'boundary-independent-tables.pdf'

    def draw(c):
        for y, left, right in (
            (110, 'Item', 'Amount'), (90, 'Alpha', '10'), (70, 'Beta', '20'),
        ):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)
        c.showPage()
        for y, left, right in (
            (740, 'Metric', 'Value'), (720, 'Open', '7'), (700, 'Closed', '3'),
        ):
            c.drawString(72, y, left)
            c.drawRightString(500, y, right)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    tables = _load_bundle(bundle)['tables']
    assert len(tables) == 2
    assert all(not table.get('cross_page_continuation', False) for table in tables)


# --- v6.1.1 correctness foundation -------------------------------------------


def test_pdf_running_chrome_is_canonical_but_hidden_from_markdown(tmp_path):
    source = tmp_path / 'running-chrome.pdf'

    def draw(c):
        for page in range(1, 5):
            if page == 1:
                c.drawString(72, 765, 'Acme Capital letterhead')
            if page == 3:
                c.drawString(72, 748, 'One-page edge disclosure')
            c.drawString(72, 730, 'CONFIDENTIAL REPORT')
            c.drawString(72, 395, 'Recurring body marker')
            c.drawString(72, 360, f'Unique body paragraph {page}.')
            c.drawString(72, 45, 'Acme Research')
            c.drawCentredString(306, 25, f'Page {page} of 4')
            if page != 4:
                c.showPage()

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    boilerplate = [node for node in data['content'] if node['type'] == 'boilerplate']
    labels = [node for node in data['content'] if node['type'] == 'page_label']
    assert [node['text'] for node in boilerplate].count('CONFIDENTIAL REPORT') == 4
    assert [node['text'] for node in boilerplate].count('Acme Research') == 4
    assert [node['text'] for node in labels] == [f'Page {page} of 4' for page in range(1, 5)]
    assert all(node['source_locator']['page'] in {1, 2, 3, 4} for node in boilerplate + labels)

    markdown = (bundle / 'running-chrome.md').read_text(encoding='utf-8')
    assert 'CONFIDENTIAL REPORT' not in markdown
    assert 'Acme Research' not in markdown
    assert 'Page 1 of 4' not in markdown
    assert 'Acme Capital letterhead' in markdown
    assert 'One-page edge disclosure' in markdown
    assert markdown.count('Recurring body marker') == 4
    assert not any(
        item['content_loss']
        for item in data['quality']['warnings']
        if item['code'] == 'running_chrome_classified'
    )


def _native_fragment(text, bbox, *, angle=0, object_index=1, container_context=()):
    fragment = {
        'text': text,
        'bbox': list(bbox),
        'text_angle': angle,
        'font_size': 10.0,
        'font_weight': 400,
        'char_width': 5.0,
        '_object_index': object_index,
    }
    if container_context:
        fragment['_container_context'] = tuple(container_context)
    return fragment


def test_pdf_native_duplicate_paint_layer_deduplicates_exact_geometry_only():
    from pdf_adapter import _deduplicate_native_fragments

    exact = _native_fragment('Duplicate paint layer', [72, 700, 190, 712], object_index=1)
    duplicate = _native_fragment('Duplicate paint layer', [72, 700, 190, 712], object_index=2)
    retained, dropped = _deduplicate_native_fragments([exact, duplicate])

    assert [item['text'] for item in retained] == ['Duplicate paint layer']
    assert dropped == 1


@pytest.mark.parametrize(
    'candidate',
    [
        _native_fragment('Repeated label', [200, 700, 280, 712], object_index=2),
        _native_fragment('Repeated label', [72, 700, 152, 712], angle=90, object_index=2),
        _native_fragment('Repeated label', [73.25, 698.75, 153.25, 710.75], object_index=2),
        _native_fragment('Different label', [72, 700, 152, 712], object_index=2),
    ],
    ids=['different-position', 'different-rotation', 'visible-shadow', 'different-text'],
)
def test_pdf_native_overlap_dedup_preserves_non_equivalent_layers(candidate):
    from pdf_adapter import _deduplicate_native_fragments

    original = _native_fragment('Repeated label', [72, 700, 152, 712], object_index=1)
    retained, dropped = _deduplicate_native_fragments([original, candidate])

    assert len(retained) == 2
    assert dropped == 0


def test_pdf_native_overlap_dedup_preserves_distinct_container_contexts():
    from pdf_adapter import _deduplicate_native_fragments

    first = _native_fragment(
        'Nested Form content', [72, 700, 170, 712],
        object_index=1, container_context=(101, 1),
    )
    second = _native_fragment(
        'Nested Form content', [72, 700, 170, 712],
        object_index=2, container_context=(202, 1),
    )

    retained, dropped = _deduplicate_native_fragments([first, second])

    assert retained == [first, second]
    assert dropped == 0


def test_pdf_container_chain_identity_tracks_nested_form_ancestry():
    import ctypes
    from types import SimpleNamespace

    from pdf_adapter import _container_chain_identity

    page = SimpleNamespace(raw=ctypes.c_void_p(101), container=None)
    outer_form = SimpleNamespace(raw=ctypes.c_void_p(202), container=page)
    inner_form = SimpleNamespace(raw=ctypes.c_void_p(303), container=outer_form)
    text_object = SimpleNamespace(container=inner_form)

    assert _container_chain_identity(text_object) == (303, 202, 101)


def test_pdf_character_geometry_refinement_preserves_internal_context():
    from pdf_adapter import _fragments_from_character_geometry

    fragment = _native_fragment(
        'Alpha Beta', [72, 700, 130, 712],
        object_index=7, container_context=(303, 9),
    )
    fragment['_object_key'] = 707
    characters = [
        {'text': char, 'bbox': [72 + index * 5, 700, 76 + index * 5, 712]}
        for index, char in enumerate('Alpha Beta')
    ]

    refined = _fragments_from_character_geometry(fragment, characters)

    assert [item['text'] for item in refined] == ['Alpha', 'Beta']
    assert all(item['_container_context'] == (303, 9) for item in refined)
    assert all(item['_object_key'] == 707 for item in refined)
    assert all(item['_object_index'] == 7 for item in refined)


def test_pdf_native_duplicate_paint_layer_emits_text_once(tmp_path):
    source = tmp_path / 'duplicate-paint.pdf'

    def draw(c):
        c.drawString(72, 720, 'Duplicate paint layer')
        c.drawString(72, 720, 'Duplicate paint layer')
        c.drawString(72, 680, 'Independent body text.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    canonical_text = '\n'.join(node.get('text', '') for node in data['content'])
    markdown = (bundle / 'duplicate-paint.md').read_text(encoding='utf-8')
    assert canonical_text.count('Duplicate paint layer') == 1
    assert markdown.count('Duplicate paint layer') == 1
    assert 'Independent body text.' in markdown


def test_pdf_ocr_auto_routes_fragment_and_unicode_map_failures():
    from pdf_adapter import _analyze_ocr_need

    fragment_failure = _analyze_ocr_need(
        'auto',
        'Healthy native text content ' * 8,
        [],
        [],
        1,
        612,
        792,
        [{'code': 'pdf_text_object_error', 'content_loss': True}],
    )
    unicode_failure = _analyze_ocr_need(
        'auto',
        'Readable-looking native text ' * 8,
        [{'text': 'Readable-looking native text'}],
        [],
        1,
        612,
        792,
        [],
        unicode_map_error_ratio=0.35,
    )

    assert fragment_failure['should_run'] is True
    assert fragment_failure['native_unusable'] is True
    assert 'native_fragment_extraction_failed' in fragment_failure['reasons']
    assert unicode_failure['should_run'] is True
    assert unicode_failure['native_unusable'] is True
    assert 'unicode_map_error' in unicode_failure['reasons']


@pytest.mark.parametrize('provider_kind', ['failure', 'empty'])
def test_pdf_ocr_force_only_failure_keeps_healthy_native_page(tmp_path, provider_kind):
    from canonical import sha256_file
    from ocr_provider import OcrProviderError
    from pdf_adapter import PdfAdapter

    source = tmp_path / f'force-{provider_kind}.pdf'
    _make_pdf(
        source,
        lambda c: c.drawString(
            72, 720, 'Healthy native paragraph remains authoritative after forced OCR.'
        ),
    )

    class Provider:
        name = 'controlled-ocr'

        def extract(self, _page, _page_number):
            if provider_kind == 'failure':
                raise OcrProviderError('controlled failure')
            return []

    digest = sha256_file(source)
    result = PdfAdapter(Provider(), ocr_mode='force').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    assert result['source_units'][0]['status'] == 'warning'
    warning = next(
        item for item in result['source_units'][0]['warnings']
        if item['code'] == ('ocr_failed' if provider_kind == 'failure' else 'ocr_empty_result')
    )
    assert warning['content_loss'] is False
    assert not any(item['code'] == 'ocr_required' for item in result['warnings'])
    text = '\n'.join(node.get('text', '') for node in result['content'])
    assert text.count('Healthy native paragraph remains authoritative after forced OCR.') == 1


def test_pdf_ocr_elapsed_time_is_not_published_and_output_is_deterministic(tmp_path):
    from canonical import sha256_file
    from ocr_provider import OcrPageResult, OcrSettings, OcrSpan
    from pdf_adapter import PdfAdapter

    source = tmp_path / 'ocr-timing.pdf'
    _make_pdf(source, lambda c: c.rect(72, 600, 200, 100, stroke=1, fill=0))

    class TimedOcr:
        name = 'timed-ocr'
        version = '1.0'
        settings = OcrSettings(mode='auto', dpi=144)

        def __init__(self, elapsed):
            self.elapsed = elapsed

        def extract(self, _page, page_number):
            polygon = ((72.0, 620.0), (250.0, 620.0), (250.0, 642.0), (72.0, 642.0))
            return OcrPageResult(
                page_number=page_number,
                engine=self.name,
                engine_version=self.version,
                runtime='injected',
                runtime_version='1.0',
                model_profile='test',
                language='ch',
                min_confidence=0.5,
                spans=(OcrSpan('Deterministic OCR result', 0.98, polygon, (72, 620, 250, 642)),),
                requested_dpi=144,
                effective_dpi=144,
                raster_width=1224,
                raster_height=1584,
                elapsed_seconds=self.elapsed,
            )

    digest = sha256_file(source)
    first = PdfAdapter(TimedOcr(0.01), ocr_mode='auto').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )
    second = PdfAdapter(TimedOcr(9.99), ocr_mode='auto').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    assert 'elapsed_seconds' not in first['source_units'][0]['locator']['ocr']
    assert first == second


def test_pdf_ocr_only_rotated_page_uses_polygon_for_display_order(tmp_path):
    from canonical import sha256_file
    from ocr_provider import OcrPageResult, OcrSettings, OcrSpan
    from pdf_adapter import PdfAdapter

    source = tmp_path / 'rotated-ocr-only.pdf'
    _make_pdf(source, lambda c: c.rect(72, 600, 200, 100, stroke=1, fill=0))

    class RotatedOcr:
        name = 'rotated-ocr'
        version = '1.0'
        settings = OcrSettings(mode='auto', dpi=144)

        def extract(self, _page, page_number):
            first_polygon = ((100.0, 100.0), (100.0, 200.0), (120.0, 200.0), (120.0, 100.0))
            second_polygon = ((200.0, 300.0), (200.0, 400.0), (220.0, 400.0), (220.0, 300.0))
            return OcrPageResult(
                page_number=page_number,
                engine=self.name,
                engine_version=self.version,
                runtime='injected',
                runtime_version='1.0',
                model_profile='test',
                language='ch',
                min_confidence=0.5,
                spans=(
                    OcrSpan('First OCR line', 0.98, first_polygon, (100, 100, 120, 200)),
                    OcrSpan('Second OCR line', 0.98, second_polygon, (200, 300, 220, 400)),
                ),
                requested_dpi=144,
                effective_dpi=144,
                raster_width=1224,
                raster_height=1584,
            )

    digest = sha256_file(source)
    result = PdfAdapter(RotatedOcr(), ocr_mode='auto').extract(
        str(source), f'sha256:{digest}', 'preserve'
    )

    unit_locator = result['source_units'][0]['locator']
    assert unit_locator['dominant_text_angle'] == 90
    assert unit_locator['orientation_normalized'] is True
    values = [node.get('text', '') for node in result['content']]
    assert values == ['First OCR line', 'Second OCR line']
    assert result['content'][0]['source_locator']['bbox'] == [100.0, 100.0, 120.0, 200.0]
    assert result['content'][0]['source_locator']['layout_bbox'] != result['content'][0]['source_locator']['bbox']


def test_pdf_two_column_tail_continues_into_right_column_once_with_spans(tmp_path):
    source = tmp_path / 'column-flow.pdf'

    def draw(c):
        for y, text in (
            (740, 'Left context one.'),
            (650, 'Left context two.'),
            (560, 'Left context three.'),
            (470, 'Left context four.'),
            (80, 'This analysis continues'),
        ):
            c.drawString(50, y, text)
        for y, text in (
            (740, 'on the next column without truncation.'),
            (650, 'Right context one.'),
            (560, 'Right context two.'),
            (470, 'Right context three.'),
            (380, 'Right context four.'),
        ):
            c.drawString(330, y, text)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    joined = next(
        node for node in data['content']
        if node.get('text') == 'This analysis continues on the next column without truncation.'
    )
    spans = joined['source_locator']['spans']
    assert [span['layout_column'] for span in spans] == ['left', 'right']
    assert [span['page'] for span in spans] == [1, 1]
    assert all('bbox' in span and 'layout_bbox' in span for span in spans)
    markdown = (bundle / 'column-flow.md').read_text(encoding='utf-8')
    assert markdown.count('This analysis continues') == 1
    assert markdown.count('on the next column without truncation.') == 1


# --- v6.2 vector-table enhancement -------------------------------------------


def test_pdf_vector_ruled_grid_recovers_short_all_text_table(tmp_path):
    source = tmp_path / 'vector-ruled-table.pdf'

    def draw(c):
        for x in (72, 250, 500):
            c.line(x, 650, x, 730)
        for y in (650, 690, 730):
            c.line(72, y, 500, y)
        c.drawString(82, 705, 'Category')
        c.drawString(260, 705, 'Region')
        c.drawString(82, 665, 'Services')
        c.drawString(260, 665, 'Asia Pacific')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 1
    table = data['tables'][0]
    assert table['raw_rows'] == [
        ['Category', 'Region'],
        ['Services', 'Asia Pacific'],
    ]
    assert table['headers'] == ['Category', 'Region']
    assert table['source_locator']['table_detection'] == 'vector_grid'
    assert table['source_locator']['column_ranges'] == [[72.0, 250.0], [250.0, 500.0]]


def test_pdf_vector_ruled_grid_does_not_invent_numeric_data_header(tmp_path):
    source = tmp_path / 'vector-data-only-table.pdf'

    def draw(c):
        for x in (72, 250, 500):
            c.line(x, 650, x, 730)
        for y in (650, 690, 730):
            c.line(72, y, 500, y)
        c.drawString(82, 705, 'Alpha')
        c.drawString(260, 705, '101')
        c.drawString(82, 665, 'Beta')
        c.drawString(260, 665, '202')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    table = _load_bundle(bundle)['tables'][0]
    assert table['raw_rows'] == [['Alpha', '101'], ['Beta', '202']]
    assert 'headers' not in table


def test_pdf_vector_booktabs_uses_midrule_as_header_evidence(tmp_path):
    source = tmp_path / 'vector-booktabs-table.pdf'

    def draw(c):
        for y in (620, 700, 740):
            c.line(72, y, 500, y)
        for y, left, right in (
            (715, 'Business', 'Market'),
            (670, 'Cloud services', 'Asia Pacific'),
            (640, 'Advisory', 'Europe'),
        ):
            c.drawString(82, y, left)
            c.drawString(330, y, right)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 1
    table = data['tables'][0]
    assert table['raw_rows'] == [
        ['Business', 'Market'],
        ['Cloud services', 'Asia Pacific'],
        ['Advisory', 'Europe'],
    ]
    assert table['headers'] == ['Business', 'Market']
    assert table['source_locator']['table_detection'] == 'vector_booktabs'


def test_pdf_vector_thin_filled_rectangles_act_as_booktabs_rules(tmp_path):
    source = tmp_path / 'vector-filled-booktabs.pdf'

    def draw(c):
        for y in (620, 700, 740):
            c.rect(72, y, 428, 1, stroke=0, fill=1)
        for y, left, right in (
            (715, 'Product', 'Territory'),
            (670, 'Analytics', 'Americas'),
            (640, 'Consulting', 'Europe'),
        ):
            c.drawString(82, y, left)
            c.drawString(330, y, right)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert len(data['tables']) == 1
    assert data['tables'][0]['raw_rows'][0] == ['Product', 'Territory']
    assert data['tables'][0]['source_locator']['table_detection'] == 'vector_booktabs'


def test_pdf_vector_page_frame_is_not_a_table(tmp_path):
    source = tmp_path / 'vector-page-frame.pdf'

    def draw(c):
        c.rect(20, 20, 572, 752, stroke=1, fill=0)
        c.drawString(72, 720, 'Ordinary paragraph inside a decorative page frame.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert data['tables'] == []
    assert 'Ordinary paragraph inside a decorative page frame.' in (
        bundle / 'vector-page-frame.md'
    ).read_text(encoding='utf-8')


def test_pdf_vector_chart_grid_with_diagonal_falls_back_losslessly(tmp_path):
    source = tmp_path / 'vector-chart-grid.pdf'

    def draw(c):
        for x in (100, 200, 300):
            c.line(x, 100, x, 300)
        for y in (100, 200, 300):
            c.line(100, y, 300, y)
        c.line(100, 100, 300, 300)
        c.drawString(110, 250, 'Chart label alpha')
        c.drawString(110, 150, 'Chart label beta')
        c.drawString(72, 720, 'Narrative remains outside the chart.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert data['tables'] == []
    markdown = (bundle / 'vector-chart-grid.md').read_text(encoding='utf-8')
    assert 'Narrative remains outside the chart.' in markdown
    assert 'Chart label alpha' in markdown
    assert 'Chart label beta' in markdown


# --- v6.2 recursive layout ----------------------------------------------------


def test_pdf_aligned_three_column_prose_is_not_table_and_orders_column_major(tmp_path):
    source = tmp_path / 'aligned-three-column-prose.pdf'

    def draw(c):
        for index, y in enumerate((740, 715, 690, 665), start=1):
            c.drawString(35, y, f'L{index} left narrative sentence.')
            c.drawString(225, y, f'M{index} middle narrative sentence.')
            c.drawString(420, y, f'R{index} right narrative sentence.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    assert data['tables'] == []
    markdown = (bundle / 'aligned-three-column-prose.md').read_text(encoding='utf-8')
    labels = [f'{column}{index}' for column in 'LMR' for index in range(1, 5)]
    positions = [markdown.index(label) for label in labels]
    assert positions == sorted(positions)


def test_pdf_recursive_layout_orders_three_staggered_columns(tmp_path):
    source = tmp_path / 'staggered-three-columns.pdf'

    def draw(c):
        for index, y in enumerate((740, 710, 680), start=1):
            c.drawString(35, y, f'L{index} left column.')
        for index, y in enumerate((732, 702, 672), start=1):
            c.drawString(225, y, f'M{index} middle column.')
        for index, y in enumerate((724, 694, 664), start=1):
            c.drawString(420, y, f'R{index} right column.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    markdown = (bundle / 'staggered-three-columns.md').read_text(encoding='utf-8')
    labels = [f'{column}{index}' for column in 'LMR' for index in range(1, 4)]
    positions = [markdown.index(label) for label in labels]
    assert positions == sorted(positions)


def test_pdf_recursive_layout_preserves_full_width_anchor_between_three_column_bands(tmp_path):
    source = tmp_path / 'three-column-bands.pdf'

    def draw(c):
        for prefix, y in (('Top', 700), ('Bottom', 350)):
            c.drawString(35, y, f'{prefix} left narrative.')
            c.drawString(225, y, f'{prefix} middle narrative.')
            c.drawString(420, y, f'{prefix} right narrative.')
            c.drawString(35, y - 22, f'{prefix} left continuation.')
            c.drawString(225, y - 22, f'{prefix} middle continuation.')
            c.drawString(420, y - 22, f'{prefix} right continuation.')
            c.drawString(35, y - 44, f'{prefix} left final line.')
            c.drawString(225, y - 44, f'{prefix} middle final line.')
            c.drawString(420, y - 44, f'{prefix} right final line.')
        c.setFont('Helvetica-Bold', 14)
        c.drawCentredString(306, 500, 'FULL WIDTH SECTION ANCHOR')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    markdown = (bundle / 'three-column-bands.md').read_text(encoding='utf-8')
    expected = [
        'Top left narrative.', 'Top middle narrative.', 'Top right narrative.',
        'FULL WIDTH SECTION ANCHOR',
        'Bottom left narrative.', 'Bottom middle narrative.', 'Bottom right narrative.',
    ]
    positions = [markdown.index(value) for value in expected]
    assert positions == sorted(positions)


def test_pdf_recursive_layout_uses_full_width_image_as_band_obstacle(tmp_path):
    from PIL import Image

    image_path = tmp_path / 'wide-obstacle.png'
    Image.new('RGB', (400, 80), 'navy').save(image_path)
    source = tmp_path / 'image-obstacle-columns.pdf'

    def draw(c):
        for prefix, base_y in (('Top', 700), ('Bottom', 320)):
            for index, y in enumerate((base_y, base_y - 22, base_y - 44), start=1):
                c.drawString(35, y, f'{prefix} L{index} narrative.')
                c.drawString(225, y, f'{prefix} M{index} narrative.')
                c.drawString(420, y, f'{prefix} R{index} narrative.')
        c.drawImage(str(image_path), 106, 430, width=400, height=80)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    image_index = next(index for index, node in enumerate(data['content']) if node['type'] == 'image')
    before = data['content'][:image_index]
    after = data['content'][image_index + 1:]
    before_text = '\n'.join(node.get('text', '') for node in before)
    after_text = '\n'.join(node.get('text', '') for node in after)
    assert all(f'Top {column}1 narrative.' in before_text for column in 'LMR')
    assert all(f'Bottom {column}1 narrative.' in after_text for column in 'LMR')
    assert not any('Bottom ' in node.get('text', '') for node in before)
    assert not any('Top ' in node.get('text', '') for node in after)


def test_pdf_recursive_layout_keeps_currency_form_rows_in_physical_order():
    from pdf_adapter import _classify_blocks, _order_lines

    def cell(text, left, right, y):
        box = [left, y, right, y + 10]
        return {'text': text, 'bbox': box, 'layout_bbox': box}

    lines = [
        _pdf_layout_line(
            'Total Reimbursement $1,701.64', y=520, left=278, right=531,
            cells=[
                cell('Total Reimbursement', 278, 374, 520),
                cell('$1,701.64', 489, 531, 520),
            ],
        ),
        _pdf_layout_line('Reimbursement Method', y=485, left=58, right=183),
        _pdf_layout_line(
            'Reimbursement Method Direct deposit', y=455, left=58, right=293,
            cells=[
                cell('Reimbursement Method', 58, 172, 455),
                cell('Direct deposit', 231, 293, 455),
            ],
        ),
        _pdf_layout_line('Notes', y=420, left=58, right=88),
        _pdf_layout_line(
            'All receipts are attached and comply with company travel',
            y=390, left=57, right=528,
        ),
        _pdf_layout_line('policy.', y=376, left=58, right=85),
        _pdf_layout_line('Approval', y=343, left=57, right=103),
        _pdf_layout_line(
            'Laura Simmons, Manager Michael Tran, Employee', y=285,
            left=58, right=392,
            cells=[
                cell('Laura Simmons, Manager', 58, 200, 285),
                cell('Michael Tran, Employee', 297, 392, 285),
            ],
        ),
    ]

    ordered, layout = _order_lines(lines, 612)

    ordered_text = [line['text'] for line in ordered]
    assert layout is None
    assert ordered_text[0] == 'Total Reimbursement $1,701.64'
    assert ordered_text.index('Direct deposit') < ordered_text.index('Notes')
    blocks = _classify_blocks(
        ordered,
        2,
        document_body_size=10,
        document_body_height=10,
        document_has_heading_size_signal=False,
    )
    assert any(
        'All receipts are attached and comply with company travel policy.'
        in block.get('text', '')
        for block in blocks
    )


# --- v6.2 typography and sequence evidence -----------------------------------


def test_pdf_body_size_bold_wrapped_heading_uses_context(tmp_path):
    source = tmp_path / 'body-size-bold-heading.pdf'

    def draw(c):
        c.setFont('Helvetica-Bold', 12)
        c.drawString(72, 740, 'Strategic priorities and')
        c.drawString(72, 725, 'market positioning')
        c.setFont('Helvetica', 12)
        c.drawString(72, 690, 'The ordinary body paragraph begins here and continues.')
        c.drawString(72, 674, 'It contains the document body typography evidence.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    headings = [node for node in data['content'] if node['type'] == 'heading']
    assert [node['text'] for node in headings] == [
        'Strategic priorities and market positioning'
    ]
    assert headings[0]['level'] == 3


@pytest.mark.parametrize(
    'value',
    [
        'Figure 2. Revenue trend',
        'Table 4. Segment results',
        'Risk factors ................ 12',
        'A.',
        'I.',
    ],
)
def test_pdf_heading_evidence_rejects_caption_toc_and_marker(value):
    from pdf_adapter import _classify_blocks

    candidate = _pdf_layout_line(value, y=730, font_size=10)
    candidate['font_weight'] = 700
    body = _pdf_layout_line(
        'Ordinary body paragraph provides regular-weight context.', y=690, font_size=10
    )
    blocks = _classify_blocks([candidate, body], page_number=1)

    assert blocks[0]['type'] == 'paragraph'


@pytest.mark.parametrize(
    ('values', 'ordinals'),
    [
        (['A) Alpha item', 'B) Beta item', 'C) Gamma item'], [1, 2, 3]),
        (['(i) First roman item', '(ii) Second roman item', '(iii) Third roman item'], [1, 2, 3]),
    ],
)
def test_pdf_alpha_and_roman_runs_emit_true_ordinals(values, ordinals):
    from pdf_adapter import _classify_blocks

    lines = [
        _pdf_layout_line(value, y=730 - index * 24, font_size=10)
        for index, value in enumerate(values)
    ]
    blocks = _classify_blocks(lines, page_number=1)

    assert [block['type'] for block in blocks] == ['list_item'] * 3
    assert [block['ordered'] for block in blocks] == [True, True, True]
    assert [block['ordinal'] for block in blocks] == ordinals
    assert [block['text'] for block in blocks] == [
        value.split(maxsplit=1)[1] for value in values
    ]
    assert [block['raw_text'] for block in blocks] == values


@pytest.mark.parametrize('value', ['A. Smith', 'I. Introduction'])
def test_pdf_isolated_alpha_or_roman_marker_remains_paragraph(value):
    from pdf_adapter import _classify_blocks

    blocks = _classify_blocks(
        [
            _pdf_layout_line(value, y=730, font_size=10),
            _pdf_layout_line('Ordinary prose follows.', y=690, font_size=10),
        ],
        page_number=1,
    )

    assert blocks[0]['type'] == 'paragraph'


def test_pdf_list_marker_is_not_duplicated_in_markdown(tmp_path):
    source = tmp_path / 'alpha-list.pdf'

    def draw(c):
        for y, value in zip(
            (730, 705, 680),
            ('A) Alpha item', 'B) Beta item', 'C) Gamma item'),
        ):
            c.drawString(72, y, value)

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')

    assert code == 0, stderr
    data = _load_bundle(bundle)
    items = [node for node in data['content'] if node['type'] == 'list_item']
    assert [node['ordinal'] for node in items] == [1, 2, 3]
    assert [node['raw_text'] for node in items] == [
        'A) Alpha item', 'B) Beta item', 'C) Gamma item'
    ]
    markdown = (bundle / 'alpha-list.md').read_text(encoding='utf-8')
    assert '1. Alpha item' in markdown
    assert '2. Beta item' in markdown
    assert '3. Gamma item' in markdown
    assert 'A) Alpha item' not in markdown
