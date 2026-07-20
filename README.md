# file-processing

A Claude Code plugin for document conversion, OKF/Cortex frontmatter preparation, content review, markdown cleanup, and translation.

## Installation
```bash
claude skill add /path/to/file-processing
```

Or manually place the plugin directory in your `.claude/skills/` folder.

## Skills

### markdown-conversion (v4.1.0)

Convert various file formats (PDF, DOCX, PPTX, URLs, etc.) to markdown. Includes Chinese text processing and encoding detection. Output defaults to the source file's directory; use `--output-path` to store in Obsidian.

```
/file-processing:markdown-conversion ~/Downloads/report.pdf
/file-processing:markdown-conversion https://example.com/article.html
/file-processing:markdown-conversion ~/Downloads/papers --types pdf
/file-processing:markdown-conversion ~/Downloads/report.pdf --okf
/file-processing:markdown-conversion ~/Downloads/report.pdf --workspace ~/knowledge-workspace
```

**Supported formats:** .pdf, .docx, .doc, .pptx, .ppt, .xlsx, .xls, .html, .csv, .json, .jsonl, .xml, .epub, .jpg/.jpeg, .png, .gif, .mp3, .wav, .mp4, .zip, .txt, .rtf, .odt, .ods, .odp, and HTTP/HTTPS URLs.

 See [skills/markdown-conversion/SKILL.md](skills/markdown-conversion/SKILL.md) for full documentation.

### okf-frontmatter (v1.0.0)

Prepare, repair, validate, and safely apply reviewed YAML frontmatter for
portable OKF Markdown or a Cortex 2.1 workspace. Existing metadata and Markdown
content are round-tripped; semantic proposals are sealed into a content-addressed
plan before writing.

```
/file-processing:okf-frontmatter ~/Documents/note.md
/file-processing:okf-frontmatter ~/Documents/notes --plan-only
/file-processing:okf-frontmatter ~/Documents/note.md --workspace ~/knowledge-workspace
```

Generic mode requires `ruamel.yaml>=0.17,<0.18` (installed when absent). Cortex
mode additionally requires a compatible `cortex` executable on `PATH`; the
plugin never installs Cortex or writes directly to an active bundle.

See [skills/okf-frontmatter/SKILL.md](skills/okf-frontmatter/SKILL.md) for the
reviewed `prepare -> plan -> apply -> validate` workflow.

### content-review (v1.0.0)

Review documents for grammar, typos, logic issues, and verify content against reference materials (fact-checking). Produces structured reports with unified diffs.
```
/file-processing:content-review ~/Documents/report.md
/file-processing:content-review ~/Documents/summary.md --references ~/Documents/source.pdf
/file-processing:content-review ~/Downloads/article.pdf --focus grammar --language zh
```

**Supported formats:** .md, .txt, .html, .rtf, .docx, .pdf

 See [skills/content-review/SKILL.md](skills/content-review/SKILL.md) for full documentation.

### markdown-cleanup (v1.0.0)

Clean up formatting artifacts in markitdown-converted .md files -- removes base64 blobs, empty rows, dead links, and other noise while preserving meaningful structure.
```
/file-processing:markdown-cleanup ~/Documents/report.md
/file-processing:markdown-cleanup ~/Obsidian\ Vault/03-Projects --dry-run --diff
```

No external dependencies (只 Python stdlib). 12 fixers: 9 enabled by default, 3 opt-in.

 See [skills/markdown-cleanup/SKILL.md](skills/markdown-cleanup/SKILL.md) for full documentation.
 See [references/fixer-reference.md](references/fixer-reference.md) for detailed fixer documentation.

 examples.
### translate (v1.0.0)

Translate files to a target language with optional reference-guided terminology. Supports direct translation and reference-guided translation with auto-generated glossary and and structured QA verification.

```
/file-processing:translate ~/Documents/report.md --language zh
/file-processing:translate ~/Downloads/article.pdf --language en
/file-processing:translate ~/Documents/meeting-notes.md --language zh --references ~/Documents/parallel-zh-summary.md
/file-processing:translate ~/Documents/report.md --language zh --references ~/Documents/reference-folder/
```

**Supported formats:** All formats supported by markdown-conversion (via markitdown), plus .md and .txt. Non-.md files are converted via markitdown first.

 See [skills/translate/SKILL.md](skills/translate/SKILL.md) for full documentation.
