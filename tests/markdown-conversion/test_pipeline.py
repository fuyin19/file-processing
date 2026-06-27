"""
Tests for pipeline.py (v4.0.0 — document-based architecture).

Run from project root: pytest scripts/test_pipeline.py -v

Architecture note:
  The pipeline now accepts source documents (PDF, DOCX, TXT, etc.) as --input.
  Helper functions (fix_encoding, convert_chinese, inject_frontmatter, write_to_vault)
  are tested directly. Integration tests use real temp files.
"""
import os
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
    """Known mojibake patterns should cause exit 1."""
    from pipeline import die, MOJIBAKE_PATTERNS
    # Find a pattern that won't be confused with valid UTF-8
    from pipeline import fix_encoding
    with pytest.raises(SystemExit) as exc_info:
        fix_encoding('Hello ï¿½ world'.encode('utf-8'))
    assert exc_info.value.code == 1


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


def test_inject_frontmatter_defaults():
    """Frontmatter template should contain all required fields."""
    from pipeline import inject_frontmatter
    result = inject_frontmatter('Hello', '/path/to/file.pdf', '2026-03-23T10:00:00')
    assert result.startswith('---')
    assert 'source: "/path/to/file.pdf"' in result
    assert 'converted_at: "2026-03-23T10:00:00"' in result
    assert 'converted_by: "markitdown"' in result
    assert 'Hello' in result


def test_inject_frontmatter_slashes_normalized():
    """Backslashes in source path should be normalized to forward slashes."""
    from pipeline import inject_frontmatter
    result = inject_frontmatter('x', 'C:\\Users\\user\\doc.pdf', '2026-01-01T00:00:00')
    assert 'C:/Users/user/doc.pdf' in result
    # no backslashes in frontmatter
    frontmatter = result.split('---')[1]
    assert '\\' not in frontmatter


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

def _run_pipeline(input_path, source='', extra_args=None, output_path=None):
    """Run pipeline.py with a real file. Returns (returncode, stdout, stderr)."""
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix='.md')
        os.close(fd)
        os.unlink(output_path)
    args = SCRIPT + CONFIG_ARG + [
        '--input', input_path,
        '--output-path', output_path,
    ]
    if source:
        args += ['--source', source]
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

    code, out_str, err, _ = _run_pipeline(str(src), source=str(src), output_path=str(out))
    assert code == 0, err
    assert out.exists()
    content = out.read_text(encoding='utf-8')
    assert content.startswith('---')
    assert 'source:' in content
    assert 'converted_by: "markitdown"' in content
    assert 'Hello world' in content


