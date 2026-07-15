"""Tests for content-review/scripts/review_plan.py.

Conventions match the other skills' test suites:
- sys.path includes the skill's scripts/ dir so the module can be imported directly.
- CLI tests run the pipeline as a subprocess via SCRIPT = [sys.executable, <path>].
- CONFIG_ARG isolates us from the real scripts/config.json (which may hold local settings).
"""
import sys
import os
import json
import subprocess
import tempfile
import re
import copy
import hashlib
import zipfile
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, '..', '..', 'skills', 'content-review', 'scripts'))
sys.path.insert(0, SCRIPTS)

import review_plan  # noqa: E402
import trigger_harness  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures')
CONFIG_PATH = os.path.join(FIXTURES, 'test_config.json')
CONFIG_ARG = ['--config', CONFIG_PATH]
SCRIPT = [sys.executable, os.path.join(SCRIPTS, 'review_plan.py')]


def _run_cli(*cli_args, check=True):
    """Run review_plan.py with CONFIG_ARG prepended (after --config prescan)."""
    cmd = SCRIPT + list(cli_args)
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')


# ---------------------------------------------------------------------------
# Repository-only trigger-description fixture (no host/LLM calls)
# ---------------------------------------------------------------------------

class TestTriggerFixtureContract:
    def test_repo_frontmatter_locks_dual_trigger_and_exclusions(self):
        skill_md = Path(HERE).parent.parent / 'skills' / 'content-review' / 'SKILL.md'
        name, description = trigger_harness.parse_skill_frontmatter(skill_md)
        lowered = description.lower()
        assert name == 'content-review'
        assert len(description) <= 1024
        assert 'human-facing, non-code prose' in lowered
        assert 'only when both conditions hold' in lowered
        assert 'proofread or edit it or verify it against supplied references' in lowered
        assert 'technical or instruction documents' in lowered
        assert '/file-processing:content-review' in description
        for excluded in (
            'source code', 'prs or diffs', 'tests', 'configs', 'logs', 'apis',
            'implementation correctness', 'skill.md', 'agents.md', 'prompts',
            'plugin metadata',
        ):
            assert excluded in lowered

    def test_versioned_fixture_covers_declared_boundaries(self):
        payload = json.loads(
            Path(FIXTURES, 'trigger_eval.json').read_text(encoding='utf-8')
        )
        assert payload['schema_version'] == '1.0.0'
        cases = payload['cases']
        assert cases and len({case['id'] for case in cases}) == len(cases)
        assert all(
            set(case) == {'id', 'query', 'should_trigger', 'category', 'critical'}
            for case in cases
        )

        negative_categories = {
            case['category'] for case in cases if not case['should_trigger']
        }
        assert {
            'code', 'pr_diff', 'tests', 'configs', 'logs',
            'api_implementation', 'skill_md', 'agents_md', 'prompts',
            'plugin_metadata',
        } <= negative_categories

        technical = {
            (case['category'], case['should_trigger'])
            for case in cases if case['id'].startswith(('zh_technical', 'en_technical'))
        }
        assert ('technical_prose_proofread', True) in technical
        assert ('api_implementation', False) in technical

        explicit = [
            case for case in cases if case['category'] == 'explicit_invocation'
        ]
        assert len(explicit) >= 3
        assert all(case['should_trigger'] for case in explicit)
        assert all('{{candidate_command}}' in case['query'] for case in explicit)
        assert any('skill' in case['id'].lower() for case in explicit)

    def test_fixture_keeps_routing_evidence_separate_from_pytest(self):
        """Pytest validates the corpus only; it never claims live host routing."""
        payload = json.loads(
            Path(FIXTURES, 'trigger_eval.json').read_text(encoding='utf-8')
        )
        assert all('trigger_rate' not in case for case in payload['cases'])
        assert all('runs' not in case for case in payload['cases'])


