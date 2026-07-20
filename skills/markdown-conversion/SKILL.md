---
name: markdown-conversion
description: |
  Convert office documents, PDFs, web pages, media, and other supported files to Markdown for Obsidian or ordinary file workflows. Use for single-file, URL, or directory conversion; text extraction; image-link preservation; version/dependency checks; and explicit OKF/Cortex-ready conversion through --okf or --workspace. When OKF mode is selected, stage the converted body and continue through the reviewed okf-frontmatter Plan -> Apply workflow instead of writing final semantic metadata directly.
metadata:
  version: 4.1.0
---

# Convert files to Markdown

Use `scripts/pipeline.py` for deterministic conversion, encoding repair,
Traditional-to-Simplified Chinese conversion, optional image stripping, and
output handling.

## Interface

```text
/file-processing:markdown-conversion <file-url-or-directory>
  [--output-path <path>]
  [--no-frontmatter | --okf]
  [--workspace <cortex-root>]
  [--keep-images]
  [--overwrite | --rename]
  [--types pdf,docx] [--no-recursive]
  [--accept-partial] [--okf-run-dir <path>]
```

`--workspace` implies `--okf`. Both are mutually exclusive with
`--no-frontmatter`.

## Examples

```text
/file-processing:markdown-conversion --version
/file-processing:markdown-conversion ~/Downloads/report.pdf
/file-processing:markdown-conversion https://example.com/article.html
/file-processing:markdown-conversion ~/Downloads/slides.pptx --keep-images
/file-processing:markdown-conversion ~/Downloads/papers --types pdf
/file-processing:markdown-conversion report.pdf --okf
/file-processing:markdown-conversion report.pdf --workspace ~/knowledge-workspace
```

## Legacy/default workflow

Run:

```bash
python scripts/pipeline.py \
  --input "<source>" --output-path "<output.md>" \
  [--source "<frontmatter-source>"] [--converted-at "<ISO8601>"] \
  [--no-frontmatter] [--keep-images] [--overwrite | --rename]
```

Directory mode uses `--input-dir`. Without `--okf`, output behavior remains the
v4.0 contract: write final Markdown immediately and inject the legacy provenance
frontmatter (`source`, `converted_at`, `converted_by`) unless
`--no-frontmatter` is present.

## OKF/Cortex workflow

Run the same pipeline with `--okf`, or with `--workspace` for policy-aware
Cortex preparation. The pipeline converts into a system-temporary run, records
`source`, `converted_at`, and `converted_by`, invokes the deterministic
`okf-frontmatter prepare` boundary, and does not create the final target.

Exit `3` is the expected handoff: read the printed `run.json`, then follow the
`/file-processing:okf-frontmatter` proposal, plan, human-review, apply, and
validate workflow. Do not report conversion success until Apply returns a valid
receipt. Exit `4` is a Cortex prerequisite/policy failure and must not downgrade
to generic OKF.

For a rejected review, remove only the exact temporary run unless the user
requested `--okf-run-dir`, `--plan-only`, or retention. On failure, retain and
report the run for recovery.

## Dependencies

- `markitdown`, `chardet`, and `opencc-python-reimplemented` are required and
  auto-installed when absent.
- `doc2docx` is installed on demand for legacy `.doc` files.
- `ruamel.yaml>=0.17,<0.18` is required only for OKF mode; an incompatible
  installed version is never auto-downgraded.
- Cortex mode requires a compatible `cortex` executable on `PATH`; the plugin
  does not install Cortex.

Use `python scripts/pipeline.py --version` to report dependency status.

## Exit codes

- `0` — final legacy output written, or empty batch completed.
- `1` — conversion, input, or argument failure.
- `2` — final output already exists without `--overwrite` or `--rename`.
- `3` — OKF conversion staged and awaiting metadata review/Apply.
- `4` — Cortex CLI unavailable/incompatible, workspace invalid, or policy invalid.

## Output and paths

- Local-file output defaults beside the source; URL output defaults to the
  current directory; batch output defaults to the input directory.
- Frontmatter source paths use forward slashes.
- Images are stripped by default; `--keep-images` preserves Markdown image
  links.
- See `references/frontmatter-template.md` for the legacy provenance template
  and `references/troubleshooting.md` for conversion failures.

