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
    assert 'markdown-conversion v6.0.0' in stdout
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
    source = tmp_path / 'continued.pdf'

    def draw(c):
        c.drawString(72, 72, 'This paragraph continues')
        c.showPage()
        c.drawString(72, 740, 'on the next page.')

    _make_pdf(source, draw)
    code, _, stderr, bundle = _run_bundle(source, tmp_path / 'out')
    assert code == 0, stderr
    paragraphs = [node for node in _load_bundle(bundle)['content'] if node['type'] == 'paragraph']
    assert len(paragraphs) == 1
    assert paragraphs[0]['text'] == 'This paragraph continues on the next page.'
    assert paragraphs[0]['source_locator']['continued_to_page'] == 2
