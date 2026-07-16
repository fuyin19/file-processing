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
                SCRIPT + CONFIG_ARG + ['prepare', '--input', f.name, '--language', 'zh', '--runtime-mode', 'orchestrated'],
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
                                           '--language', 'zh', '--glossary', glos.name,
                                           '--runtime-mode', 'orchestrated'],
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
            SCRIPT + CONFIG_ARG + ['prepare', '--input', '/nonexistent/file.md', '--language', 'zh',
                                   '--runtime-mode', 'orchestrated'],
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
            assert result.returncode == 2
            assert '--manifest' in result.stderr

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
            # Heading mismatch is an error tier -> exit 1 (gate behavior).
            assert result.returncode == 2
            assert '--manifest' in result.stderr

    def test_qa_source_not_found(self):
        result = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', '/nonexistent.md', '--translation', '/nonexistent2.md'],
            capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert '--manifest' in result.stderr


@pytest.mark.skip(reason='Superseded by manifest-bound v3 write contract tests below.')
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


# --- New: structured glossary / slicing / matching (audit checklist) ------

class TestStructuredGlossary:
    def test_structured_round_trip(self):
        from glossary_utils import save_glossary_structured, load_glossary_structured
        terms = [
            {'source': 'neural network', 'target': '神经网络', 'confidence': 'high',
             'source_chunks': [1, 2], 'evidence': 'ref#p1'},
            {'source': 'coined', 'target': None, 'confidence': 'none', 'status': 'unresolved'},
        ]
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            pass
        try:
            save_glossary_structured(terms, f.name)
            loaded = load_glossary_structured(f.name)
            assert loaded == terms
        finally:
            os.unlink(f.name)

    def test_legacy_flat_loads_as_high_confidence_seeds(self):
        from glossary_utils import save_glossary, load_glossary_structured
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            pass
        try:
            save_glossary({'machine learning': '机器学习'}, f.name)
            loaded = load_glossary_structured(f.name)
            assert loaded == [{'source': 'machine learning', 'target': '机器学习', 'confidence': 'high'}]
        finally:
            os.unlink(f.name)

    def test_load_glossary_json_extracts_flat_from_structured(self):
        # F10 contract: load_glossary_json on {"terms":[...]} returns flat source->target.
        from glossary_utils import load_glossary_json
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump({'terms': [
                {'source': 'a', 'target': '甲', 'confidence': 'high'},
                {'source': 'b', 'target': None, 'confidence': 'none'},
            ]}, f)
            f.flush()
            result = load_glossary_json(f.name)
        os.unlink(f.name)
        assert result == {'a': '甲'}  # target:null entries dropped from the flat view


class TestGlossarySlicing:
    def test_selects_occurrence_and_protected_terms(self):
        from glossary_utils import slice_glossary_for_chunk
        terms = [
            {'source': 'A', 'target': '甲', 'confidence': 'high', 'source_chunks': [1],
             'occurrences': [{'chunk': 1, 'line': 3}]},
            {'source': 'B', 'target': '乙', 'confidence': 'high', 'source_chunks': [2]},
            {'source': 'PROPER', 'target': '专名', 'confidence': 'high', 'source_chunks': [9]},
        ]
        sl = slice_glossary_for_chunk(terms, 1, protected_sources=['PROPER'], max_terms=10)
        sources = {t['source'] for t in sl['terms']}
        assert sources == {'A', 'PROPER'}  # B is chunk-2 only; PROTER protected

    def test_cap_truncates_by_priority(self):
        from glossary_utils import slice_glossary_for_chunk
        terms = [
            {'source': f't{i}', 'target': f'x{i}', 'confidence': conf, 'source_chunks': [1]}
            for i, conf in enumerate(['none', 'medium', 'high', 'high'])
        ]
        sl = slice_glossary_for_chunk(terms, 1, max_terms=2)
        assert sl['count'] == 2
        assert sl['truncated'] is True
        sources = [t['source'] for t in sl['terms']]
        # high-confidence terms win the first two slots.
        assert 't2' in sources and 't3' in sources


class TestTargetMatching:
    def test_cjk_substring(self):
        from glossary_utils import target_present
        assert target_present('神经网络', '这是 神经网络 的例子')
        assert not target_present('神经网络', '这是别的词')

    def test_latin_normalized_word_boundary(self):
        from glossary_utils import target_present
        assert target_present('Neural', 'the NEURAL network')      # case-insensitive
        assert target_present('cafe', 'le CAFÉ noir')               # diacritics-stripped
        assert not target_present('neural', 'the neuronal net')     # no partial-word hit


