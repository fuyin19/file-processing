# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**file-processing** is a Claude Code plugin (v3.5.0) containing four file processing skills:

- **markdown-conversion** (v4.0.0) — Convert documents to markdown. Python pipeline with Chinese text processing, encoding detection, and optional image stripping. Output defaults to the source file's directory; use `--output-path` to write to an Obsidian vault.
- **content-review** (v1.1.0) — Review files for grammar, typos, logic, and stylistic issues. Verify content against reference materials (fact-checking). SKILL.md-only with `references/` and `assets/`.
- **markdown-cleanup** (v1.0.0) — Clean up formatting artifacts in markitdown-converted .md files. Pure Python stdlib.
- **translate** (v1.0.0) — Translate files to a target language with optional reference-guided terminology. Hybrid architecture: Python pipeline for deterministic work, Claude for linguistic work.

## Commands

### Running Tests

```bash
# Markdown-conversion tests
python -m pytest tests/markdown-conversion/test_pipeline.py -v

# Markdown-cleanup tests
python -m pytest tests/markdown-cleanup/test_cleanup_pipeline.py -v

# Translate tests
python -m pytest tests/translate/test_translate_pipeline.py -v

# Single test or pattern
python -m pytest tests/markdown-conversion/test_pipeline.py -k "test_fix_encoding_utf8" -v
python -m pytest tests/translate/test_translate_pipeline.py -k "glossary" -v

# Categories (markdown-conversion)
python -m pytest tests/markdown-conversion/test_pipeline.py -k "overwrite or rename" -v    # output write / conflict resolution
python -m pytest tests/markdown-conversion/test_pipeline.py -k "integration or full_pipeline" -v    # end-to-end
python -m pytest tests/markdown-conversion/test_pipeline.py -k "precheck" -v     # validation
python -m pytest tests/markdown-conversion/test_pipeline.py -k "config" -v                          # config loading
```

All test files use the same pattern: `sys.path` includes the skill's `scripts/` dir, and integration tests run the pipeline as a subprocess via `SCRIPT = [sys.executable, <pipeline.py path>]`.

**Test config isolation**: All test suites pass `--config tests/<skill>/fixtures/test_config.json` via `CONFIG_ARG` to avoid touching the real `config.json` (which may contain API keys or local settings). When adding subprocess-based tests, always use `CONFIG_ARG` from the test file.

### Running Pipelines Directly

```bash
# Markdown-conversion — single file
python skills/markdown-conversion/scripts/pipeline.py --input <file> --output-path <out.md>

# Markdown-conversion — URL (markitdown fetches automatically)
python skills/markdown-conversion/scripts/pipeline.py --input <url> --output-path <out.md>

# Markdown-conversion — batch
python skills/markdown-conversion/scripts/pipeline.py --input-dir <dir> --output-path <outdir> [--types pdf,docx] [--no-recursive] [--overwrite|--rename]

# Markdown-conversion — alternate config / version
python skills/markdown-conversion/scripts/pipeline.py --config <path> --input <file> --output-path <out.md>
python skills/markdown-conversion/scripts/pipeline.py --version

# Markdown-cleanup — single file or directory
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --input <file.md>
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --input <dir>

# Markdown-cleanup — preview / selective fixers / list fixers
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --input <file.md> --dry-run --diff
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --input <file.md> --only base64_image_stubs,blank_lines
python skills/markdown-cleanup/scripts/cleanup_pipeline.py --list-fixers
```

**Exit codes (markdown-conversion)**: 0=success, 1=error, 2=output file exists (needs `--overwrite` or `--rename`). `--overwrite` replaces; `--rename` appends numeric suffix.

**Exit codes (markdown-cleanup)**: 0=success, 1=error. Output writes to same directory as source by default.

### Content Review and Translate (SKILL.md-only invocation)

