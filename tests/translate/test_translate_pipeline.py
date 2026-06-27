"""
Tests for translate_pipeline.py and glossary_utils.py.

Run from project root:
  python -m pytest tests/translate/test_translate_pipeline.py -v
"""
import os
import json
import subprocess
import tempfile
import pytest
import sys

# Path to the translate scripts
_PIPELINE_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'translate', 'scripts')
sys.path.insert(0, os.path.normpath(_PIPELINE_DIR))

SCRIPT = [sys.executable, os.path.normpath(os.path.join(_PIPELINE_DIR, 'translate_pipeline.py'))]

# Test config
_TEST_CONFIG = os.path.normpath(os.path.join(os.path.dirname(__file__), 'fixtures', 'test_config.json'))
CONFIG_ARG = ['--config', _TEST_CONFIG]


# --- glossary_utils unit tests -------------------------------------------

class TestLoadGlossaryJson:
    def test_simple_dict(self):
        from glossary_utils import load_glossary_json
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump({"machine learning": "机器学习", "neural network": "神经网络"}, f)
            f.flush()
            result = load_glossary_json(f.name)
        os.unlink(f.name)
        assert result == {"machine learning": "机器学习", "neural network": "神经网络"}

    def test_list_of_dicts(self):
        from glossary_utils import load_glossary_json
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump([{"source": "hello", "target": "你好"}, {"source": "world", "target": "世界"}], f)
            f.flush()
            result = load_glossary_json(f.name)
        os.unlink(f.name)
        assert result == {"hello": "你好", "world": "世界"}


class TestLoadGlossaryCsv:
    def test_csv_with_headers(self):
        from glossary_utils import load_glossary_csv
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
            f.write("english,chinese\n")
            f.write("hello,你好\n")
            f.write("world,世界\n")
            f.flush()
            result = load_glossary_csv(f.name)
        os.unlink(f.name)
        assert result == {"hello": "你好", "world": "世界"}

    def test_tsv_with_headers(self):
        from glossary_utils import load_glossary_csv
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tsv', delete=False, encoding='utf-8') as f:
            f.write("source\ttarget\n")
            f.write("gradient descent\t梯度下降\n")
            f.flush()
            result = load_glossary_csv(f.name)
        os.unlink(f.name)
        assert result == {"gradient descent": "梯度下降"}

    def test_csv_fallback_first_two_columns(self):
        from glossary_utils import load_glossary_csv
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
            f.write("hello,你好\n")
            f.write("world,世界\n")
            f.flush()
            result = load_glossary_csv(f.name)
        os.unlink(f.name)
        assert result == {"hello": "你好", "world": "世界"}


class TestLoadGlossaryMd:
    def test_md_table(self):
        from glossary_utils import load_glossary_md
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write("| english | chinese |\n")
            f.write("|---------|---------|\n")
            f.write("| hello | 你好 |\n")
            f.write("| world | 世界 |\n")
            f.flush()
            result = load_glossary_md(f.name)
        os.unlink(f.name)
        assert result == {"hello": "你好", "world": "世界"}

    def test_non_table_returns_empty(self):
        from glossary_utils import load_glossary_md
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write("# This is just text\n\nSome paragraph.\n")
            f.flush()
            result = load_glossary_md(f.name)
        os.unlink(f.name)
        assert result == {}


class TestLoadGlossary:
    def test_json_by_extension(self):
        from glossary_utils import load_glossary
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump({"AI": "人工智能"}, f)
            f.flush()
            result = load_glossary(f.name)
        os.unlink(f.name)
        assert result == {"AI": "人工智能"}


class TestDeriveOutputPath:
    def test_basic(self):
        from glossary_utils import derive_output_path
        result = derive_output_path('/docs/report.md', 'zh')
        assert result.replace('\\', '/').endswith('/docs/report.zh.md')

    def test_strips_existing_suffix(self):
        from glossary_utils import derive_output_path
        result = derive_output_path('/docs/report.zh.md', 'en')
        assert result.replace('\\', '/').endswith('/docs/report.en.md')

    def test_no_double_suffix(self):
        from glossary_utils import derive_output_path
        result = derive_output_path('/docs/report.en.md', 'zh')
        assert 'report.zh.md' in result
        assert 'report.en.zh.md' not in result


class TestDeriveGlossaryPath:
    def test_basic(self):
        from glossary_utils import derive_glossary_path
        result = derive_glossary_path('/docs/report.md', 'zh')
        assert result.replace('\\', '/').endswith('/docs/report.glossary.zh.json')