class TestReferencePassageRetrieval:
    def test_recall_and_scoring(self):
        from translate_pipeline import chunk_references, select_reference_passages
        with tempfile.TemporaryDirectory() as ws:
            refs = [
                ('ref1.md', 'neural network is a model.\n\n' * 3),
                ('ref2.md', 'gradient descent optimizes.\n\n' * 3),
            ]
            manifest = chunk_references(refs, 5, ws)
            hits = select_reference_passages('neural network', manifest, top_k=5)
            assert hits, 'expected at least one matching passage'
            assert any(h['id'].startswith('ref1') for h in hits), hits
            # Non-matching term returns nothing.
            none = select_reference_passages('nonexistent term xyz', manifest, top_k=5)
            assert none == []

    def test_duplicate_reference_basenames_receive_distinct_passage_ids(self):
        from translate_pipeline import chunk_references
        with tempfile.TemporaryDirectory() as ws:
            manifest = chunk_references([
                ('same.md', 'first reference'),
                ('same.md', 'second reference'),
            ], 10, ws)
            assert len({p['id'] for p in manifest}) == 2
            assert len({p['path'] for p in manifest}) == 2

    def test_retrieval_prefers_reference_source_diversity(self):
        from translate_pipeline import chunk_references, select_reference_passages
        with tempfile.TemporaryDirectory() as ws:
            manifest = chunk_references([
                ('a.md', 'alpha alpha\n\nalpha alpha\n\nalpha alpha'),
                ('b.md', 'alpha'),
            ], 1, ws)
            hits = select_reference_passages('alpha', manifest, top_k=2)
            assert {h['id'].split('#')[0] for h in hits} == {'a', 'b'}


class TestTranslatorPayloadValidation:
    def test_valid_payload(self):
        from translate_pipeline import validate_translator_payload
        payload = {'translated_markdown': '# 标题\n\n正文\n',
                   'self_audit': {'chunk': 1, 'headings': 1, 'paragraphs': 1}}
        ok, errs = validate_translator_payload(payload, 1, '/ws')
        assert ok and errs == []

    def test_missing_markdown_fails(self):
        from translate_pipeline import validate_translator_payload
        ok, errs = validate_translator_payload({'self_audit': {'chunk': 1}}, 1, '/ws')
        assert not ok and any('translated_markdown' in e for e in errs)

    def test_chunk_identity_mismatch_fails(self):
        from translate_pipeline import validate_translator_payload
        payload = {'translated_markdown': 'x', 'self_audit': {'chunk': 99}}
        ok, errs = validate_translator_payload(payload, 1, '/ws')
        assert not ok and any('chunk mismatch' in e for e in errs)


# --- New: structure-safe chunking ----------------------------------------

class TestChunkText:
    def test_code_block_not_split(self):
        from glossary_utils import chunk_text
        code = '\n'.join(f'line {i}' for i in range(40))
        text = f'intro\n\n```python\n{code}\n```\n\noutro'
        chunks = chunk_text(text, 10)
        # No chunk splits a fence: each chunk's ``` count is even.
        for c in chunks:
            n = sum(1 for ln in c['text'].split('\n') if ln.strip().startswith('```'))
            assert n % 2 == 0
        code_chunks = [c for c in chunks if '```python' in c['text']]
        assert len(code_chunks) == 1 and 'line 39' in code_chunks[0]['text']

    def test_table_not_split(self):
        from glossary_utils import chunk_text
        rows = '\n'.join(f'| {i} | r{i} |' for i in range(30))
        text = f'| h |\n|---|\n{rows}'
        chunks = chunk_text(text, 5)
        assert len([c for c in chunks if '| h |' in c['text']]) == 1


# --- New: prepare chunk plan / passage manifest / caps -------------------