class TestTriggerHarnessAdapter:
    def test_loader_and_unique_command_substitution_are_offline(self):
        cases = trigger_harness.load_fixture(Path(FIXTURES, 'trigger_eval.json'))
        explicit = next(case for case in cases if case['category'] == 'explicit_invocation')
        query = trigger_harness.substitute_query(explicit, '/content-review-candidate-deadbeef')
        assert '{{candidate_command}}' not in query
        assert '/content-review-candidate-deadbeef' in query

    def test_infrastructure_errors_are_not_counted_as_routing_false(self):
        case = {
            'id': 'infra', 'query': 'query', 'should_trigger': False,
            'category': 'code', 'critical': True,
        }
        run = {
            'case_id': 'infra', 'run': 1, 'outcome': 'infrastructure_error',
            'triggered': None, 'error': 'host missing',
        }
        summary = trigger_harness.summarize([case], [run], 1, 0.5)
        assert summary['summary']['infrastructure_errors'] == 1
        assert summary['summary']['routing_failures'] == 0
        assert summary['results'][0]['pass'] is False

    def test_explicit_command_expansion_marker_is_detected(self):
        state = {
            'pending_tool': None, 'partial_json': '', 'partial_text': '',
            'decision_reason': None, 'negative_reason': None,
            'message_stopped': None,
        }
        event = {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'CR_EXPLICIT_DEADBEEF'},
            },
        }
        assert trigger_harness._event_decision(
            event, 'content-review-candidate-deadbeef',
            'CR_EXPLICIT_DEADBEEF', state,
        ) is True
        assert state['decision_reason'] == 'explicit_command_marker'

    def test_explicit_marker_cannot_satisfy_an_automatic_case(self):
        state = {
            'pending_tool': None, 'partial_json': '', 'partial_text': '',
            'decision_reason': None, 'negative_reason': None,
            'message_stopped': None,
        }
        text_event = {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'CR_EXPLICIT_DEADBEEF'},
            },
        }
        assert trigger_harness._event_decision(
            text_event, 'content-review-candidate-deadbeef', None, state,
        ) is None
        result_event = {'type': 'result', 'is_error': False, 'result': 'done'}
        assert trigger_harness._event_decision(
            result_event, 'content-review-candidate-deadbeef', None, state,
        ) is False

    def test_message_stop_waits_for_a_final_error_result(self):
        state = {
            'pending_tool': None, 'partial_json': '', 'partial_text': '',
            'decision_reason': None, 'negative_reason': None,
            'message_stopped': None,
        }
        stop_event = {
            'type': 'stream_event', 'event': {'type': 'message_stop'},
        }
        assert trigger_harness._event_decision(
            stop_event, 'candidate', None, state,
        ) is None
        with pytest.raises(RuntimeError, match='host failed'):
            trigger_harness._event_decision(
                {'type': 'result', 'is_error': True, 'result': 'host failed'},
                'candidate', None, state,
            )

    def test_a_different_first_tool_is_a_decisive_routing_false(self):
        state = {
            'pending_tool': None, 'partial_json': '', 'partial_text': '',
            'decision_reason': None, 'negative_reason': None,
            'message_stopped': None,
        }
        event = {
            'type': 'stream_event',
            'event': {
                'type': 'content_block_start',
                'content_block': {'type': 'tool_use', 'name': 'Bash'},
            },
        }
        assert trigger_harness._event_decision(
            event, 'candidate', None, state,
        ) is False
        assert state['decision_reason'] == 'first_tool:Bash'

    def test_gating_run_count_cannot_be_zero_or_non_three(self):
        script = str(Path(HERE, 'trigger_harness.py'))
        zero = subprocess.run(
            [sys.executable, script, '--prepare-only', '--runs-per-query', '0',
             '--diagnostic'],
            capture_output=True, text=True, encoding='utf-8',
        )
        assert zero.returncode != 0
        assert 'greater than zero' in zero.stderr

        non_three = subprocess.run(
            [sys.executable, script, '--prepare-only', '--runs-per-query', '1'],
            capture_output=True, text=True, encoding='utf-8',
        )
        assert non_three.returncode != 0
        assert 'gating verification requires --runs-per-query 3' in non_three.stderr

    def test_harness_enforces_single_candidate_isolation(self):
        assert trigger_harness.DEFAULT_WORKERS == 1
        result = subprocess.run(
            [sys.executable, str(Path(HERE, 'trigger_harness.py')),
             '--prepare-only', '--workers', '2'],
            capture_output=True, text=True, encoding='utf-8',
        )
        assert result.returncode != 0
        assert 'invalid choice' in result.stderr


# ---------------------------------------------------------------------------
# Structure-safe chunking (pure functions)
# ---------------------------------------------------------------------------

class TestParseBlocks:
    def test_prose_split_at_blank_lines(self):
        text = "para one\n\npara two\n\npara three"
        blocks = review_plan.parse_blocks(text)
        # 3 prose blocks + 2 blank-line blocks interleaved
        types = [b[0] for b in blocks]
        assert types.count('prose') == 5  # 3 paragraphs + 2 blanks

    def test_fenced_code_block_is_atomic(self):
        text = "intro\n\n```python\nline1\nline2\n```\n\noutro"
        blocks = review_plan.parse_blocks(text)
        code_blocks = [b for b in blocks if b[0] == 'code']
        assert len(code_blocks) == 1
        # The code block contains its own fence lines.
        assert '```python' in code_blocks[0][1][0]
        assert any('```' in l for l in code_blocks[0][1])

    def test_table_is_atomic(self):
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        blocks = review_plan.parse_blocks(text)
        table_blocks = [b for b in blocks if b[0] == 'table']
        assert len(table_blocks) == 1
        assert len(table_blocks[0][1]) == 4

    def test_every_line_covered(self):
        text = "a\n```x\nb\n```\n\nc\n| d | e |\n| 1 | 1 |\n"
        blocks = review_plan.parse_blocks(text)
        rejoined = '\n'.join(line for _, lines in blocks for line in lines)
        # parse_blocks splits on '\n', so rejoined equals the original (modulo a trailing newline).
        assert rejoined.startswith('a\n```x\nb\n```')


class TestChunkTextStructureSafety:
    def _fence_count(self, chunk_text):
        return sum(1 for line in chunk_text.split('\n') if re.match(r'^\s*(```|~~~)', line))

    def test_code_block_not_split_mid_fence(self):
        # A 50-line code block with chunk_lines=10 must land intact in ONE chunk.
        code_body = '\n'.join(f'line {i}' for i in range(50))
        text = f"intro\n\n```python\n{code_body}\n```\n\noutro"
        chunks = review_plan.chunk_text(text, 10)
        # Every chunk has balanced fences (even count).
        for c in chunks:
            assert self._fence_count(c['text']) % 2 == 0, f"chunk {c['index']} splits a fence"
        # Exactly one chunk is oversized and contains the whole block.
        code_chunks = [c for c in chunks if '```python' in c['text']]
        assert len(code_chunks) == 1
        assert 'line 49' in code_chunks[0]['text']

    def test_table_not_split(self):
        rows = '\n'.join(f"| {i} | row {i} |" for i in range(30))
        text = f"| h |\n|---|\n{rows}"
        chunks = review_plan.chunk_text(text, 5)
        table_chunks = [c for c in chunks if '| h |' in c['text']]
        assert len(table_chunks) == 1  # table not split across chunks

    def test_chunks_are_contiguous_and_complete(self):
        text = '\n'.join(f'line {i}' for i in range(100))
        chunks = review_plan.chunk_text(text, 25)
        assert chunks[0]['start'] == 1
        assert chunks[-1]['end'] == 100
        for i in range(1, len(chunks)):
            assert chunks[i]['start'] == chunks[i - 1]['end'] + 1

    def test_small_file_single_chunk(self):
        text = "just one short paragraph"
        chunks = review_plan.chunk_text(text, 400)
        assert len(chunks) == 1
        assert chunks[0]['start'] == 1 and chunks[0]['end'] == 1


# ---------------------------------------------------------------------------
# Versioned protocol and exact-focus contract
# ---------------------------------------------------------------------------

