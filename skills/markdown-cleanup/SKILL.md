---
name: markdown-cleanup
description: |
  Clean up formatting artifacts in markdown files converted by markitdown.
  Use this skill whenever the user wants to:
  - Fix recurring formatting issues in markitdown-converted .md files
  - Remove base64 image stubs, print metadata, XML leakage
  - Remove orphaned image references from PPTX conversions
  - Clean up excessive blank lines, empty table rows, dead TOC links
  - Fix broken Word hyperlinks, renumber lists
  Even if they don't say "cleanup", if they mention fixing formatting
  in their markdown files, cleaning up conversion artifacts, or tidying .md files, use this skill.
metadata:
  version: 1.0.0
---

# Markdown Cleanup

Clean up formatting artifacts in markdown files that were converted using markitdown. Runs a series of fixers that remove noise (base64 blobs, empty rows, dead links) while preserving meaningful document structure (PPT slide boundaries, frontmatter, code blocks, actual content).

## Design Principles

- **Preserve meaningful structure** — PPT slide boundaries, document sections, tables with content, list numbering are kept by default
- **Never remove meaningful content** — only strips non-renderable/noise artifacts
- **Conservative defaults** — aggressive fixers (list renumbering, broken link repair, slide comment removal) disabled by default, available via `--only`

## Commands

### `/file-processing:markdown-cleanup <path> [options]`

Clean up one or more markdown files. `<path>` can be a single `.md` file or a directory (auto-detected).

**Arguments:**
- `path` (required): Path to a `.md` file or directory of `.md` files
- `--dry-run`: Show what would change without modifying files
- `--diff`: Show unified diff of changes
- `--only <fixers>`: Run ONLY these fixers (comma-separated)
- `--disable <fixers>`: Disable these fixers (comma-separated)
- `--output-path <path>`: Write output to a specific path (single file only; default: in-place)
- `--no-recursive`: Only process top-level files in directory (default: recursive)

**Examples:**
```
/file-processing:markdown-cleanup ~/Documents/report.md
/file-processing:markdown-cleanup ~/Obsidian\ Vault/03-IBD --dry-run --diff
/file-processing:markdown-cleanup ~/Downloads/slides.md --only base64_image_stubs,blank_lines
/file-processing:markdown-cleanup ~/vault/project/ --disable blank_lines
/file-processing:markdown-cleanup ~/vault/slides.md --only broken_word_hyperlinks,list_numbering
```

## Available Fixers

### Enabled by Default (safe — no meaningful content loss)

| Fixer | What it removes |
|-------|----------------|
| `base64_image_stubs` | `![...](data:image/...;base64,...)` unrenderable blobs |
| `empty_pptx_notes` | Empty `### Notes:` headings (preserves notes with actual content) |
| `orphaned_image_refs` | Broken image links to extracted PPTX media (`Image0.jpg`, `图片201.jpg`) |
| `print_metadata` | `JOBNAME:`, `Mark Trace:` typesetting traces |
| `xml_data_leakage` | Raw XBRL/XML identifier blocks |
| `empty_table_rows` | Table rows where all cells are empty |
| `dead_toc_links` | Dead anchor links (`#_Toc*`, `#bookmark*`) → converted to plain text |
| `backslash_escapes` | `\_` → `_`, `\*` → `*` (outside code blocks) |
| `blank_lines` | Collapse 3+ consecutive blank lines → 1 blank line |

### Disabled by Default (may alter meaning — use via `--only`)

| Fixer | What it does | Why disabled by default |
|-------|-------------|------------------------|
| `broken_word_hyperlinks` | Fix `[at **www.x.com** text](atwww.x.comtext)` → `[text](http://www.x.com)` | Display text may be meaningful as-is |
| `list_numbering` | Merge consecutive numbered lists that restart from 1 | Restart is often intentional (separate Q&A sections) |
| `slide_comments` | Remove `<!-- Slide number: N -->` PPTX boundary markers | These ARE the slide structure — removing loses slide boundaries |

## Workflow

### Step 1: Run cleanup pipeline

```bash
python scripts/cleanup_pipeline.py \
  --input "<file_or_dir>" \
  [--dry-run] [--diff] \
  [--only <fixer1,fixer2>] [--disable <fixer1>] \
  [--no-recursive] [--output-path <path>]
```

Gate:
- Exit code `0` — success; report the `[OK]` lines from stdout
- Exit code `1` — error; surface the stderr message to the user

### Step 2: Report results

Report the summary line(s) from stdout. If `--diff` was used, the user can see the detailed changes.

## Configuration

Settings are stored in `scripts/config.json`. Edit this file to change default fixer behavior.

Each fixer can be toggled on/off. CLI `--only` and `--disable` flags override config settings.

For detailed fixer documentation with examples, see `references/fixer-reference.md`.

## Prerequisites

No external dependencies — uses only Python standard library (`re`, `difflib`).

## Safety

- **Frontmatter preserved**: YAML `---` blocks at file start are never modified
- **Code blocks protected**: Fenced (` ``` `) and inline (`` ` ``) code blocks are extracted before fixers run and restored after
- **In-place by default**: Output writes to the same directory as source; use `--dry-run` to preview first