class TestCollectReferenceFiles:
    def test_collects_from_directory(self):
        from glossary_utils import collect_reference_files
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some files
            open(os.path.join(tmpdir, 'ref1.md'), 'w').close()
            open(os.path.join(tmpdir, 'ref2.txt'), 'w').close()
            open(os.path.join(tmpdir, 'image.png'), 'wb').close()

            result = collect_reference_files([tmpdir])
            basenames = [os.path.basename(f) for f in result]
            assert 'ref1.md' in basenames
            assert 'ref2.txt' in basenames
            assert 'image.png' in basenames

    def test_collects_specific_files(self):
        from glossary_utils import collect_reference_files
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, 'a.md')
            f2 = os.path.join(tmpdir, 'b.md')
            open(f1, 'w').close()
            open(f2, 'w').close()

            result = collect_reference_files([f1])
            assert len(result) == 1
            assert result[0] == f1


# --- Pipeline structural analysis tests ----------------------------------

class TestCountHeadings:
    def test_counts_levels(self):
        from translate_pipeline import count_headings
        text = "# H1\n## H2\n## H2\n### H3\n"
        result = count_headings(text)
        assert result == {1: 1, 2: 2, 3: 1}

    def test_empty(self):
        from translate_pipeline import count_headings
        assert count_headings("") == {}


class TestCountParagraphs:
    def test_counts_text_lines(self):
        from translate_pipeline import count_paragraphs
        text = "First para.\n\nSecond para.\n\n# Heading\n\nThird para.\n"
        result = count_paragraphs(text)
        assert result == 3

    def test_skips_code_blocks(self):
        from translate_pipeline import count_paragraphs
        text = "Text before.\n```\ncode line\n```\nText after.\n"
        result = count_paragraphs(text)
        assert result == 2


class TestCountTables:
    def test_counts_table_blocks(self):
        from translate_pipeline import count_tables
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n\nPara\n\n| c | d |\n|---|---|\n"
        result = count_tables(text)
        assert result == 2


class TestDetectUntranslatedFragments:
    def test_detects_english_in_zh(self):
        from translate_pipeline import detect_untranslated_fragments
        text = "这是一段中文，this is an untranslated fragment in the text，还有更多中文。"
        result = detect_untranslated_fragments(text, 'en', 'zh')
        assert len(result) > 0
        assert any('untranslated fragment' in f for f in result)

    def test_detects_chinese_in_en(self):
        from translate_pipeline import detect_untranslated_fragments
        text = "This is English text 这是一个未翻译的片段 and more English."
        result = detect_untranslated_fragments(text, 'zh', 'en')
        assert len(result) > 0

    def test_no_false_positives_in_code(self):
        from translate_pipeline import detect_untranslated_fragments
        text = "这是中文文本。\n```\nconst message = 'this is code';\n```\n更多中文。\n"
        result = detect_untranslated_fragments(text, 'en', 'zh')
        assert len(result) == 0


# --- Pipeline CLI integration tests --------------------------------------

class TestPipelineCLIPrepare:
    def test_prepare_outputs_source(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write("# Test Document\n\nHello world.\n")
            f.flush()
            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['prepare', '--input', f.name, '--language', 'zh'],
                capture_output=True, text=True,
            )
        os.unlink(f.name)
        assert result.returncode == 0
        assert '=== SOURCE TEXT' in result.stdout
        assert 'Hello world.' in result.stdout
        assert 'zh' in result.stdout

    def test_prepare_with_glossary(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as src:
            src.write("# Test\n\nMachine learning is great.\n")
            src.flush()
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as glos:
                json.dump({"machine learning": "机器学习"}, glos)
                glos.flush()
                result = subprocess.run(
                    SCRIPT + CONFIG_ARG + ['prepare', '--input', src.name,
                                           '--language', 'zh', '--glossary', glos.name],
                    capture_output=True, text=True,
                )
                glos_path = glos.name
            src_path = src.name
        os.unlink(src_path)
        os.unlink(glos_path)
        assert result.returncode == 0
        assert '=== GLOSSARY' in result.stdout
        assert '机器学习' in result.stdout

    def test_prepare_file_not_found(self):
        result = subprocess.run(
            SCRIPT + CONFIG_ARG + ['prepare', '--input', '/nonexistent/file.md', '--language', 'zh'],
            capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert 'not found' in result.stderr.lower() or 'error' in result.stderr.lower()


class TestPipelineCLIQa:
    def test_qa_passes_clean_translation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, 'source.md')
            tgt = os.path.join(tmpdir, 'translation.md')
            with open(src, 'w', encoding='utf-8') as f:
                f.write("# Title\n\nFirst paragraph.\n\nSecond paragraph.\n")
            with open(tgt, 'w', encoding='utf-8') as f:
                f.write("# 标题\n\n第一段。\n\n第二段。\n")

            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', tgt, '--language', 'zh'],
                capture_output=True, text=True,
            )
            assert result.returncode == 0
            assert 'All checks passed' in result.stdout or 'Issues found: 0' in result.stdout or 'QA Report' in result.stdout

    def test_qa_detects_heading_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, 'source.md')
            tgt = os.path.join(tmpdir, 'translation.md')
            with open(src, 'w', encoding='utf-8') as f:
                f.write("# Title\n## Section 1\n## Section 2\n")
            with open(tgt, 'w', encoding='utf-8') as f:
                f.write("# 标题\n## 章节 1\n")

            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', tgt, '--language', 'zh'],
                capture_output=True, text=True,
            )
            assert result.returncode == 0
            assert 'H2 heading count mismatch' in result.stdout

    def test_qa_source_not_found(self):
        result = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', '/nonexistent.md', '--translation', '/nonexistent2.md'],
            capture_output=True, text=True,
        )
        assert result.returncode == 1


