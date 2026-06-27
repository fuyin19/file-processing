"""
Tests for cleanup_pipeline.py and fixers.py.

Run from project root:
  python -m pytest tests/markdown-cleanup/test_cleanup_pipeline.py -v
"""
import os
import subprocess
import tempfile
import pytest
import sys

# Path to the cleanup scripts
_PIPELINE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'markdown-cleanup', 'scripts')
sys.path.insert(0, os.path.normpath(_PIPELINE_DIR))

SCRIPT = [sys.executable, os.path.normpath(os.path.join(_PIPELINE_DIR, 'cleanup_pipeline.py'))]

# Test config
_TEST_CONFIG = os.path.normpath(os.path.join(os.path.dirname(__file__), 'fixtures', 'test_config.json'))
CONFIG_ARG = ['--config', _TEST_CONFIG]


# --- Fixer unit tests ---------------------------------------------------

class TestRemoveBase64ImageStubs:
    def test_removes_data_image(self):
        from fixers import remove_base64_image_stubs
        text = 'Hello ![](data:image/jpeg;base64,/9j/4AAQ) world'
        result, count = remove_base64_image_stubs(text)
        assert count == 1
        assert 'base64' not in result
        assert 'Hello' in result
        assert 'world' in result

    def test_removes_with_alt_text(self):
        from fixers import remove_base64_image_stubs
        text = '![chart](data:image/png;base64,iVBORw0KGgo=)'
        result, count = remove_base64_image_stubs(text)
        assert count == 1
        assert result.strip() == ''

    def test_preserves_normal_images(self):
        from fixers import remove_base64_image_stubs
        text = '![logo](logo.png) and ![photo](photo.jpg)'
        result, count = remove_base64_image_stubs(text)
        assert count == 0
        assert '![logo](logo.png)' in result

    def test_multiple_stubs(self):
        from fixers import remove_base64_image_stubs
        text = 'a ![](data:image/png;base64,abc) b ![](data:image/jpeg;base64,def) c'
        result, count = remove_base64_image_stubs(text)
        assert count == 2


class TestRemovePrintMetadata:
    def test_removes_jobname(self):
        from fixers import remove_print_metadata
        text = 'JOBNAME: test_doc PAGE: 1 SESS: 103 OUTPUT: Fri Mar 20 16:51:12 2026'
        result, count = remove_print_metadata(text)
        assert count >= 1
        assert 'JOBNAME' not in result

    def test_removes_mark_trace(self):
        from fixers import remove_print_metadata
        text = 'Mark Trace: > 075 (Fri Feb 27 21:53:35 2026)'
        result, count = remove_print_metadata(text)
        assert count == 1
        assert 'Mark Trace' not in result

    def test_preserves_normal_text(self):
        from fixers import remove_print_metadata
        text = 'Normal paragraph about PAGE numbers'
        result, count = remove_print_metadata(text)
        assert count == 0
        assert text == result


class TestRemoveXmlDataLeakage:
    def test_removes_long_namespace_identifiers(self):
        from fixers import remove_xml_data_leakage
        text = 'ifrs-full:MeasurementBasisSummaryDescriptionOfBasisOfMeasurement'
        result, count = remove_xml_data_leakage(text)
        assert count >= 1
        assert 'ifrs-full' not in result

    def test_removes_underscore_heavy_identifiers(self):
        from fixers import remove_xml_data_leakage
        text = 'some_very_long_identifier_with_many_parts_here'
        result, count = remove_xml_data_leakage(text)
        assert count >= 1

    def test_preserves_normal_text(self):
        from fixers import remove_xml_data_leakage
        text = '正常文本 normal text'
        result, count = remove_xml_data_leakage(text)
        assert count == 0
        assert text == result