```bash
# Content review
/file-processing:content-review <filepath-or-url> [--focus grammar|style|logic|consistency|all] [--language <lang>]
/file-processing:content-review <filepath-or-url> --references <ref1> [ref2...] [--focus grammar|style|logic|consistency|all]

# Translate
/file-processing:translate <filepath> --language <lang>
/file-processing:translate <filepath> --language <lang> --references <ref1> [ref2 ...]
/file-processing:translate <filepath> --language <lang> --glossary <glossary.json> --overwrite
```

## Architecture

### Skill Structure

The plugin lives in `skills/` with one subdirectory per skill. Each skill has a `SKILL.md` defining the `/file-processing:<name>` command and workflow. Script-backed skills (`markdown-conversion`, `markdown-cleanup`, `translate`) have a `scripts/` directory with Python pipelines. `content-review` is SKILL.md-only with `references/` (check criteria) and `assets/` (report template).

### Pipeline Flow (markdown-conversion)

1. **Precheck** — validates inputs and mutual exclusivity (`--input` vs `--input-dir`). URLs skip file-existence checks.
2. **Conversion** — markitdown Python API via `convert_basic()`.
3. **Image handling** — two mutually exclusive modes:
   - **Default**: `strip_images()` removes `![...](...)` and orphaned image filename lines
   - **`--keep-images`**: Pass through raw markitdown output unchanged
4. **Encoding fix** — chardet detection, UTF-8 normalization, mojibake rejection (`_mojibake_re`)
5. **Chinese conversion** — two-pass opencc t2s with stability gate
6. **Frontmatter injection** — YAML header with source path, timestamp, converter
7. **Write output** — file creation (defaults to the source file's directory) with overwrite/rename conflict resolution

### Cleanup Pipeline Flow (markdown-cleanup)

1. **Precheck** → **Load config** (merge `DEFAULT_CONFIG` with `config.json`, resolve `--only`/`--disable`)
2. **Collect .md files** → **Protect** (extract frontmatter, replace code blocks with placeholders)
3. **Run fixers** — pure functions `(text) → (text, changes)`, executed in defined order. 12 fixers total (10 enabled, 2 disabled by default). Key principle: preserve meaningful structure (PPT slide boundaries, list numbering, sections).
4. **Restore** → **Write** → **Report**

Uses only Python stdlib (`re`, `difflib`).

### Translate Pipeline Flow (translate)

1. **Prepare** — reads input (markitdown for non-.md), collects references from dirs recursively, outputs structured text
2. **Glossary generation** — Claude extracts terminology mappings from source + references. Saves as JSON (first-class output artifact)
3. **Translate** — Claude translates using the glossary
4. **QA** — deterministic structural checks: heading/paragraph/table counts, untranslated fragment detection, glossary coverage
5. **Write** — filename with language suffix (`report.zh.md`), frontmatter injection

Glossary supports JSON, CSV/TSV, and Markdown table formats via `glossary_utils.py`.

### Cross-Skill Patterns

- **Config merge**: `load_config()` reads `scripts/config.json`, auto-creates with defaults if missing, merges partial configs with `DEFAULT_CONFIG`
- **Auto-dependency management**: `_ensure_package()` installs missing packages on demand via pip
- **Gate-based error handling**: Each step validates output and calls `die()` on failure
- **Code block/frontmatter protection**: Used by both cleanup and translate pipelines
- **Path resolution**: `config.json` resolved relative to pipeline script's directory (not CWD), unless overridden by `--config`. Source paths in frontmatter have backslashes normalized to forward slashes.

## Configuration

Each skill stores its own `scripts/config.json` (gitignored):
- `skills/markdown-conversion/scripts/config.json` — currently no settings (reserved; output defaults to the source file's directory)
- `skills/markdown-cleanup/scripts/config.json` — fixer enable/disable settings
- `skills/translate/scripts/config.json` — default target language

## Permissions

`.claude/settings.json` pre-allows:
- `python -m pytest tests/markdown-conversion/test_pipeline.py *`
- `python -m pytest tests/markdown-cleanup/test_cleanup_pipeline.py *`
- `python *pipeline.py*`
- `python *cleanup_pipeline.py*`
- `python *translate_pipeline.py*`