class TestV3Protocol:
    def test_protocol_versions_and_bounds_are_explicit(self):
        assert review_plan.PLAN_SCHEMA == 'ReviewPlan/v3'
        assert review_plan.CELL_RESULT_SCHEMA == 'CellResult/v3'
        assert review_plan.OBSERVATION_SCHEMA == 'ReducerObservation/v1'
        assert review_plan.EVIDENCE_SCHEMA == 'ReferenceEvidence/v1'
        assert review_plan.MAX_REDUCER_INPUT_CHARS == 50_000
        assert review_plan.MAX_REDUCER_DEPTH == 3
        assert review_plan.RESULT_MAX_ATTEMPTS == 3

    @pytest.mark.parametrize(
        ('focus', 'checks'),
        [
            ('grammar', ['grammar']),
            ('style', ['style']),
            ('logic', ['logic']),
            ('consistency', ['consistency']),
            ('all', ['grammar', 'style', 'logic', 'consistency']),
        ],
    )
    def test_focus_maps_to_exact_checks(self, focus, checks):
        assert review_plan.compute_checks(focus) == checks

    def test_unknown_focus_is_rejected(self):
        with pytest.raises((ValueError, SystemExit)):
            review_plan.compute_checks('bogus')


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

class TestDedupe:
    def test_merges_same_line_quote_category(self):
        findings = [
            {'line': 5, 'quote': 'teh', 'category': 'spelling', 'issue': 'a'},
            {'line': 5, 'quote': 'teh', 'category': 'spelling', 'issue': 'dup'},
            {'line': 5, 'quote': 'teh', 'category': 'grammar', 'issue': 'other cat'},
        ]
        out = review_plan._dedupe(findings)
        assert len(out) == 2  # (line,quote,spelling) merged; different category kept


# ---------------------------------------------------------------------------
# CLI: plan (dry-run)
# ---------------------------------------------------------------------------

class TestCLIPlan:
    def test_plan_emits_v3_hashes_manifest_and_run_specific_workspace(self, tmp_path):
        src = tmp_path / 'doc.md'
        src.write_text('# Title\n\nfirst paragraph\n\nsecond paragraph\n', encoding='utf-8')
        workspace_root = tmp_path / 'runs'

        plans = []
        for _ in range(2):
            res = _run_cli(
                *CONFIG_ARG, 'plan', '--input', str(src),
                '--workspace', str(workspace_root), '--focus', 'grammar',
            )
            assert res.returncode == 0, res.stderr
            plans.append(json.loads(res.stdout))

        first, second = plans
        assert first['schema'] == 'ReviewPlan/v3'
        assert first['version'] == '2.0.0'
        assert first['run_id'] != second['run_id']
        assert first['run_workspace'] != second['run_workspace']
        assert Path(first['run_workspace']).is_dir()
        assert Path(first['run_workspace']).parent == workspace_root
        assert first['source_hash'] == hashlib.sha256(
            src.read_bytes()
        ).hexdigest()
        canonical = Path(first['source']['canonical_path'])
        assert canonical.is_file()
        assert first['source']['sha256'] == hashlib.sha256(
            canonical.read_bytes()
        ).hexdigest()
        assert Path(first['source']['line_map_path']).is_file()
        assert first['checks'] == ['grammar']
        assert all(
            cell['checks'] == ['grammar']
            for cell in first['cells'] if cell['stage'] == 'local'
        )
        for chunk in first['chunks']:
            chunk_path = Path(chunk['path'])
            assert chunk_path.is_file()
            assert chunk['sha256'] == hashlib.sha256(chunk_path.read_bytes()).hexdigest()

    @pytest.mark.parametrize('focus', ['grammar', 'style', 'logic', 'consistency'])
    def test_plan_propagates_exact_focus_to_local_cells(self, tmp_path, focus):
        src = tmp_path / f'{focus}.md'
        src.write_text('One concise sentence.\n', encoding='utf-8')
        res = _run_cli(
            *CONFIG_ARG, 'plan', '--input', str(src), '--focus', focus,
        )
        assert res.returncode == 0, res.stderr
        plan = json.loads(res.stdout)
        assert plan['checks'] == [focus]
        local = [cell for cell in plan['cells'] if cell['stage'] == 'local']
        assert local
        assert {check for cell in local for check in cell['checks']} == {focus}
        expected_dimension = (
            'grammar-style' if focus in {'grammar', 'style'} else 'logic-consistency'
        )
        assert {cell['dimension'] for cell in local} == {expected_dimension}

    def test_references_schedule_full_dag_even_with_narrow_focus(self, tmp_path):
        src = tmp_path / 'doc.md'
        ref = tmp_path / 'ref.md'
        src.write_text('The project started in 2025.\n', encoding='utf-8')
        ref.write_text('Work commenced during the prior calendar year.\n', encoding='utf-8')
        res = _run_cli(
            *CONFIG_ARG, 'plan', '--input', str(src), '--focus', 'style',
            '--references', str(ref),
        )
        assert res.returncode == 0, res.stderr
        plan = json.loads(res.stdout)
        reference_cells = [cell for cell in plan['cells'] if cell['stage'] == 'reference']
        assert reference_cells
        dimensions = {cell['dimension'] for cell in reference_cells}
        assert {
            'claim-extraction', 'semantic-routing', 'grounding',
            'reference-coverage', 'adjudication',
        } <= dimensions
        assert plan['reference_artifacts']
        assert plan['passages']
        assert all('sha256' in artifact for artifact in plan['reference_artifacts'])
        assert all('id' in passage for passage in plan['passages'])

    def test_auto_language_uses_dominant_source_language(self, tmp_path):
        src = tmp_path / 'zh.md'
        src.write_text('这是中文工作报告。报告内容主要使用中文。\nEnglish.\n', encoding='utf-8')
        res = _run_cli(
            *CONFIG_ARG, 'plan', '--input', str(src), '--language', 'auto',
        )
        assert res.returncode == 0, res.stderr
        plan = json.loads(res.stdout)
        assert plan['language'] == {'requested': 'auto', 'resolved': 'zh'}

    def test_caps_exceeded_returns_actual_counts_and_safe_remedies(self, tmp_path):
        small_cfg = tmp_path / 'cfg.json'
        small_cfg.write_text(json.dumps({
            'chunk_lines': 2, 'max_chunks': 2, 'max_cells': 60,
        }), encoding='utf-8')
        src = tmp_path / 'big.md'
        src.write_text('\n\n'.join(f'paragraph {i}' for i in range(50)), encoding='utf-8')
        res = _run_cli(
            '--config', str(small_cfg), 'plan', '--input', str(src),
            '--chunk-lines', '2',
        )
        assert res.returncode == 2
        assert re.search(r'actual[^\n]*chunks?[^\n]*\d+|chunk count \d+', res.stderr, re.I)
        assert '--chunk-lines' in res.stderr
        assert '--focus' in res.stderr
        assert 'split' in res.stderr.lower()
        assert 'accept-partial' not in res.stderr
        assert '--force' not in res.stderr

    def test_accept_partial_is_not_a_plan_cap_bypass(self):
        plan_help = _run_cli(*CONFIG_ARG, 'plan', '--help')
        assemble_help = _run_cli(*CONFIG_ARG, 'assemble', '--help')
        assert plan_help.returncode == assemble_help.returncode == 0
        assert '--accept-partial' not in plan_help.stdout
        assert '--accept-partial' in assemble_help.stdout