class TestRemoveEmptyPptxNotes:
    def test_removes_empty_notes(self):
        from fixers import remove_empty_pptx_notes
        text = '### Notes:\n\nNext paragraph'
        result, count = remove_empty_pptx_notes(text)
        assert count == 1
        assert '### Notes:' not in result

    def test_removes_notes_before_heading(self):
        from fixers import remove_empty_pptx_notes
        text = '### Notes:\n## Next Section'
        result, count = remove_empty_pptx_notes(text)
        assert count == 1

    def test_preserves_notes_with_content(self):
        from fixers import remove_empty_pptx_notes
        text = '### Notes:\nThis is actual note content.\n\nNext paragraph'
        result, count = remove_empty_pptx_notes(text)
        assert count == 0
        assert '### Notes:' in result
        assert 'actual note content' in result


class TestRemoveOrphanedImageRefs:
    def test_removes_image_refs(self):
        from fixers import remove_orphaned_image_refs
        text = '![alt](Image0.jpg) some text ![alt](Picture20.jpg)'
        result, count = remove_orphaned_image_refs(text)
        assert count == 2
        assert 'Image0' not in result

    def test_removes_chinese_image_refs(self):
        from fixers import remove_orphaned_image_refs
        text = '![图标](图片201.jpg) content'
        result, count = remove_orphaned_image_refs(text)
        assert count == 1

    def test_preserves_normal_images(self):
        from fixers import remove_orphaned_image_refs
        text = '![logo](company-logo.png) text'
        result, count = remove_orphaned_image_refs(text)
        assert count == 0
        assert '![logo](company-logo.png)' in result


class TestRemoveDeadTocLinks:
    def test_removes_toc_links(self):
        from fixers import remove_dead_toc_links
        text = '[Introduction 6](#_Toc221481040)'
        result, count = remove_dead_toc_links(text)
        assert count == 1
        assert result == 'Introduction 6'

    def test_removes_bookmark_links(self):
        from fixers import remove_dead_toc_links
        text = '[目录 vii](#bookmark2)'
        result, count = remove_dead_toc_links(text)
        assert count == 1
        assert result == '目录 vii'

    def test_preserves_valid_links(self):
        from fixers import remove_dead_toc_links
        text = '[valid link](https://example.com)'
        result, count = remove_dead_toc_links(text)
        assert count == 0
        assert result == text


class TestRemoveEmptyTableRows:
    def test_removes_empty_rows(self):
        from fixers import remove_empty_table_rows
        text = '| a | b |\n| --- | --- |\n|  |  |\n| c | d |'
        result, count = remove_empty_table_rows(text)
        assert count == 1
        assert '| c | d |' in result

    def test_preserves_rows_with_content(self):
        from fixers import remove_empty_table_rows
        text = '| a | b |\n| --- | --- |\n| x | y |'
        result, count = remove_empty_table_rows(text)
        assert count == 0


class TestUnescapeBackslashChars:
    def test_unescapes_underscore(self):
        from fixers import unescape_backslash_chars
        text = 'HIP25080021\\_E\\_Reborn'
        result, count = unescape_backslash_chars(text)
        assert count == 2
        assert result == 'HIP25080021_E_Reborn'

    def test_unescapes_asterisk(self):
        from fixers import unescape_backslash_chars
        text = 'some \\*bold\\* text'
        result, count = unescape_backslash_chars(text)
        assert count == 2
        assert result == 'some *bold* text'

    def test_preserves_double_backslash(self):
        from fixers import unescape_backslash_chars
        text = 'path \\\\ server'
        result, count = unescape_backslash_chars(text)
        # \\ is not matched by our pattern ([_*#])
        assert '\\\\' in result


class TestCollapseBlankLines:
    def test_collapses_triple_newlines(self):
        from fixers import collapse_blank_lines
        text = 'hello\n\n\n\nworld'
        result, count = collapse_blank_lines(text)
        assert count >= 1
        assert '\n\n\n' not in result
        assert 'hello' in result
        assert 'world' in result

    def test_preserves_double_newlines(self):
        from fixers import collapse_blank_lines
        text = 'hello\n\nworld'
        result, count = collapse_blank_lines(text)
        assert count == 0
        assert text == result

    def test_strips_trailing_whitespace(self):
        from fixers import collapse_blank_lines
        text = 'hello   \nworld  '
        result, count = collapse_blank_lines(text)
        assert 'hello   ' not in result
        assert 'hello\n' in result