class TestPipelineCLIWrite:
    def test_write_creates_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, 'source.md')
            translation = os.path.join(tmpdir, 'translation.md')
            with open(src, 'w', encoding='utf-8') as f:
                f.write("# Original\n")
            with open(translation, 'w', encoding='utf-8') as f:
                f.write("# 标题\n\n翻译内容。\n")

            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['write', '--input', src,
                                       '--translation', translation, '--language', 'zh'],
                capture_output=True, text=True,
            )
            assert result.returncode == 0
            assert '[OK]' in result.stdout

            # Verify output file exists
            expected = os.path.join(tmpdir, 'source.zh.md')
            assert os.path.exists(expected)

            # Verify frontmatter
            with open(expected, 'r', encoding='utf-8') as f:
                content = f.read()
            assert content.startswith('---')
            assert 'translated_by: "translate"' in content
            assert 'target_language: "zh"' in content
            assert '# 标题' in content

    def test_write_no_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, 'source.md')
            translation = os.path.join(tmpdir, 'translation.md')
            with open(src, 'w', encoding='utf-8') as f:
                f.write("# Original\n")
            with open(translation, 'w', encoding='utf-8') as f:
                f.write("# Title\n")

            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['write', '--input', src,
                                       '--translation', translation, '--language', 'en',
                                       '--no-frontmatter'],
                capture_output=True, text=True,
            )
            assert result.returncode == 0
            expected = os.path.join(tmpdir, 'source.en.md')
            with open(expected, 'r', encoding='utf-8') as f:
                content = f.read()
            assert not content.startswith('---')

    def test_write_file_exists_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, 'source.md')
            translation = os.path.join(tmpdir, 'translation.md')
            existing = os.path.join(tmpdir, 'source.zh.md')
            for f_path in [src, translation, existing]:
                with open(f_path, 'w', encoding='utf-8') as f:
                    f.write("content\n")

            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['write', '--input', src,
                                       '--translation', translation, '--language', 'zh'],
                capture_output=True, text=True,
            )
            assert result.returncode == 1
            assert 'already exists' in result.stderr

    def test_write_overwrite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, 'source.md')
            translation = os.path.join(tmpdir, 'translation.md')
            existing = os.path.join(tmpdir, 'source.zh.md')
            with open(src, 'w', encoding='utf-8') as f:
                f.write("# Original\n")
            with open(translation, 'w', encoding='utf-8') as f:
                f.write("# 新标题\n")
            with open(existing, 'w', encoding='utf-8') as f:
                f.write("# 旧标题\n")

            result = subprocess.run(
                SCRIPT + CONFIG_ARG + ['write', '--input', src,
                                       '--translation', translation, '--language', 'zh',
                                       '--overwrite'],
                capture_output=True, text=True,
            )
            assert result.returncode == 0
            with open(existing, 'r', encoding='utf-8') as f:
                content = f.read()
            assert '新标题' in content
            assert '旧标题' not in content


class TestPipelineCLIVersion:
    def test_version(self):
        result = subprocess.run(
            SCRIPT + ['--version'],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert 'translate v' in result.stdout
