---
name: markdown-conversion
description: |
  Convert office documents, PDFs, web pages, media, and other supported files to Markdown for Obsidian or ordinary file workflows. Use for single-file, URL, or directory conversion, text extraction, deterministic draft YAML frontmatter, version/dependency checks, and output conflict handling.
metadata:
  version: 5.0.0
---

# Convert files to Markdown

Use `scripts/pipeline.py` for deterministic conversion, encoding repair,
Traditional-to-Simplified Chinese conversion, image-marker removal, draft YAML
frontmatter, and output handling.

## Interface

```text
/file-processing:markdown-conversion <file-url-or-directory>
  [--output-path <path>]
  [--timestamp <ISO-date-or-aware-datetime>]
  [--no-frontmatter]
  [--overwrite | --rename]
  [--types pdf,docx] [--no-recursive]
```

## Examples

```text
/file-processing:markdown-conversion --version
/file-processing:markdown-conversion ~/Downloads/report.pdf
/file-processing:markdown-conversion https://example.com/article.html
/file-processing:markdown-conversion ~/Downloads/report.pdf --timestamp 2026-07-22
/file-processing:markdown-conversion ~/Downloads/papers --types pdf
```

## Workflow

Run:

```bash
python scripts/pipeline.py \
  --input "<source>" --output-path "<output.md>" \
  [--timestamp "<ISO-date-or-aware-datetime>"] \
  [--no-frontmatter] [--overwrite | --rename]
```

Directory mode uses `--input-dir`. Every conversion path runs the same
deterministic sequence:

1. Convert with MarkItDown.
2. Remove inline Markdown image markers and orphan image-filename lines.
3. Normalize encoding and reject known mojibake.
4. Convert Traditional Chinese to Simplified Chinese.
5. Inject draft frontmatter unless `--no-frontmatter` was supplied.
6. Write the final Markdown directly.

## Draft frontmatter

The default frontmatter has exactly five fields:

```yaml
---
type: ""
title: "<first H1 or source stem>"
description: ""
tags: []
timestamp: "<conversion timestamp>"
---
```

`title` uses the first H1 after conversion, falling back to the input source
stem. `type` and `description` remain empty and `tags` remains an empty list.
The pipeline never writes a `resource` field.

By default, `timestamp` is the timezone-aware conversion time. `--timestamp`
accepts an ISO date or an RFC3339 timezone-aware datetime (`T`, seconds, and
`Z` or `±HH:MM`) and preserves the supplied
value exactly. Naive datetimes are rejected.

## Dependencies

- `markitdown`, `chardet`, and `opencc-python-reimplemented` are required and
  auto-installed when absent.
- `doc2docx` is installed on demand for legacy `.doc` files.

Use `python scripts/pipeline.py --version` to report dependency status.

## Exit codes

- `0` — output written, or an empty batch completed.
- `1` — conversion, input, timestamp, or other validation failure.
- `2` — output already exists without `--overwrite` or `--rename`.

## Output and paths

- Local-file output defaults beside the source; URL output defaults to the
  current directory; batch output defaults to the input directory.
- MarkItDown inline image markers and orphan image-filename lines are always
  removed; there is no preservation switch.
- See `references/frontmatter-template.md` for field details and
  `references/troubleshooting.md` for conversion failures.