class TestFixBrokenWordHyperlinks:
    def test_fixes_mangled_url(self):
        from fixers import fix_broken_word_hyperlinks
        text = '[at **www.example.com**](atwww.example.com)'
        result, count = fix_broken_word_hyperlinks(text)
        assert count == 1
        assert 'http://www.example.com' in result

    def test_preserves_valid_links(self):
        from fixers import fix_broken_word_hyperlinks
        text = '[visit us](https://example.com)'
        result, count = fix_broken_word_hyperlinks(text)
        assert count == 0
        assert text == result


class TestRemoveSlideComments:
    def test_removes_slide_comments(self):
        from fixers import remove_slide_comments
        text = '<!-- Slide number: 1 -->\ncontent'
        result, count = remove_slide_comments(text)
        assert count == 1
        assert 'Slide' not in result

    def test_preserves_other_comments(self):
        from fixers import remove_slide_comments
        text = '<!-- TODO: fix this -->\ncontent'
        result, count = remove_slide_comments(text)
        assert count == 0
        assert 'TODO' in result


# --- Code block protection tests ----------------------------------------

class TestCodeBlockProtection:
    def test_protects_fenced_code(self):
        from fixers import protect_code_blocks, restore_code_blocks
        text = 'before\n```python\nx = 1 + 1\n```\nafter'
        protected, placeholders = protect_code_blocks(text)
        assert '```' not in protected
        assert len(placeholders) == 1
        restored = restore_code_blocks(protected, placeholders)
        assert restored == text

    def test_protects_inline_code(self):
        from fixers import protect_code_blocks, restore_code_blocks
        text = 'use `\\_` to escape underscores'
        protected, placeholders = protect_code_blocks(text)
        assert '`' not in protected
        restored = restore_code_blocks(protected, placeholders)
        assert restored == text

    def test_fixers_dont_touch_code_blocks(self):
        from fixers import (
            protect_code_blocks, restore_code_blocks,
            unescape_backslash_chars,
        )
        text = '```\nHIP25080021\\_E\\_Reborn\n```\n'
        protected, placeholders = protect_code_blocks(text)
        result, _ = unescape_backslash_chars(protected)
        restored = restore_code_blocks(result, placeholders)
        assert '\\_' in restored  # Should NOT be unescaped inside code block


class TestFrontmatterExtraction:
    def test_extracts_frontmatter(self):
        from fixers import extract_frontmatter
        text = '---\nsource: "test"\n---\n\nBody text'
        fm, body = extract_frontmatter(text)
        assert fm.startswith('---')
        assert 'source:' in fm
        assert body == 'Body text'

    def test_no_frontmatter(self):
        from fixers import extract_frontmatter
        text = 'Just body text'
        fm, body = extract_frontmatter(text)
        assert fm == ''
        assert body == text


# --- Pipeline integration tests -----------------------------------------