# ---------------------------------------------------------------------------
# Canonical input conversion
# ---------------------------------------------------------------------------

def _write_minimal_docx(path: Path, text: str) -> None:
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p><w:sectPr/></w:body>
</w:document>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', content_types)
        archive.writestr('_rels/.rels', rels)
        archive.writestr('word/document.xml', document_xml)


def _write_minimal_pdf(path: Path, text: str) -> None:
    escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    stream = f'BT /F1 18 Tf 72 720 Td ({escaped}) Tj ET'.encode('ascii')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        (b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
         b'/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>'),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(stream)).encode('ascii') + b' >>\nstream\n'
        + stream + b'\nendstream',
    ]
    data = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{index} 0 obj\n'.encode('ascii'))
        data.extend(obj)
        data.extend(b'\nendobj\n')
    xref = len(data)
    data.extend(f'xref\n0 {len(objects) + 1}\n'.encode('ascii'))
    data.extend(b'0000000000 65535 f \n')
    for offset in offsets[1:]:
        data.extend(f'{offset:010d} 00000 n \n'.encode('ascii'))
    data.extend(
        f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n'
        f'startxref\n{xref}\n%%EOF\n'.encode('ascii')
    )
    path.write_bytes(data)


class TestCanonicalInputs:
    @pytest.mark.parametrize(
        ('suffix', 'writer', 'needle'),
        [
            ('.html', lambda p, t: p.write_text(
                f'<html><body><h1>{t}</h1></body></html>', encoding='utf-8'
            ), 'Canonical HTML prose'),
            ('.docx', _write_minimal_docx, 'Canonical DOCX prose'),
            ('.pdf', _write_minimal_pdf, 'Canonical PDF prose'),
        ],
    )
    def test_real_small_fixture_converts_to_canonical_markdown(
        self, tmp_path, suffix, writer, needle,
    ):
        src = tmp_path / f'input{suffix}'
        writer(src, needle)
        res = _run_cli(*CONFIG_ARG, 'plan', '--input', str(src), '--focus', 'grammar')
        assert res.returncode == 0, res.stderr
        plan = json.loads(res.stdout)
        canonical = Path(plan['source']['canonical_path']).read_text(encoding='utf-8')
        assert needle in canonical
        assert plan['input_kind'] != 'direct-text'
        assert plan['source']['diff_applicable'] is False

    def test_rtf_is_explicitly_unsupported(self, tmp_path):
        src = tmp_path / 'legacy.rtf'
        src.write_text(r'{\rtf1\ansi Raw control text}', encoding='utf-8')
        res = _run_cli(*CONFIG_ARG, 'plan', '--input', str(src))
        assert res.returncode == 1
        assert 'rtf' in res.stderr.lower()
        assert 'unsupported' in res.stderr.lower()

    def test_url_is_classified_before_local_abspath(self):
        url = 'https://example.invalid/report.html'
        classified = review_plan.classify_input(url)
        assert classified['kind'] == 'url'
        assert classified['original'] == url
        assert not classified.get('local_path')


# ---------------------------------------------------------------------------
# CLI: assemble (mocked cell results)
# ---------------------------------------------------------------------------

def _prepare_plan(tmp_path, text='recieve the report.\n', *, suffix='.md', focus='all',
                  language='auto', reference_text=None, chunk_lines=None):
    src = tmp_path / f'doc{suffix}'
    if suffix == '.html':
        src.write_text(f'<html><body><p>{text}</p></body></html>', encoding='utf-8')
    else:
        src.write_text(text, encoding='utf-8')
    args = [*CONFIG_ARG, 'plan', '--input', str(src), '--focus', focus,
            '--language', language, '--workspace', str(tmp_path / 'runs')]
    if chunk_lines is not None:
        args += ['--chunk-lines', str(chunk_lines)]
    if reference_text is not None:
        ref = tmp_path / 'reference.md'
        ref.write_text(reference_text, encoding='utf-8')
        args += ['--references', str(ref)]
    result = _run_cli(*args)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    plan_path = tmp_path / 'plan.json'
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding='utf-8')
    return plan, plan_path, src


def _valid_result(plan, cell, *, findings=None, observations=None,
                  reference_assessments=None):
    identity = {
        'id': cell['id'], 'stage': cell['stage'], 'dimension': cell['dimension'],
    }
    if 'chunk' in cell:
        identity['chunk'] = cell['chunk']
    payload = {
        'schema': 'CellResult/v3',
        'run_id': plan['run_id'],
        'input_hash': cell['input_hash'],
        'cell': identity,
        'checked_thoroughly': True,
        'checks_completed': list(cell['checks']),
        'findings': findings or [],
        'observations': observations or [],
    }
    if reference_assessments is not None or cell['dimension'] in {
        'semantic-routing', 'grounding', 'reference-coverage', 'adjudication',
    }:
        payload['reference_assessments'] = reference_assessments or []
    return payload


def _write_result(cells_dir: Path, cell, payload) -> Path:
    path = cells_dir / cell['result_file']
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


