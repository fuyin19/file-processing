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

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, '..', '..', 'skills', 'content-review', 'scripts'))
sys.path.insert(0, SCRIPTS)

import review_plan  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures')
CONFIG_PATH = os.path.join(FIXTURES, 'test_config.json')
CONFIG_ARG = ['--config', CONFIG_PATH]
SCRIPT = [sys.executable, os.path.join(SCRIPTS, 'review_plan.py')]


def _run_cli(*cli_args, check=True):
    """Run review_plan.py with CONFIG_ARG prepended (after --config prescan)."""
    cmd = SCRIPT + list(cli_args)
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')


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
# Dimension selection
# ---------------------------------------------------------------------------

class TestDimensions:
    def test_all_default_no_refs(self):
        assert review_plan.compute_dimensions('all', False) == ['grammar-style', 'logic-consistency']

    def test_all_with_references(self):
        assert review_plan.compute_dimensions('all', True) == [
            'grammar-style', 'logic-consistency', 'fact-check'
        ]

    def test_single_focus(self):
        assert review_plan.compute_dimensions('grammar', False) == ['grammar-style']
        assert review_plan.compute_dimensions('consistency', True) == ['logic-consistency']

    def test_unknown_focus_dies(self):
        with pytest.raises(SystemExit):
            review_plan.compute_dimensions('bogus', False)


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
    def _write_tmp(self, suffix='.md'):
        fd, path = tempfile.mkstemp(suffix=suffix)
        return fd, path

    def test_dry_run_emits_plan_json(self, tmp_path):
        src = tmp_path / 'doc.md'
        src.write_text('# Title\n\nfirst paragraph\n\nsecond paragraph\n', encoding='utf-8')
        res = _run_cli(*CONFIG_ARG, 'plan', '--input', str(src), '--dry-run')
        assert res.returncode == 0, res.stderr
        plan = json.loads(res.stdout)
        assert plan['n_chunks'] == 1
        assert plan['dimensions'] == ['grammar-style', 'logic-consistency']
        assert plan['n_cells'] == 2
        assert plan['dry_run'] is True
        # Dry-run must NOT write chunk files.
        assert not os.path.exists(plan['workspace'])

    def test_focus_filter_reduces_dimensions(self, tmp_path):
        src = tmp_path / 'doc.md'
        src.write_text('a\n\nb\n', encoding='utf-8')
        res = _run_cli(*CONFIG_ARG, 'plan', '--input', str(src), '--focus', 'logic', '--dry-run')
        assert res.returncode == 0, res.stderr
        plan = json.loads(res.stdout)
        assert plan['dimensions'] == ['logic-consistency']
        assert plan['n_cells'] == 1

    def test_references_add_fact_check(self, tmp_path):
        src = tmp_path / 'doc.md'
        ref = tmp_path / 'ref.md'
        src.write_text('a\n', encoding='utf-8')
        ref.write_text('ref content\n', encoding='utf-8')
        res = _run_cli(
            *CONFIG_ARG, 'plan', '--input', str(src),
            '--references', str(ref), '--dry-run',
        )
        assert res.returncode == 0, res.stderr
        plan = json.loads(res.stdout)
        assert 'fact-check' in plan['dimensions']
        assert plan['n_cells'] == 3  # 1 chunk x 3 dims

    def test_plan_writes_chunk_files(self, tmp_path):
        src = tmp_path / 'doc.md'
        src.write_text('# H\n\n' + '\n\n'.join(f'para {i}' for i in range(20)) + '\n', encoding='utf-8')
        res = _run_cli(*CONFIG_ARG, 'plan', '--input', str(src), '--chunk-lines', '5')
        assert res.returncode == 0, res.stderr
        plan = json.loads(res.stdout)
        ws = plan['workspace']
        assert os.path.isdir(ws)
        assert os.path.exists(os.path.join(ws, 'chunk_001.md'))
        assert plan['n_chunks'] == len(os.listdir(ws))

    def test_caps_exceeded_returns_code_2(self, tmp_path):
        # Small-cap config in a temp file: max_chunks=2.
        small_cfg = tmp_path / 'cfg.json'
        small_cfg.write_text(json.dumps({"chunk_lines": 2, "max_chunks": 2, "max_cells": 60}), encoding='utf-8')
        src = tmp_path / 'big.md'
        # Blank-line-separated paragraphs -> many atomic prose blocks -> many chunks.
        src.write_text('\n\n'.join(f'line {i}' for i in range(50)), encoding='utf-8')
        res = subprocess.run(
            SCRIPT + ['--config', str(small_cfg), 'plan', '--input', str(src), '--chunk-lines', '2'],
            capture_output=True, text=True, encoding='utf-8',
        )
        assert res.returncode == 2
        assert 'max_chunks' in res.stderr


# ---------------------------------------------------------------------------
# CLI: assemble (mocked cell results)
# ---------------------------------------------------------------------------