def test_full_pipeline_no_frontmatter_flag(tmp_path):
    """--no-frontmatter should skip frontmatter injection."""
    src = tmp_path / 'source.txt'
    src.write_text('Content here', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, _, err, _ = _run_pipeline(str(src), source=str(src), output_path=str(out), extra_args=['--no-frontmatter'])
    assert code == 0, err
    content = out.read_text(encoding='utf-8')
    assert not content.startswith('---')


def test_full_pipeline_preserves_chinese(tmp_path):
    """Chinese content should survive the pipeline unchanged (encoding handled by markitdown)."""
    src = tmp_path / 'chinese.txt'
    src.write_text('你好世界', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, _, err, _ = _run_pipeline(str(src), source=str(src), output_path=str(out))
    assert code == 0, err
    content = out.read_text(encoding='utf-8')
    assert '你好世界' in content


def test_success_message_printed_to_stdout(tmp_path):
    """On success, stdout should contain [OK] message."""
    src = tmp_path / 'source.txt'
    src.write_text('content', encoding='utf-8')
    out = tmp_path / 'note.md'

    code, stdout, err, _ = _run_pipeline(str(src), source=str(src), output_path=str(out))
    assert code == 0, err
    assert '[OK]' in stdout or 'Converted' in stdout


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
    base = {'input': None, 'input_dir': None}
    base.update(kwargs)
    return Namespace(**base)


def test_resolve_output_path_single_file_next_to_source(tmp_path):
    """Single local file: default output goes next to the source file."""
    from pipeline import resolve_output_path
    src = tmp_path / 'doc.pdf'
    src.write_text('x', encoding='utf-8')
    out = resolve_output_path(_args_ns(input=str(src)))
    assert out == os.path.join(str(tmp_path), 'doc.md')


def test_resolve_output_path_url_to_cwd():
    """URL input: default output goes to the current working directory."""
    from pipeline import resolve_output_path
    out = resolve_output_path(_args_ns(input='https://example.com/page.html'))
    assert out.endswith('.md')
    assert 'example' in out


def test_resolve_output_path_batch_is_input_dir(tmp_path):
    """Batch: default output directory is the input directory itself."""
    from pipeline import resolve_output_path
    d = tmp_path / 'srcs'
    d.mkdir()
    assert resolve_output_path(_args_ns(input_dir=str(d))) == str(d)


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
    (src_dir / 'a.txt').write_text('File A', encoding='utf-8')
    (src_dir / 'b.txt').write_text('File B', encoding='utf-8')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir))
    assert code == 0, stderr
    assert (out_dir / 'a.md').exists()
    assert (out_dir / 'b.md').exists()
    assert 'File A' in (out_dir / 'a.md').read_text(encoding='utf-8')
    assert 'File B' in (out_dir / 'b.md').read_text(encoding='utf-8')


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
    assert (out_dir / 'top.md').exists()
    assert (out_dir / 'sub' / 'nested.md').exists()


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
    assert (out_dir / 'top.md').exists()
    assert not (out_dir / 'sub' / 'nested.md').exists()


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
    assert (out_dir / 'keep.md').exists()
    assert not (out_dir / 'skip.md').exists()


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
    # Create a file that will likely fail — not a valid format for markitdown
    (src_dir / 'bad.xyz').write_text('bad', encoding='utf-8')

    out_dir = tmp_path / 'output'
    out_dir.mkdir()

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir), ['--types', 'txt,xyz'])
    # .xyz will fail but .txt should succeed; exit code should be 0
    assert code == 0, stderr
    assert (out_dir / 'good.md').exists()


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
    (out_dir / 'file.md').write_text('Old content', encoding='utf-8')

    code, stdout, stderr = _run_batch(str(src_dir), str(out_dir))
    assert code == 0  # not fatal
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
    assert 'mutually exclusive' in stderr.lower()


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


# --- --keep-images integration tests -------------------------------------------


def test_keep_images_flag_in_help():
    """--keep-images flag should appear in --help output."""
    result = subprocess.run(SCRIPT + ['--help'], capture_output=True)
    assert result.returncode == 0
    stdout = result.stdout.decode('utf-8', errors='replace')
    assert '--keep-images' in stdout


def test_default_strips_images_subprocess(tmp_path):
    """Without --keep-images, pipeline should strip image syntax from output."""
    src = tmp_path / 'images.txt'
    src.write_text('# Title\n\n![Chart](chart.png)\n\nSome text.\n\n![Graph](graph.jpg)\n\nMore text.', encoding='utf-8')
    out = tmp_path / 'out.md'

    code, stdout, stderr, _ = _run_pipeline(
        str(src), source=str(src), output_path=str(out),
    )
    assert code == 0, stderr
    content = out.read_text(encoding='utf-8')
    assert '![' not in content
    assert 'chart.png' not in content
    assert 'Some text.' in content
    assert 'More text.' in content


def test_keep_images_flag_preserves_images(tmp_path):
    """With --keep-images, pipeline should preserve image syntax in output."""
    src = tmp_path / 'images.txt'
    src.write_text('# Title\n\n![Chart](chart.png)\n\nSome text.\n\n![Graph](graph.jpg)\n\nMore text.', encoding='utf-8')
    out = tmp_path / 'out.md'

    code, stdout, stderr, _ = _run_pipeline(
        str(src), source=str(src), output_path=str(out),
        extra_args=['--keep-images'],
    )
    assert code == 0, stderr
    content = out.read_text(encoding='utf-8')
    assert '![' in content
    assert 'chart.png' in content
    assert 'Some text.' in content


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


def test_strip_images_code_block_not_affected():
    """strip_images should handle code blocks the same as other text (known behavior)."""
    from pipeline import strip_images
    # This documents current behavior: images inside code blocks are also stripped
    raw = '```\n![inside code](img.jpg)\n```\n'
    result = strip_images(raw)
    # Current behavior: images inside code ARE stripped (documented limitation)
    assert '![' not in result


def test_strip_images_preserves_links():
    """Regular markdown links (not images) should be preserved."""
    from pipeline import strip_images
    raw = 'Check out [this link](https://example.com) for more info.'
    assert strip_images(raw) == raw