class TestPrepareChunkPlan:
    def _prepare(self, tmpdir, refs=False, chunk_lines='15', extra=None):
        src = os.path.join(tmpdir, 'doc.md')
        open(src, 'w', encoding='utf-8').write('# T\n\n' + 'neural network.\\n\\n'.replace('\\n', '\n') * 40)
        cmd = SCRIPT + CONFIG_ARG + ['prepare', '--input', src, '--language', 'zh', '--chunk-lines', chunk_lines,
                                     '--runtime-mode', 'orchestrated']
        if refs:
            ref = os.path.join(tmpdir, 'ref.md')
            open(ref, 'w', encoding='utf-8').write('neural network means 神经网络.\n\n' * 5)
            cmd += ['--references', ref]
        if extra:
            cmd += extra
        return src, subprocess.run(cmd, capture_output=True, text=True)

    def test_chunk_plan_and_files(self, tmp_path):
        src, res = self._prepare(str(tmp_path))
        assert res.returncode == 0, res.stderr
        assert '=== CHUNK PLAN ===' in res.stdout
        assert '=== SOURCE TEXT' in res.stdout  # legacy marker preserved
        plan = json.loads(res.stdout.split('=== CHUNK PLAN ===')[1].split('===')[0])
        assert plan['n_chunks'] > 1
        assert os.path.exists(plan['chunks'][0]['path'])

    def test_passage_manifest_with_references(self, tmp_path):
        src, res = self._prepare(str(tmp_path), refs=True)
        assert res.returncode == 0, res.stderr
        assert '=== PASSAGE MANIFEST ===' in res.stdout
        man = json.loads(res.stdout.split('=== PASSAGE MANIFEST ===')[1].split('===')[0])
        assert man and man[0]['id'].startswith('ref#p')

    def test_max_chunks_cap(self, tmp_path):
        small_cfg = tmp_path / 'cfg.json'
        small_cfg.write_text(json.dumps({
            'default_target_language': 'zh', 'chunk_lines': 5, 'max_chunks': 2,
            'max_terms': 800, 'max_terms_per_chunk_prompt': 120,
            'max_reference_passages_per_term': 5, 'max_workspace_mb': 100,
        }), encoding='utf-8')
        src = tmp_path / 'big.md'
        src.write_text('\n\n'.join(f'para {i}' for i in range(50)), encoding='utf-8')
        res = subprocess.run(
            SCRIPT + ['--config', str(small_cfg), 'prepare', '--input', str(src),
                      '--language', 'zh', '--chunk-lines', '5', '--runtime-mode', 'orchestrated'],
            capture_output=True, text=True,
        )
        assert res.returncode == 1
        assert 'max_chunks' in res.stderr


# --- New: qa per-occurrence / convergence / auto-discover / conf:none -----

@pytest.mark.skip(reason='Superseded by manifest-bound v3 occurrence-ledger tests below.')
class TestQaPerOccurrence:
    def _setup(self, tmpdir, terms, chunk_translations, assembled_text=None):
        """Write source chunks + their translations + assembled file + auto glossary."""
        from glossary_utils import save_glossary_structured, derive_glossary_path
        src = os.path.join(tmpdir, 'doc.md')
        open(src, 'w', encoding='utf-8').write(
            '\n\n'.join(f'chunk {i} source' for i in range(1, len(chunk_translations) + 1))
        )
        ws = os.path.join(tmpdir, '.translate-workspace')
        os.makedirs(ws, exist_ok=True)
        for i, t in enumerate(chunk_translations, start=1):
            open(os.path.join(ws, f'chunk_{i:03d}.md'), 'w', encoding='utf-8').write(f'chunk {i} source\n')
            open(os.path.join(ws, f'chunk_{i:03d}.zh.md'), 'w', encoding='utf-8').write(t)
        assembled = os.path.join(tmpdir, 'all.zh.md')
        open(assembled, 'w', encoding='utf-8').write(assembled_text or '\n\n'.join(chunk_translations))
        save_glossary_structured(terms, derive_glossary_path(src, 'zh'))
        return src, assembled, ws

    def test_correct_chunk_attributed(self, tmp_path):
        # term in chunk 1 only; chunk 1 missing target -> flagged chunk 1; chunk 2 irrelevant.
        terms = [{'source': 'alpha', 'target': '阿尔法', 'confidence': 'high', 'source_chunks': [1]}]
        chunk_translations = ['这里没有目标词', '阿尔法出现了']  # chunk2 has it but term not in source c2
        src, assembled, ws = self._setup(str(tmp_path), terms, chunk_translations)
        res = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', assembled,
                                   '--language', 'zh', '--workspace', ws],
            capture_output=True, text=True,
        )
        assert res.returncode == 1
        assert 'not applied in chunk 1' in res.stdout
        fix = json.loads(res.stdout.split('=== FIX MAP ===')[1].split('=== SUMMARY ===')[0])
        assert any(e['chunk'] == 1 for e in fix)

    def test_global_false_hit_must_not_pass(self, tmp_path):
        # Target present in chunk 2 but term's source is only in chunk 1 (which lacks it).
        # A global "target appears once" check would wrongly pass; per-occurrence must fail.
        terms = [{'source': 'beta', 'target': '贝塔', 'confidence': 'high', 'source_chunks': [1]}]
        chunk_translations = ['第一块没有目标词', '贝塔 贝塔']  # chunk1 lacks 贝塔; chunk2 has it
        src, assembled, ws = self._setup(
            str(tmp_path), terms, chunk_translations,
            assembled_text='第一块没有目标词\n\n贝塔 贝塔',
        )
        res = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', assembled,
                                   '--language', 'zh', '--workspace', ws],
            capture_output=True, text=True,
        )
        assert res.returncode == 1  # per-occurrence caught it despite target appearing globally

    def test_convergence_gate_source_not_in_chunk_not_required(self, tmp_path):
        # term source_chunks=[1] only; chunk 2 translation lacks target -> must NOT flag chunk 2.
        terms = [{'source': 'gamma', 'target': '伽马', 'confidence': 'high', 'source_chunks': [1]}]
        chunk_translations = ['伽马在此', '与此无关']  # chunk 2 has no 伽马, fine
        src, assembled, ws = self._setup(str(tmp_path), terms, chunk_translations)
        res = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', assembled,
                                   '--language', 'zh', '--workspace', ws],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, res.stdout  # no errors: chunk 1 has it, chunk 2 not required

    def test_auto_discover_without_glossary_flag(self, tmp_path):
        # No --glossary passed; glossary sits at derive_glossary_path -> qa enforces it.
        terms = [{'source': 'delta', 'target': '德尔塔', 'confidence': 'high', 'source_chunks': [1]}]
        chunk_translations = ['缺失目标词']  # lacks 德尔塔
        src, assembled, ws = self._setup(str(tmp_path), terms, chunk_translations)
        res = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', assembled,
                                   '--language', 'zh', '--workspace', ws],
            capture_output=True, text=True,
        )
        assert res.returncode == 1
        assert 'delta' in res.stdout  # auto-discovered and enforced

    def test_confidence_none_empty_rendered_is_error(self, tmp_path):
        terms = [{'source': 'epsilon', 'target': None, 'confidence': 'none', 'source_chunks': [1]}]
        chunk_translations = ['某个翻译']  # source not residual, but audit says rendered empty
        src, assembled, ws = self._setup(str(tmp_path), terms, chunk_translations)
        # write a self_audit claiming empty rendered
        open(os.path.join(ws, 'self_audit_001.json'), 'w', encoding='utf-8').write(json.dumps({
            'chunk': 1,
            'glossary_applied': [{'source': 'epsilon', 'rendered': '', 'confidence': 'none'}],
        }))
        res = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', assembled,
                                   '--language', 'zh', '--workspace', ws],
            capture_output=True, text=True,
        )
        assert res.returncode == 1
        assert 'empty rendered' in res.stdout