class TestCLIAssemble:
    def _make_plan(self, tmp_path, dims=('grammar-style', 'logic-consistency')):
        src = tmp_path / 'doc.md'
        src.write_text('# Title\n\nrecieve the report.\n\nTurnover grew 12% here and was flat there.\n', encoding='utf-8')
        res = _run_cli(*CONFIG_ARG, 'plan', '--input', str(src), '--dry-run')
        plan = json.loads(res.stdout)
        # Force the requested dims + matching cells for deterministic assemble tests.
        chunks = plan['chunks']
        cells = [
            {'dimension': d, 'chunk': c['index'], 'lines': c['start'] and f"{c['start']}-{c['end']}",
             'chunk_path': ''}
            for d in dims for c in chunks
        ]
        plan['dimensions'] = list(dims)
        plan['cells'] = cells
        plan['has_references'] = 'fact-check' in dims
        plan['file'] = str(src)
        return plan, src

    def _write_cell(self, cells_dir, dim, chunk, payload):
        with open(os.path.join(cells_dir, f"{dim}__{chunk:03d}.json"), 'w', encoding='utf-8') as f:
            json.dump(payload, f)

    def test_coverage_and_dedup_and_diff(self, tmp_path):
        plan, src = self._make_plan(tmp_path)
        cells_dir = tmp_path / 'cells'
        cells_dir.mkdir()
        plan_path = tmp_path / 'plan.json'
        plan_path.write_text(json.dumps(plan), encoding='utf-8')

        self._write_cell(cells_dir, 'grammar-style', 1, {
            'cell': {'dimension': 'grammar-style', 'chunk': 1},
            'checked_thoroughly': True,
            'findings': [
                {'severity': 'high', 'line': 3, 'quote': 'recieve', 'category': 'spelling',
                 'issue': 'Misspelling', 'suggestion': 'receive', 'fixable': True},
                # Duplicate of the next one -> should be merged by dedupe.
                {'severity': 'medium', 'line': 5, 'quote': 'Turnover', 'category': 'consistency',
                 'issue': 'dup', 'suggestion': '', 'fixable': False},
            ],
        })
        self._write_cell(cells_dir, 'logic-consistency', 1, {
            'cell': {'dimension': 'logic-consistency', 'chunk': 1},
            'checked_thoroughly': True,
            'findings': [
                {'severity': 'medium', 'line': 5, 'quote': 'Turnover', 'category': 'consistency',
                 'issue': 'revenue figure contradicts itself', 'suggestion': 'reconcile', 'fixable': False},
            ],
        })

        res = _run_cli(*CONFIG_ARG, 'assemble', '--plan', str(plan_path), '--cells-dir', str(cells_dir))
        assert res.returncode == 0, res.stderr
        out = json.loads(res.stdout)
        assert out['incomplete'] is False
        # Two findings after dedupe (the line-5 consistency dup merges).
        assert out['deduped_count'] == 2
        # The fixable spelling finding produces a diff.
        assert 'recieve' in out['diff'] and 'receive' in out['diff']
        assert 'Coverage Table' in out['report']

    def test_failed_required_cell_marks_incomplete(self, tmp_path):
        plan, src = self._make_plan(tmp_path)
        cells_dir = tmp_path / 'cells'
        cells_dir.mkdir()
        plan_path = tmp_path / 'plan.json'
        plan_path.write_text(json.dumps(plan), encoding='utf-8')

        self._write_cell(cells_dir, 'grammar-style', 1, {
            'cell': {'dimension': 'grammar-style', 'chunk': 1},
            'findings': [],
        })
        # logic-consistency result is MISSING -> FAILED required cell.

        res = _run_cli(*CONFIG_ARG, 'assemble', '--plan', str(plan_path), '--cells-dir', str(cells_dir))
        out = json.loads(res.stdout)
        assert out['incomplete'] is True
        assert 'diff' not in out  # no diff when incomplete
        assert out['failed_required_cells']

    def test_accept_partial_emits_diff_despite_failed(self, tmp_path):
        plan, src = self._make_plan(tmp_path)
        cells_dir = tmp_path / 'cells'
        cells_dir.mkdir()
        plan_path = tmp_path / 'plan.json'
        plan_path.write_text(json.dumps(plan), encoding='utf-8')

        self._write_cell(cells_dir, 'grammar-style', 1, {
            'cell': {'dimension': 'grammar-style', 'chunk': 1},
            'findings': [
                {'severity': 'high', 'line': 3, 'quote': 'recieve', 'category': 'spelling',
                 'issue': 'x', 'suggestion': 'receive', 'fixable': True},
            ],
        })
        # logic-consistency missing -> FAILED, but --accept-partial forces a diff.
        res = _run_cli(
            *CONFIG_ARG, 'assemble', '--plan', str(plan_path),
            '--cells-dir', str(cells_dir), '--accept-partial',
        )
        out = json.loads(res.stdout)
        assert out['incomplete'] is False  # forced partial
        assert 'diff' in out

    def test_identity_mismatch_marks_failed(self, tmp_path):
        plan, src = self._make_plan(tmp_path)
        cells_dir = tmp_path / 'cells'
        cells_dir.mkdir()
        plan_path = tmp_path / 'plan.json'
        plan_path.write_text(json.dumps(plan), encoding='utf-8')

        self._write_cell(cells_dir, 'grammar-style', 1, {
            'cell': {'dimension': 'logic-consistency', 'chunk': 99},  # wrong identity
            'findings': [],
        })
        self._write_cell(cells_dir, 'logic-consistency', 1, {
            'cell': {'dimension': 'logic-consistency', 'chunk': 1},
            'findings': [],
        })
        res = _run_cli(*CONFIG_ARG, 'assemble', '--plan', str(plan_path), '--cells-dir', str(cells_dir))
        out = json.loads(res.stdout)
        # grammar-style cell FAILED (identity mismatch) -> required -> incomplete
        assert out['incomplete'] is True


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

class TestVersion:
    def test_version_flag(self):
        res = _run_cli('--version')
        assert res.returncode == 0
        assert 'review_plan' in res.stdout