class TestPipelineCLI:
    def test_version(self):
        result = subprocess.run(
            SCRIPT + ['--version'], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert 'markdown-cleanup v' in result.stdout

    def test_list_fixers(self):
        result = subprocess.run(
            SCRIPT + CONFIG_ARG + ['--list-fixers'], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert 'base64_image_stubs' in result.stdout
        assert 'blank_lines' in result.stdout

    def test_single_file_dry_run(self):
        with tempfile.NamedTemporaryFile(suffix='.md', mode='w', delete=False,
                                          encoding='utf-8') as f:
            f.write('hello\n\n\n\nworld ![](data:image/png;base64,abc)')
            path = f.name
        try:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', path, '--dry-run', '--diff'],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            assert 'base64' in result.stdout or 'blank' in result.stdout
        finally:
            os.unlink(path)

    def test_single_file_in_place(self):
        with tempfile.NamedTemporaryFile(suffix='.md', mode='w', delete=False,
                                          encoding='utf-8') as f:
            f.write('hello\n\n\n\nworld ![](data:image/png;base64,abc)')
            path = f.name
        try:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', path],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert 'base64' not in content
            assert 'hello' in content
            assert 'world' in content
        finally:
            os.unlink(path)

    def test_directory_processing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files
            for i in range(3):
                with open(os.path.join(tmpdir, f'test{i}.md'), 'w', encoding='utf-8') as f:
                    f.write(f'file{i}\n\n\n\ncontent ![](data:image/png;base64,abc{i})')

            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', tmpdir, '--dry-run'],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            assert 'BATCH' in result.stdout

    def test_only_flag(self):
        with tempfile.NamedTemporaryFile(suffix='.md', mode='w', delete=False,
                                          encoding='utf-8') as f:
            f.write('hello\n\n\n\nworld ![](data:image/png;base64,abc)')
            path = f.name
        try:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', path, '--only', 'blank_lines'],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            # base64 should still be present (only blank_lines ran)
            assert 'base64' in content
            # but triple blank lines should be gone
            assert '\n\n\n' not in content
        finally:
            os.unlink(path)

    def test_disable_flag(self):
        with tempfile.NamedTemporaryFile(suffix='.md', mode='w', delete=False,
                                          encoding='utf-8') as f:
            f.write('hello\n\n\n\nworld ![](data:image/png;base64,abc)')
            path = f.name
        try:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', path, '--disable', 'base64_image_stubs'],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            # base64 should still be present (disabled)
            assert 'base64' in content
            # but blank lines should be collapsed
            assert '\n\n\n' not in content
        finally:
            os.unlink(path)

    def test_non_md_file_rejected(self):
        with tempfile.NamedTemporaryFile(suffix='.txt', mode='w', delete=False) as f:
            f.write('not markdown')
            path = f.name
        try:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', path],
                capture_output=True, text=True
            )
            assert result.returncode == 1
        finally:
            os.unlink(path)

    def test_preserves_frontmatter(self):
        with tempfile.NamedTemporaryFile(suffix='.md', mode='w', delete=False,
                                          encoding='utf-8') as f:
            f.write('---\nsource: "test.pdf"\nconverted_at: "2026-04-05"\n---\n\nbody\n\n\n\ntext')
            path = f.name
        try:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', path],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert '---' in content
            assert 'source:' in content
            assert 'converted_at:' in content
        finally:
            os.unlink(path)

    def test_preserves_chinese_text(self):
        with tempfile.NamedTemporaryFile(suffix='.md', mode='w', delete=False,
                                          encoding='utf-8') as f:
            f.write('铂生卓越生物科技（北京）有限公司\n\n\n\n核心战略图景')
            path = f.name
        try:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', path],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            assert '铂生卓越' in content
            assert '核心战略' in content
            assert '\n\n\n' not in content
        finally:
            os.unlink(path)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', tmpdir],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            assert 'No .md files found' in result.stdout

    def test_preserves_slide_structure(self):
        """PPTX slide comments should be preserved by default."""
        with tempfile.NamedTemporaryFile(suffix='.md', mode='w', delete=False,
                                          encoding='utf-8') as f:
            f.write('<!-- Slide number: 1 -->\n\nTitle\n\n### Notes:\n\n<!-- Slide number: 2 -->\n\nContent\n\n### Notes:\n')
            path = f.name
        try:
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['--input', path],
                capture_output=True, text=True
            )
            assert result.returncode == 0
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Slide comments preserved (disabled by default)
            assert '<!-- Slide number: 1 -->' in content
            assert '<!-- Slide number: 2 -->' in content
            # Empty Notes removed
            assert '### Notes:' not in content
        finally:
            os.unlink(path)