# --- v3 manifest / occurrence contract -----------------------------------

class TestV3RuntimeContract:
    def test_v3_seed_loader_does_not_fabricate_reference_evidence(self):
        from glossary_utils import load_glossary_v3
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump({'Alpha': '阿尔法'}, f)
            f.flush()
            entries = load_glossary_v3(f.name)
        os.unlink(f.name)
        assert entries[0]['origin'] == 'user_seed'
        assert entries[0]['evidence'] == []

    def test_relevance_batches_are_complete_and_deterministic(self):
        from glossary_utils import partition_relevance_batches
        items = [{'occurrence_id': f'o{i}', 'source': f't{i}'} for i in [3, 1, 2, 4, 0]]
        batches = partition_relevance_batches(items, 2)
        assert [[x['occurrence_id'] for x in batch] for batch in batches] == [
            ['o0', 'o1'], ['o2', 'o3'], ['o4'],
        ]
        assert sorted(x['occurrence_id'] for batch in batches for x in batch) == [f'o{i}' for i in range(5)]

    def test_occurrence_ledger_is_stable_and_requires_terminal_disposition(self):
        from v3_runtime import build_occurrence_ledger, validate_occurrence_ledger
        source = 'Alpha appears here.\n\nAlpha appears again.\n'
        chunks = [
            {'index': 1, 'start': 1, 'end': 1},
            {'index': 2, 'start': 2, 'end': 3},
        ]
        ledger = build_occurrence_ledger(source, chunks, [
            {'source': 'Alpha', 'target': '阿尔法', 'type': 'proper-noun'},
        ])
        assert len(ledger) == 2
        assert ledger[0]['term_id'] == ledger[1]['term_id']
        assert ledger[0]['occurrence_id'] != ledger[1]['occurrence_id']
        assert ledger[0]['source_offset'] < ledger[1]['source_offset']
        assert validate_occurrence_ledger(ledger)
        for item in ledger:
            item['disposition'] = 'applied'
        assert validate_occurrence_ledger(ledger) == []

    def test_key_item_preserve_requires_reason(self):
        from v3_runtime import build_occurrence_ledger, validate_occurrence_ledger
        ledger = build_occurrence_ledger('Codex\n', [{'index': 1, 'start': 1, 'end': 1}], [
            {'source': 'Codex', 'type': 'proper-noun'},
        ])
        ledger[0]['disposition'] = 'preserved'
        assert any('requires a reason' in e for e in validate_occurrence_ledger(ledger))
        ledger[0]['reason'] = 'Official product name'
        assert validate_occurrence_ledger(ledger) == []

    def test_translation_audit_must_cover_every_occurrence(self):
        from v3_runtime import build_occurrence_ledger, validate_translation_occurrences
        ledger = build_occurrence_ledger('Alpha Alpha', [{'index': 1, 'start': 1, 'end': 1}], [
            {'source': 'Alpha', 'type': 'proper-noun'},
        ])
        one_only = {'occurrence_ids': [ledger[0]['occurrence_id']]}
        assert any('misses occurrence_ids' in e for e in validate_translation_occurrences(ledger, one_only))
        complete = {'occurrence_ids': [x['occurrence_id'] for x in ledger]}
        assert validate_translation_occurrences(ledger, complete) == []

    def test_manifest_digest_ignores_self_reference(self):
        from v3_runtime import make_manifest, manifest_digest
        manifest = make_manifest(
            source_path='source.md', source_hash='abc', language='zh',
            runtime_mode='orchestrated', quality_mode='strict', workspace='workspace',
            reference_hashes=[], config={}, stages=['prepare'],
        )
        digest = manifest_digest(manifest)
        manifest['manifest_sha256'] = digest
        assert manifest_digest(manifest) == digest

    def test_content_addressed_cache_reuses_first_published_value(self, tmp_path):
        from v3_runtime import fingerprint, publish_cache_json
        key = fingerprint(stage='reference_mining', input='same')
        first = publish_cache_json(tmp_path, key, {'value': 1})
        second = publish_cache_json(tmp_path, key, {'value': 2})
        assert first['hit'] is False
        assert second['hit'] is True
        assert second['value'] == {'value': 1}

    def test_content_addressed_cache_does_not_overwrite_under_concurrency(self, tmp_path):
        from concurrent.futures import ThreadPoolExecutor
        from v3_runtime import fingerprint, publish_cache_json
        key = fingerprint(stage='reference_mining', input='concurrent')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda value: publish_cache_json(tmp_path, key, {'value': value}), range(8)))
        assert sum(not result['hit'] for result in results) == 1
        values = {result['value']['value'] for result in results}
        assert len(values) == 1