def _write_all_results(plan, cells_dir: Path, overrides=None, *, plan_path=None):
    cells_dir.mkdir(exist_ok=True)
    overrides = overrides or {}
    payloads = {}
    for cell in plan['cells']:
        payload = copy.deepcopy(overrides.get(cell['id'], _valid_result(plan, cell)))
        if cell['stage'] == 'global':
            inputs = []
            for dependency_id in cell['dependencies']:
                serialized = json.dumps(
                    payloads[dependency_id].get('observations', []),
                    ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                )
                inputs.append({
                    'cell_id': dependency_id,
                    'sha256': hashlib.sha256(serialized.encode('utf-8')).hexdigest(),
                    'serialized_chars': len(serialized),
                })
            payload['observation_inputs'] = inputs
        result_path = _write_result(
            cells_dir, cell, payload
        )
        payloads[cell['id']] = payload
        if plan_path is not None:
            accepted = _run_cli(
                *CONFIG_ARG, 'validate-cell', '--plan', str(plan_path),
                '--result', str(result_path), '--cell-id', cell['id'],
            )
            assert accepted.returncode == 0, accepted.stdout + accepted.stderr


def _finding(line=1, original='recieve', revised='receive', *, category='spelling',
             evidence=None):
    return {
        'locations': [{'start_line': line, 'end_line': line}],
        'original_text': original,
        'revised_text': revised,
        'change': f'Replace “{original}” with “{revised}”.',
        'reason': 'Correct the misspelling.',
        'severity': 'high',
        'category': category,
        'fixable': True,
        'evidence': evidence or [],
    }


