---
name: markdown-conversion
description: |
  Convert office documents, PDFs, web pages, and other files to markdown for storage in Obsidian.
  Use this skill whenever the user wants to:
  - Convert Word documents (.docx, .doc), PDFs, Excel files, PowerPoints, or other documents to markdown
  - Save converted documents to their Obsidian vault
  - Extract text content from files for note-taking or archiving
  - Transform document formats for Obsidian compatibility
  - Convert web pages or URLs to markdown and save to Obsidian
  Even if they don't explicitly say "markitdown" or "convert to markdown", if they mention wanting to read, extract, or archive documents in Obsidian, use this skill.
  Also use this skill when the user asks about the markdown-conversion version, installed dependencies, or whether the skill is configured correctly.
metadata:
  version: 4.0.0
---

# MarkItDown Skill for Obsidian

Convert office documents and PDFs to markdown. Output defaults to the source file's directory; use `--output-path` to write to an Obsidian vault or any other location.

## Overview

This skill uses the **markitdown Python package** to convert documents to markdown, then passes the output through `scripts/pipeline.py` which automatically fixes encoding, converts Traditional Chinese to Simplified Chinese, injects YAML frontmatter, and writes the result next to the source file by default.

## Commands

### `/file-processing:markdown-conversion --version`

Show the current installed skill version and dependency status.

Run:
```bash
python scripts/pipeline.py --version
```

Report the output to the user.

### `/file-processing:markdown-conversion <filepath-or-url-or-directory> [options]`

Convert a document, web page, or directory of files to markdown and save to Obsidian vault. When the input is a directory, batch-converts all supported files (each file processed independently; errors don't stop the batch).

**Arguments:**
- `filepath-or-url-or-directory` (required): Path to a local file, an HTTP/HTTPS URL, or a directory to batch-convert
- `--output-path`: Output file path (single file) or directory (batch). Defaults to the source file's directory (single local file), the current working directory (URL), or the input directory (batch)
- `--no-frontmatter`: Skip adding YAML frontmatter
- `--keep-images`: Preserve markdown image links in output (default: images are stripped)

**Batch mode arguments** (when input is a directory):
- `--types pdf,docx`: Only convert specific file types (default: all supported)
- `--no-recursive`: Only convert top-level files (default: recursive)
- `--overwrite`: Overwrite existing output files
- `--rename`: Rename output if file exists (append timestamp)

**Image handling modes:**
- Default (no flags): All image markers (`![...](...)`) are stripped from the output
- `--keep-images`: Preserve original image markers as-is

**Examples:**
```
/file-processing:markdown-conversion --version
/file-processing:markdown-conversion ~/Downloads/report.pdf
/file-processing:markdown-conversion https://example.com/article.html
/file-processing:markdown-conversion ~/Documents/notes.docx --output-path ~/Documents/work/meeting-notes.md
/file-processing:markdown-conversion ~/Downloads/article.pdf --no-frontmatter
/file-processing:markdown-conversion ~/Downloads/slides.pptx --keep-images
/file-processing:markdown-conversion ~/Downloads/report.pdf --output-path ~/Documents/notes/report.md
/file-processing:markdown-conversion ~/Downloads/papers --types pdf
/file-processing:markdown-conversion ~/Documents/archive --no-recursive
/file-processing:markdown-conversion ~/Downloads/mixed --output-path ~/Documents/imported --overwrite
```

## Supported File Types

| Category | Extensions | Notes |
|----------|-----------|-------|
| **Documents** | .pdf, .docx, .doc, .pptx, .ppt, .xlsx, .xls | .doc auto-converted via doc2docx |
| **Web/Data** | .html, .csv, .json, .jsonl, .xml, .epub | |
| **Media** | .jpg/.jpeg, .png, .gif | EXIF extraction + OCR if deps installed |
| | .mp3, .wav, .mp4 | Metadata extraction + transcription |
| **Other** | .zip | Iterates through contents |
| | .txt, .rtf, .odt, .ods, .odp | |

## Configuration

No configuration is required. Converted files are written next to the source file by default. To write to a specific location (e.g., an Obsidian vault), pass `--output-path`.

For troubleshooting common issues, see `references/troubleshooting.md`.

## Output FormatConverted files include YAML frontmatter — see `references/frontmatter-template.md` for the template and field definitions.

## Prerequisites

**Required:**
- `markitdown` — document conversion (auto-installed if missing)
- `chardet` — encoding detection (auto-installed if missing)
- `opencc-python-reimplemented` — Traditional→Simplified Chinese conversion (auto-installed if missing)

**Optional:**
- `doc2docx` — legacy `.doc` format support (auto-installed if missing)

## Workflow

### Step 1: Conversion & Pipeline

```bash
python scripts/pipeline.py \
  --input "<source_file_or_url>" \
  --source "<absolute_original_path_with_forward_slashes_or_url>" \
  [--output-path "<output_path.md>"] \
  --converted-at "<ISO8601_now>" \
  [--no-frontmatter] \
  [--keep-images] \
  [--overwrite | --rename]
```

Pipeline handles conversion (via markitdown Python API, which natively supports URLs), encoding fix, image stripping, T→S Chinese conversion, and output write in one step.

**Batch mode** (convert entire directory):
```bash
python scripts/pipeline.py \
  --input-dir "<source_directory>" \
  [--output-path "<output_dir>"] \
  --converted-at "<ISO8601_now>" \
  [--no-recursive] \
  [--types pdf,docx] \
  [--overwrite | --rename]
```

Gate:
- Exit code `0` — success; read the `[OK]` confirmation from stdout and report to user
- Exit code `1` — error; surface the stderr message to the user and stop
- Exit code `2` — output file already exists; prompt user: overwrite, rename, or cancel; then re-invoke pipeline.py with `--overwrite` or `--rename`

### Success

Report the `[OK]` line from pipeline.py stdout to the user.

## Path Resolution

- **Input paths:** Relative paths resolved against current working directory; absolute paths used as-is
- **Output paths:** Defaults to the source file's directory (`<source_dir>/<filename>.md`); URLs default to the current working directory (`./<slug>.md`). Override with `--output-path`. If the source directory is not writable, pass `--output-path` pointing to a writable location.
- **Path format:** Forward slashes in frontmatter for cross-platform compatibility