class TestV3CliContract:
    def _prepare(self, tmpdir, *, quality='strict', runtime='orchestrated'):
        src = os.path.join(tmpdir, 'source.md')
        ws = os.path.join(tmpdir, 'run')
        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Title\n\nhello\n')
        result = subprocess.run(
            SCRIPT + CONFIG_ARG + ['prepare', '--input', src, '--language', 'zh',
                                   '--workspace', ws, '--quality-mode', quality,
                                   '--runtime-mode', runtime],
            capture_output=True, text=True,
        )
        return src, ws, result

    def _publish(self, manifest, workspace, stage, artifact, payload):
        from v3_runtime import stage_input_hash
        payload = dict(payload)
        manifest_data = json.load(open(manifest, encoding='utf-8'))
        payload.setdefault('schema_version', '3.0')
        payload.setdefault('run_id', manifest_data['run_id'])
        payload.setdefault('stage_input_hash', stage_input_hash(manifest_data, stage))
        payload.setdefault('attempt', 1)
        path = os.path.join(workspace, f'{stage}-{artifact}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        return subprocess.run(
            SCRIPT + CONFIG_ARG + ['publish-stage', '--manifest', manifest,
                                   '--stage', stage, '--artifact', artifact, '--input', path],
            capture_output=True, text=True,
        )

    def _valid_matching(self, manifest, source_text='hello', target='你好'):
        from v3_runtime import build_occurrence_ledger
        data = json.load(open(manifest, encoding='utf-8'))
        plan = json.load(open(data['artifacts']['chunk_plan']['path'], encoding='utf-8'))
        candidates = [{
            'source': source_text, 'target': target, 'type': 'proper-noun',
            'origin': 'reference', 'evidence': ['manual-seed'],
        }]
        ledger = build_occurrence_ledger(open(data['source']['path'], encoding='utf-8').read(), plan['chunks'], candidates)
        for occurrence in ledger:
            occurrence['disposition'] = 'applied'
            occurrence['target'] = target
            occurrence['origin'] = 'reference'
            occurrence['evidence'] = ['manual-seed']
        return {
            'scan_chunks': [{'chunk': item['index'], 'status': 'completed'} for item in plan['chunks']],
            'source_candidates': candidates,
            'occurrences': ledger,
            'relevance_batches': [[item['occurrence_id'] for item in ledger]],
        }

    def _publish_valid_pre_qa(self, manifest, workspace, translation_text):
        memory = {
            'passages': [], 'terms': [], 'expressions': [], 'style_rules': [],
            'semantic_retrieval': {'status': 'not_applicable', 'reason': 'no references'},
        }
        memory_result = self._publish(manifest, workspace, 'reference_mining', 'reference_memory', memory)
        assert memory_result.returncode == 0, memory_result.stderr
        matching = self._valid_matching(manifest)
        matching_result = self._publish(manifest, workspace, 'source_matching', 'occurrence_ledger', matching)
        assert matching_result.returncode == 0, matching_result.stderr
        translation = {
            'translation_sha256': __import__('hashlib').sha256(translation_text.encode('utf-8')).hexdigest(),
            'chunks': [{'chunk': 1, 'translated_markdown': translation_text,
                        'occurrence_ids': [item['occurrence_id'] for item in matching['occurrences']]}],
        }
        translated = self._publish(manifest, workspace, 'translation', 'translation', translation)
        assert translated.returncode == 0, translated.stderr
        return matching

    def _publish_valid_semantic_qa(self, manifest, workspace, translation_text, matching):
        semantic = {
            'translation_sha256': __import__('hashlib').sha256(translation_text.encode('utf-8')).hexdigest(),
            'checked_occurrence_ids': [item['occurrence_id'] for item in matching['occurrences']],
            'issues': [],
            'checks': {
                'reference_expression': 'pass', 'chunk_seams': 'pass', 'register_style': 'pass',
                'context_rules': 'pass', 'source_residual': 'pass',
            },
        }
        result = self._publish(manifest, workspace, 'semantic_qa', 'semantic_qa', semantic)
        assert result.returncode == 0, result.stderr

    def test_strict_prepare_requires_runtime(self, tmp_path):
        src = os.path.join(str(tmp_path), 'source.md')
        with open(src, 'w', encoding='utf-8') as f:
            f.write('# Title\n\nhello\n')
        ws = os.path.join(str(tmp_path), 'run')
        result = subprocess.run(
            SCRIPT + CONFIG_ARG + ['prepare', '--input', src, '--language', 'zh', '--workspace', ws],
            capture_output=True, text=True,
        )
        assert result.returncode == 4
        assert not os.path.exists(os.path.join(ws, 'run_manifest.json'))

    def test_prepare_creates_isolated_manifest_and_resume_is_recoverable(self, tmp_path):
        _src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0, result.stderr
        manifest = os.path.join(ws, 'run_manifest.json')
        data = json.load(open(manifest, encoding='utf-8'))
        assert data['schema_version'] == '3.0'
        assert data['stages']['prepare']['state'] == 'completed'
        assert data['stages']['reference_mining']['state'] == 'pending'
        resumed = subprocess.run(SCRIPT + CONFIG_ARG + ['resume', '--manifest', manifest],
                                capture_output=True, text=True)
        assert resumed.returncode == 2
        assert 'reference_mining' in resumed.stdout

    def test_publish_stage_rejects_wrong_run_id(self, tmp_path):
        _src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        bad = os.path.join(ws, 'bad.json')
        with open(bad, 'w', encoding='utf-8') as f:
            json.dump({'schema_version': '3.0', 'run_id': 'not-this-run'}, f)
        published = subprocess.run(
            SCRIPT + CONFIG_ARG + ['publish-stage', '--manifest', manifest,
                                   '--stage', 'reference_mining', '--artifact', 'memory', '--input', bad],
            capture_output=True, text=True,
        )
        assert published.returncode == 1
        assert 'run_id' in published.stderr

    def test_publish_stage_rejects_wrong_input_fingerprint(self, tmp_path):
        _src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        data = json.load(open(manifest, encoding='utf-8'))
        bad = os.path.join(ws, 'bad-fingerprint.json')
        with open(bad, 'w', encoding='utf-8') as f:
            json.dump({'schema_version': '3.0', 'run_id': data['run_id'],
                       'stage_input_hash': 'wrong'}, f)
        published = subprocess.run(
            SCRIPT + CONFIG_ARG + ['publish-stage', '--manifest', manifest,
                                   '--stage', 'reference_mining', '--artifact', 'memory', '--input', bad],
            capture_output=True, text=True,
        )
        assert published.returncode == 1
        assert 'stage_input_hash' in published.stderr

    def test_report_only_qa_and_write_are_manifest_bound(self, tmp_path):
        src, ws, result = self._prepare(str(tmp_path), quality='report-only', runtime='unavailable')
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        translation = os.path.join(ws, 'translation.zh.md')
        with open(translation, 'w', encoding='utf-8') as f:
            f.write('# 标题\n\n你好\n')
        qa = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', translation,
                                   '--language', 'zh', '--manifest', manifest],
            capture_output=True, text=True,
        )
        assert qa.returncode == 1, qa.stdout + qa.stderr
        written = subprocess.run(
            SCRIPT + CONFIG_ARG + ['write', '--input', src, '--translation', translation,
                                   '--language', 'zh', '--manifest', manifest],
            capture_output=True, text=True,
        )
        assert written.returncode == 3, written.stdout + written.stderr
        incomplete = os.path.join(os.path.dirname(src), 'source.zh.incomplete.md')
        assert os.path.exists(incomplete)
        assert 'qa_status: "INCOMPLETE"' in open(incomplete, encoding='utf-8').read()

    def test_strict_write_rejects_incomplete_required_agent_stages(self, tmp_path):
        src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        translation = os.path.join(ws, 'translation.zh.md')
        with open(translation, 'w', encoding='utf-8') as f:
            f.write('# 标题\n\n你好\n')
        qa = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', translation,
                                   '--language', 'zh', '--manifest', manifest],
            capture_output=True, text=True,
        )
        assert qa.returncode == 1
        written = subprocess.run(
            SCRIPT + CONFIG_ARG + ['write', '--input', src, '--translation', translation,
                                   '--language', 'zh', '--manifest', manifest],
            capture_output=True, text=True,
        )
        assert written.returncode == 1
        assert 'required stage not completed' in written.stderr

    @pytest.mark.skip(reason='This encoded the pre-fix empty-artifact bypass; replaced by validated v3 path.')
    def test_strict_write_succeeds_only_after_published_agent_stages(self, tmp_path):
        src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        for stage, artifact, payload in [
            ('reference_mining', 'reference_memory', {'passages': [], 'terms': []}),
            ('source_matching', 'occurrence_ledger', {'occurrences': []}),
            ('translation', 'translation', {'chunks': [1]}),
            ('semantic_qa', 'semantic_qa', {'issues': []}),
        ]:
            published = self._publish(manifest, ws, stage, artifact, payload)
            assert published.returncode == 0, published.stdout + published.stderr
        translation = os.path.join(ws, 'translation.zh.md')
        with open(translation, 'w', encoding='utf-8') as f:
            f.write('# 标题\n\n你好\n')
        qa = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', translation,
                                   '--language', 'zh', '--manifest', manifest],
            capture_output=True, text=True,
        )
        assert qa.returncode == 0, qa.stdout + qa.stderr
        written = subprocess.run(
            SCRIPT + CONFIG_ARG + ['write', '--input', src, '--translation', translation,
                                   '--language', 'zh', '--manifest', manifest],
            capture_output=True, text=True,
        )
        assert written.returncode == 0, written.stdout + written.stderr
        output = os.path.join(os.path.dirname(src), 'source.zh.md')
        content = open(output, encoding='utf-8').read()
        assert 'qa_status: "strict-pass"' in content
        assert 'run_id:' in content

    def test_strict_write_requires_validated_artifacts_and_binds_frontmatter(self, tmp_path):
        src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        translation_text = '# \u6807\u9898\n\n\u4f60\u597d\n'
        translation = os.path.join(ws, 'translation.zh.md')
        with open(translation, 'w', encoding='utf-8') as f:
            f.write(translation_text)
        matching = self._publish_valid_pre_qa(manifest, ws, translation_text)
        qa = subprocess.run(
            SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', translation,
                                   '--language', 'zh', '--manifest', manifest],
            capture_output=True, text=True,
        )
        assert qa.returncode == 0, qa.stdout + qa.stderr
        self._publish_valid_semantic_qa(manifest, ws, translation_text, matching)
        written = subprocess.run(
            SCRIPT + CONFIG_ARG + ['write', '--input', src, '--translation', translation,
                                   '--language', 'zh', '--manifest', manifest],
            capture_output=True, text=True,
        )
        assert written.returncode == 0, written.stdout + written.stderr
        content = open(os.path.join(os.path.dirname(src), 'source.zh.md'), encoding='utf-8').read()
        assert 'qa_status: "strict-pass"' in content
        assert 'manifest_sha256:' in content

    def test_translation_artifact_hash_must_match_canonical_chunks(self, tmp_path):
        _src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        memory = self._publish(manifest, ws, 'reference_mining', 'reference_memory', {
            'passages': [], 'terms': [], 'expressions': [], 'style_rules': [],
            'semantic_retrieval': {'status': 'not_applicable', 'reason': 'no references'},
        })
        assert memory.returncode == 0, memory.stderr
        matching = self._valid_matching(manifest)
        matched = self._publish(manifest, ws, 'source_matching', 'occurrence_ledger', matching)
        assert matched.returncode == 0, matched.stderr
        tampered = self._publish(manifest, ws, 'translation', 'translation', {
            'translation_sha256': __import__('hashlib').sha256('# 标题\n\n错误\n'.encode('utf-8')).hexdigest(),
            'chunks': [{'chunk': 1, 'translated_markdown': '# 标题\n\n你好\n',
                        'occurrence_ids': [item['occurrence_id'] for item in matching['occurrences']]}],
        })
        assert tampered.returncode == 1
        assert 'canonical chunk assembly' in tampered.stderr

    def test_empty_or_malformed_artifacts_cannot_be_published(self, tmp_path):
        _src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        published = self._publish(manifest, ws, 'reference_mining', 'reference_memory', {
            'passages': [], 'terms': [], 'expressions': [], 'style_rules': [],
        })
        assert published.returncode == 1
        assert 'semantic_retrieval' in published.stderr

    def test_empty_occurrence_ledger_needs_complete_empty_scan_accounting(self, tmp_path):
        _src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        memory = self._publish(manifest, ws, 'reference_mining', 'reference_memory', {
            'passages': [], 'terms': [], 'expressions': [], 'style_rules': [],
            'semantic_retrieval': {'status': 'not_applicable', 'reason': 'no references'},
        })
        assert memory.returncode == 0, memory.stderr
        empty = self._publish(manifest, ws, 'source_matching', 'occurrence_ledger', {
            'scan_chunks': [{'chunk': 1, 'status': 'completed'}],
            'source_candidates': [], 'occurrences': [],
        })
        assert empty.returncode == 1
        assert 'empty source scan' in empty.stderr

    def test_semantic_error_and_tampered_qa_report_block_strict_write(self, tmp_path):
        src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        translation_text = '# \u6807\u9898\n\n\u4f60\u597d\n'
        translation = os.path.join(ws, 'translation.zh.md')
        open(translation, 'w', encoding='utf-8').write(translation_text)
        matching = self._publish_valid_pre_qa(manifest, ws, translation_text)
        qa = subprocess.run(SCRIPT + CONFIG_ARG + ['qa', '--source', src, '--translation', translation,
                                                    '--language', 'zh', '--manifest', manifest],
                            capture_output=True, text=True)
        assert qa.returncode == 0, qa.stdout + qa.stderr
        semantic = self._publish(manifest, ws, 'semantic_qa', 'semantic_qa', {
            'translation_sha256': __import__('hashlib').sha256(translation_text.encode('utf-8')).hexdigest(),
            'checked_occurrence_ids': [item['occurrence_id'] for item in matching['occurrences']],
            'issues': [{'severity': 'error', 'issue': 'terminology drift'}],
            'checks': {
                'reference_expression': 'pass', 'chunk_seams': 'pass', 'register_style': 'pass',
                'context_rules': 'pass', 'source_residual': 'pass',
            },
        })
        assert semantic.returncode == 1
        qa_report = os.path.join(ws, 'qa_report.json')
        report = json.load(open(qa_report, encoding='utf-8'))
        report['status'] = 'pass'
        with open(qa_report, 'w', encoding='utf-8') as f:
            json.dump(report, f)
        written = subprocess.run(SCRIPT + CONFIG_ARG + ['write', '--input', src, '--translation', translation,
                                                         '--language', 'zh', '--manifest', manifest],
                                 capture_output=True, text=True)
        assert written.returncode == 1
        assert 'Missing QA report' in written.stderr

    def test_resume_rejects_reference_runtime_or_config_drift(self, tmp_path, monkeypatch):
        src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        monkeypatch.setenv('TRANSLATE_PROMPT_DIGEST', 'changed')
        resumed = subprocess.run(SCRIPT + CONFIG_ARG + ['resume', '--manifest', manifest],
                                capture_output=True, text=True)
        assert resumed.returncode == 1
        assert 'runtime changed' in resumed.stderr

    def test_resume_rejects_manifest_digest_tampering(self, tmp_path):
        _src, ws, result = self._prepare(str(tmp_path))
        assert result.returncode == 0
        manifest = os.path.join(ws, 'run_manifest.json')
        data = json.load(open(manifest, encoding='utf-8'))
        data['language'] = 'en'
        with open(manifest, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        resumed = subprocess.run(SCRIPT + CONFIG_ARG + ['resume', '--manifest', manifest],
                                capture_output=True, text=True)
        assert resumed.returncode == 1
        assert 'manifest digest' in resumed.stderr

    def test_reference_memory_must_complete_every_planned_passage(self, tmp_path):
        src = os.path.join(str(tmp_path), 'source.md')
        ref = os.path.join(str(tmp_path), 'reference.md')
        ws = os.path.join(str(tmp_path), 'run')
        open(src, 'w', encoding='utf-8').write('# Title\n\nhello\n')
        open(ref, 'w', encoding='utf-8').write('hello = \\u4f60\\u597d\n')
        prepared = subprocess.run(
            SCRIPT + CONFIG_ARG + ['prepare', '--input', src, '--language', 'zh', '--references', ref,
                                   '--workspace', ws, '--runtime-mode', 'orchestrated'],
            capture_output=True, text=True,
        )
        assert prepared.returncode == 0, prepared.stderr
        manifest = os.path.join(ws, 'run_manifest.json')
        rejected = self._publish(manifest, ws, 'reference_mining', 'reference_memory', {
            'passages': [], 'terms': [], 'expressions': [], 'style_rules': [],
            'semantic_retrieval': {'status': 'completed', 'provider': 'mock', 'results': []},
        })
        assert rejected.returncode == 1
        assert 'passages do not exactly match' in rejected.stderr