class TestCellValidationAndStatus:
    def test_status_keeps_undispatched_and_retryable_cells_out_of_failed(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(tmp_path, focus='grammar')
        cells_dir = tmp_path / 'cells'
        cells_dir.mkdir()

        pending = _run_cli(
            *CONFIG_ARG, 'status', '--plan', str(plan_path),
            '--cells-dir', str(cells_dir),
        )
        pending_data = json.loads(pending.stdout)
        assert pending_data['counts']['failed'] == 0
        assert pending_data['counts']['completed'] == 0
        assert all(row['status'] == 'pending' for row in pending_data['cells'])

        cell = plan['cells'][0]
        invalid = _valid_result(plan, cell)
        invalid['checked_thoroughly'] = False
        result_path = _write_result(cells_dir, cell, invalid)
        first = _run_cli(
            *CONFIG_ARG, 'validate-cell', '--plan', str(plan_path),
            '--result', str(result_path), '--cell-id', cell['id'],
        )
        assert first.returncode == 1

        retryable = _run_cli(
            *CONFIG_ARG, 'status', '--plan', str(plan_path),
            '--cells-dir', str(cells_dir),
        )
        retryable_data = json.loads(retryable.stdout)
        row = next(item for item in retryable_data['cells'] if item['cell_id'] == cell['id'])
        assert row['status'] == 'retryable'
        assert row['retry_remaining'] == 2
        assert retryable_data['counts']['failed'] == 0

        for _ in range(2):
            _run_cli(
                *CONFIG_ARG, 'validate-cell', '--plan', str(plan_path),
                '--result', str(result_path), '--cell-id', cell['id'],
            )
        terminal = _run_cli(
            *CONFIG_ARG, 'status', '--plan', str(plan_path),
            '--cells-dir', str(cells_dir),
        )
        terminal_data = json.loads(terminal.stdout)
        row = next(item for item in terminal_data['cells'] if item['cell_id'] == cell['id'])
        assert row['status'] == 'FAILED'
        assert row['retry_remaining'] == 0
        assert terminal_data['counts']['failed'] == 1

    def test_valid_cell_records_acceptance_and_status_counts(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(tmp_path, focus='grammar')
        cell = next(cell for cell in plan['cells'] if cell['stage'] == 'local')
        result_path = tmp_path / 'result.json'
        result_path.write_text(json.dumps(_valid_result(plan, cell), ensure_ascii=False), encoding='utf-8')
        validated = _run_cli(*CONFIG_ARG, 'validate-cell', '--plan', str(plan_path),
                             '--result', str(result_path))
        assert validated.returncode == 0, validated.stderr
        assert json.loads(validated.stdout)['status'] == 'accepted'

        cells_dir = tmp_path / 'cells'
        _write_all_results(plan, cells_dir, plan_path=plan_path)
        status = _run_cli(*CONFIG_ARG, 'status', '--plan', str(plan_path),
                          '--cells-dir', str(cells_dir))
        data = json.loads(status.stdout)
        assert data['complete'] is True
        assert {
            'planned', 'dispatched', 'valid', 'retried', 'completed', 'failed',
        } <= set(data['counts'])
        assert data['counts']['planned'] == len(plan['cells'])
        assert data['counts']['dispatched'] == len(plan['cells'])
        assert data['counts']['valid'] == len(plan['cells'])
        assert data['counts']['failed'] == 0
        assert data['counts'].get('unverified', 0) == 0
        assert data['counts']['completed'] == len(plan['cells'])

    def test_schema_hash_quote_range_observation_and_check_order_gates(self, tmp_path):
        plan, _, _ = _prepare_plan(tmp_path, 'recieve the report.\n', focus='all')
        cell = next(cell for cell in plan['cells']
                    if cell['stage'] == 'local' and len(cell['checks']) == 2)
        observation = {
            'schema': 'ReducerObservation/v1', 'kind': 'term', 'key': 'report',
            'value': 'report', 'normalized_value': 'report',
            'locations': [{'start_line': 1, 'end_line': 1}],
        }
        base = _valid_result(plan, cell, findings=[_finding()], observations=[observation])
        assert review_plan.validate_cell_result(plan, cell, base) == []

        corruptions = []
        for mutate in (
            lambda p: p.update(schema='CellResult/v2'),
            lambda p: p.update(run_id='stale-run'),
            lambda p: p.update(input_hash='0' * 64),
            lambda p: p.update(checked_thoroughly=False),
            lambda p: p.update(checks_completed=list(reversed(p['checks_completed']))),
            lambda p: p['findings'][0].update(original_text='not a verbatim quote'),
            lambda p: p['findings'][0].update(locations=[{'start_line': 99, 'end_line': 99}]),
            lambda p: p['observations'][0].update(kind='instruction'),
            lambda p: p['observations'][0].update(value='invented term'),
        ):
            payload = copy.deepcopy(base)
            mutate(payload)
            corruptions.append(review_plan.validate_cell_result(plan, cell, payload))
        assert all(errors for errors in corruptions)

    def test_initial_plus_two_retries_then_failed(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(tmp_path, focus='grammar')
        cell = next(cell for cell in plan['cells'] if cell['stage'] == 'local')
        invalid = _valid_result(plan, cell)
        invalid['checked_thoroughly'] = False
        result_path = tmp_path / 'invalid.json'
        result_path.write_text(json.dumps(invalid), encoding='utf-8')
        states = []
        for _ in range(4):
            result = _run_cli(*CONFIG_ARG, 'validate-cell', '--plan', str(plan_path),
                              '--result', str(result_path))
            assert result.returncode == 1
            states.append(json.loads(result.stdout))
        assert [state['status'] for state in states[:3]] == ['retry', 'retry', 'FAILED']
        assert states[-1]['status'] == 'FAILED'
        assert states[-1]['attempt_count'] == 3
        assert states[-1]['retry_remaining'] == 0

        valid_path = tmp_path / 'valid-after-failed.json'
        valid_path.write_text(json.dumps(_valid_result(plan, cell)), encoding='utf-8')
        still_failed = _run_cli(
            *CONFIG_ARG, 'validate-cell', '--plan', str(plan_path),
            '--result', str(valid_path),
        )
        assert still_failed.returncode == 1
        assert json.loads(still_failed.stdout)['status'] == 'FAILED'

    def test_concurrent_validate_cell_updates_preserve_every_ledger_record(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(
            tmp_path, 'one\n\ntwo\n\nthree\n\nfour\n', focus='all', chunk_lines=1,
        )
        selected = [cell for cell in plan['cells'] if cell['stage'] == 'local'][:8]
        result_dir = tmp_path / 'concurrent-results'
        result_dir.mkdir()
        processes = []
        for cell in selected:
            result_path = _write_result(result_dir, cell, _valid_result(plan, cell))
            processes.append(subprocess.Popen(
                SCRIPT + [*CONFIG_ARG, 'validate-cell', '--plan', str(plan_path),
                          '--result', str(result_path), '--cell-id', cell['id']],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8',
            ))
        completed = [process.communicate(timeout=60) for process in processes]
        assert all(process.returncode == 0 for process in processes), completed
        ledger = json.loads(Path(plan['attempts_path']).read_text(encoding='utf-8'))
        assert {cell['id'] for cell in selected} <= set(ledger['cells'])
        for cell in selected:
            record = ledger['cells'][cell['id']]
            assert record['status'] == 'accepted'
            assert len(record['attempts']) == 1

    def test_reference_assessment_requires_all_batches_for_not_established(self, tmp_path):
        plan, _, _ = _prepare_plan(
            tmp_path, 'The launch was in 2025.\n', focus='grammar',
            reference_text='Operations commenced in the prior year.\n',
        )
        cell = next(cell for cell in plan['cells'] if cell['dimension'] == 'grounding')
        batch_ids = list(cell['required_batch_ids'])
        assessment = {
            'claim_key': 'launch-date', 'status': 'not-established',
            'batch_ids': batch_ids, 'completed_batch_ids': [], 'evidence': [],
        }
        invalid = _valid_result(plan, cell, reference_assessments=[assessment])
        assert any('requires all' in error for error in review_plan.validate_cell_result(plan, cell, invalid))
        assessment['completed_batch_ids'] = batch_ids
        assert review_plan.validate_cell_result(
            plan, cell, _valid_result(plan, cell, reference_assessments=[assessment])
        ) == []

        bad_evidence = {
            'schema': 'ReferenceEvidence/v1', 'reference_id': 'ref-001',
            'passage_id': 'not-in-manifest', 'location': 'line 1', 'quote': 'prior year',
        }
        assessment['status'] = 'supported'
        assessment['evidence'] = [bad_evidence]
        assert any('passage_id' in error for error in review_plan.validate_cell_result(
            plan, cell, _valid_result(plan, cell, reference_assessments=[assessment])
        ))

    def test_reference_assessments_are_mandatory_and_conclusive_status_needs_all_batches(self, tmp_path):
        plan, _, _ = _prepare_plan(
            tmp_path, 'The launch was in 2025.\n', focus='grammar',
            reference_text='The reference states a launch date.\n',
        )
        cell = next(cell for cell in plan['cells'] if cell['dimension'] == 'grounding')
        missing = _valid_result(plan, cell)
        missing.pop('reference_assessments')
        assert any('required' in error for error in review_plan.validate_cell_result(plan, cell, missing))
        for status in ('supported', 'contradicted'):
            assessment = {
                'claim_key': 'launch-date', 'status': status,
                'batch_ids': list(cell['required_batch_ids']),
                'completed_batch_ids': [], 'evidence': [],
            }
            errors = review_plan.validate_cell_result(
                plan, cell, _valid_result(plan, cell, reference_assessments=[assessment])
            )
            assert any('conclusive status requires all' in error for error in errors)

    def test_omission_sentinel_and_category_are_reserved_for_reference_coverage(self, tmp_path):
        plan, _, _ = _prepare_plan(tmp_path, 'Source text.\n', focus='grammar')
        local = next(cell for cell in plan['cells'] if cell['stage'] == 'local')
        omission = _finding(1, '[Missing from source]', 'Add omitted text.', category='omission')
        errors = review_plan.validate_cell_result(
            plan, local, _valid_result(plan, local, findings=[omission])
        )
        assert any('reference-coverage' in error for error in errors)

    @pytest.mark.parametrize(
        ('focus', 'allowed_category', 'rejected_category'),
        [
            ('grammar', 'grammar', 'style'),
            ('style', 'style', 'grammar'),
            ('logic', 'logic', 'consistency'),
            ('consistency', 'consistency', 'logic'),
        ],
    )
    def test_finding_category_must_match_exact_focus_and_stage(
        self, tmp_path, focus, allowed_category, rejected_category,
    ):
        plan, _, _ = _prepare_plan(tmp_path, 'recieve the report.\n', focus=focus)
        local = next(cell for cell in plan['cells'] if cell['stage'] == 'local')
        allowed = _valid_result(
            plan, local, findings=[_finding(category=allowed_category)],
        )
        assert review_plan.validate_cell_result(plan, local, allowed) == []

        rejected = _valid_result(
            plan, local, findings=[_finding(category=rejected_category)],
        )
        assert any('category' in error for error in review_plan.validate_cell_result(
            plan, local, rejected,
        ))

        reference_only = _valid_result(
            plan, local, findings=[_finding(category='fact')],
        )
        assert any('category' in error for error in review_plan.validate_cell_result(
            plan, local, reference_only,
        ))


class TestReducerBounds:
    def test_batches_are_deterministic_bounded_and_at_most_three_levels(self):
        inputs = [{'id': f'cell-{index:03d}', 'payload': 'x' * 250}
                  for index in range(60)]
        first = review_plan.plan_reducer_batches(inputs, max_chars=50_000, max_depth=3)
        second = review_plan.plan_reducer_batches(inputs, max_chars=50_000, max_depth=3)
        assert first == second
        assert max(batch['input_budget_chars'] for batch in first) <= 50_000
        assert max(batch['depth'] for batch in first) <= 3

    def test_oversized_item_or_nonconvergence_is_rejected(self):
        with pytest.raises(ValueError):
            review_plan.plan_reducer_batches([{'id': 'x', 'payload': 'y' * 100}], max_chars=20)
        with pytest.raises(ValueError):
            review_plan.plan_reducer_batches(
                [{'id': str(i), 'payload': 'z' * 30} for i in range(20)],
                max_chars=80, max_depth=1,
            )

    def test_actual_observations_are_batched_by_serialized_character_count(self):
        observations = [
            {'schema': 'ReducerObservation/v1', 'kind': 'term',
             'key': f'k{i}', 'value': 'x' * 700, 'normalized_value': f'k{i}',
             'locations': [{'start_line': 1, 'end_line': 1}]}
            for i in range(100)
        ]
        batches = review_plan.batch_observations(observations, max_chars=5_000)
        assert len(batches) > 1
        assert sum(len(batch['observations']) for batch in batches) == len(observations)
        assert all(batch['serialized_chars'] <= 5_000 for batch in batches)
        with pytest.raises(ValueError):
            review_plan.batch_observations(
                [{**observations[0], 'value': 'x' * 6_000}], max_chars=5_000,
            )


class TestV3Assembly:
    def test_complete_report_has_only_five_localized_fields_and_diff_is_opt_in(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(tmp_path, 'recieve the report.\n', focus='grammar', language='en')
        local = next(cell for cell in plan['cells'] if cell['stage'] == 'local')
        cells_dir = tmp_path / 'cells'
        _write_all_results(plan, cells_dir, {
            local['id']: _valid_result(plan, local, findings=[_finding()]),
        }, plan_path=plan_path)
        assembled = _run_cli(*CONFIG_ARG, 'assemble', '--plan', str(plan_path),
                             '--cells-dir', str(cells_dir))
        assert assembled.returncode == 0, assembled.stderr
        data = json.loads(assembled.stdout)
        assert data['complete'] is True
        assert data['diff_status'] == 'not_requested'
        assert 'diff' not in data
        report = data['report']
        for label in ('Original location:', 'Original text:', 'Revised text:',
                      'Exact change:', 'Reason for change:'):
            assert label in report
        assert '{{' not in report
        assert 'severity' not in report.lower()
        assert 'category' not in report.lower()
        assert 'coverage table' not in report.lower()

        with_diff = _run_cli(*CONFIG_ARG, 'assemble', '--plan', str(plan_path),
                             '--cells-dir', str(cells_dir), '--diff')
        diff_data = json.loads(with_diff.stdout)
        assert diff_data['diff_status'] == 'generated'
        assert '-recieve the report.' in diff_data['diff']
        assert '+receive the report.' in diff_data['diff']

    def test_deletion_marker_produces_a_real_deletion_in_diff(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(tmp_path, 'Delete this sentence.\nKeep this one.\n',
                                           focus='grammar', language='en')
        local = next(cell for cell in plan['cells'] if cell['stage'] == 'local')
        finding = _finding(1, 'Delete this sentence.', '[Delete this text]')
        finding['change'] = 'Delete the first sentence.'
        cells_dir = tmp_path / 'cells'
        _write_all_results(plan, cells_dir, {
            local['id']: _valid_result(plan, local, findings=[finding]),
        }, plan_path=plan_path)
        data = json.loads(_run_cli(
            *CONFIG_ARG, 'assemble', '--plan', str(plan_path),
            '--cells-dir', str(cells_dir), '--diff',
        ).stdout)
        assert data['diff_status'] == 'generated'
        assert '-Delete this sentence.' in data['diff']
        assert '[Delete this text]' not in data['diff']

    def test_partial_acceptance_never_becomes_complete_or_claims_zero_issues(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(tmp_path, focus='grammar', language='en')
        cells_dir = tmp_path / 'cells'
        cells_dir.mkdir()
        first = plan['cells'][0]
        _write_result(cells_dir, first, _valid_result(plan, first))
        for accept in (False, True):
            args = [*CONFIG_ARG, 'assemble', '--plan', str(plan_path),
                    '--cells-dir', str(cells_dir)]
            if accept:
                args.append('--accept-partial')
            data = json.loads(_run_cli(*args).stdout)
            assert data['complete'] is False
            assert data['status'] == 'partial'
            assert 'Review incomplete' in data['report']
            assert 'No issues requiring changes' not in data['report']
            assert 'Found 0 issue' not in data['report']
            assert 'Coverage Table' not in data['report']
            assert data['diff_status'] == 'not_requested'

    def test_chinese_report_and_omission_sentinel_are_localized(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(
            tmp_path, '这是一份中文报告。\n', focus='grammar', language='zh',
            reference_text='报告还应说明风险管理安排。\n',
        )
        coverage = next(cell for cell in plan['cells'] if cell['dimension'] == 'reference-coverage')
        finding = _finding(1, '[原文缺失]', '建议新增风险管理安排。', category='omission')
        finding['change'] = '新增风险管理安排。'
        finding['reason'] = '参考资料包含该项重要信息。'
        finding['fixable'] = False
        passage = plan['reference_passages'][0]
        finding['evidence'] = [{
            'schema': 'ReferenceEvidence/v1',
            'reference_id': passage['reference_id'],
            'passage_id': passage['id'],
            'location': '第1行',
            'quote': '报告还应说明风险管理安排。',
        }]
        cells_dir = tmp_path / 'cells'
        _write_all_results(plan, cells_dir, {
            coverage['id']: _valid_result(plan, coverage, findings=[finding]),
        }, plan_path=plan_path)
        data = json.loads(_run_cli(
            *CONFIG_ARG, 'assemble', '--plan', str(plan_path), '--cells-dir', str(cells_dir),
        ).stdout)
        assert data['complete'] is True
        report = data['report']
        for label in ('原文位置：', '原文：', '修改后文本：', '具体改动：', '改动原因：'):
            assert label in report
        assert '[原文缺失]' in report
        assert 'reference.md' in report
        assert passage['id'] in report
        assert 'Original text:' not in report
        assert '{{' not in report

    def test_forced_english_report_keeps_chinese_document_omission_sentinel(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(
            tmp_path, '这是一份中文报告。\n', focus='grammar', language='en',
            reference_text='报告还应说明风险管理安排。\n',
        )
        coverage = next(cell for cell in plan['cells'] if cell['dimension'] == 'reference-coverage')
        assert plan['document_language'] == 'zh'
        assert plan['resolved_language'] == 'en'
        assert coverage['text_contract']['omission_sentinel'] == '[原文缺失]'
        assert coverage['text_contract']['revised_text'] == 'document-language:zh'
        assert coverage['text_contract']['change'] == 'report-language:en'
        passage = plan['reference_passages'][0]
        finding = _finding(1, '[原文缺失]', '建议新增风险管理安排。', category='omission')
        finding.update({
            'change': 'Add the risk-management arrangements.',
            'reason': 'The reference contains material information omitted from the source.',
            'fixable': False,
            'evidence': [{
                'schema': 'ReferenceEvidence/v1',
                'reference_id': passage['reference_id'], 'passage_id': passage['id'],
                'location': 'line 1', 'quote': '报告还应说明风险管理安排。',
            }],
        })
        cells_dir = tmp_path / 'cells'
        _write_all_results(plan, cells_dir, {
            coverage['id']: _valid_result(plan, coverage, findings=[finding]),
        }, plan_path=plan_path)
        data = json.loads(_run_cli(
            *CONFIG_ARG, 'assemble', '--plan', str(plan_path), '--cells-dir', str(cells_dir),
        ).stdout)
        report = data['report']
        assert data['complete'] is True
        assert '[原文缺失]' in report
        assert 'Original location:' in report
        assert 'Exact change:' in report
        assert 'Reason for change:' in report
        assert '原文位置：' not in report

    def test_accepted_unverified_reference_result_forces_partial_assembly(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(
            tmp_path, 'The launch was in 2025.\n', focus='grammar', language='en',
            reference_text='The schedule is described in different terms.\n',
        )
        claim_cell = next(cell for cell in plan['cells'] if cell['dimension'] == 'claim-extraction')
        claim = {
            'schema': 'ReducerObservation/v1', 'kind': 'claim',
            'key': 'launch-date', 'value': 'The launch was in 2025.',
            'normalized_value': 'launch year 2025',
            'locations': [{'start_line': 1, 'end_line': 1}],
        }
        overrides = {
            claim_cell['id']: _valid_result(plan, claim_cell, observations=[claim]),
        }
        for cell in plan['cells']:
            if cell['dimension'] not in {
                'semantic-routing', 'grounding', 'reference-coverage', 'adjudication',
            }:
                continue
            assessment = {
                'claim_key': 'launch-date', 'status': 'unverified/incomplete',
                'batch_ids': list(cell.get('required_batch_ids', [])),
                'completed_batch_ids': [], 'evidence': [],
            }
            overrides[cell['id']] = _valid_result(
                plan, cell, reference_assessments=[assessment],
            )
        cells_dir = tmp_path / 'cells'
        _write_all_results(plan, cells_dir, overrides, plan_path=plan_path)
        data = json.loads(_run_cli(
            *CONFIG_ARG, 'assemble', '--plan', str(plan_path),
            '--cells-dir', str(cells_dir), '--accept-partial',
        ).stdout)
        assert data['complete'] is False
        assert data['status'] == 'partial'
        assert data['unverified_cells']
        assert 'Review incomplete' in data['report']

    def test_missing_authoritative_template_is_an_error(self, tmp_path, monkeypatch):
        plan, _, _ = _prepare_plan(tmp_path, focus='grammar')
        monkeypatch.setattr(review_plan, 'TEMPLATE_PATH', str(tmp_path / 'missing-template.md'))
        with pytest.raises(SystemExit):
            review_plan._build_report(plan, [], [], False)

    def test_diff_is_not_applicable_to_converted_input(self, tmp_path):
        plan, plan_path, _ = _prepare_plan(tmp_path, 'Clean prose.', suffix='.html', focus='grammar')
        cells_dir = tmp_path / 'cells'
        _write_all_results(plan, cells_dir, plan_path=plan_path)
        data = json.loads(_run_cli(
            *CONFIG_ARG, 'assemble', '--plan', str(plan_path), '--cells-dir', str(cells_dir), '--diff',
        ).stdout)
        assert data['complete'] is True
        assert data['diff_status'] == 'not_applicable'
        assert data['diff_reason']
        assert 'diff' not in data


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

class TestVersion:
    def test_version_flag(self):
        res = _run_cli('--version')
        assert res.returncode == 0
        assert 'review_plan' in res.stdout
