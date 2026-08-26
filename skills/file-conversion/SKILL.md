---
name: file-conversion
description: |
  Route supported local PDF and Office files or directories into one canonical Markdown bundle plus a sibling native PDF, using one source snapshot and one publication transaction. Use when both machine-readable Markdown/JSON and a high-fidelity PDF are required.
metadata:
  version: 2.0.0
---

# Create Markdown + PDF bundles

Run `scripts/pipeline.py`. This local-only router accepts the same PDF, Word,
PowerPoint, and Excel formats as `pdf-conversion`. Each bundle is:

```text
report/
├── report.md
├── report.json
├── report.pdf
├── src/report.docx
└── assets/images/       # only when Markdown extraction emits images
```

```text
/file-processing:file-conversion <file-or-directory>
  [--output-dir <directory>]
  [--formats markdown,pdf]
  [--language-normalization simplified|preserve|traditional]
  [--timestamp <ISO-date-or-aware-datetime>] [--no-frontmatter]
  [--overwrite | --rename] [--types pdf,docx,xlsx] [--no-recursive]
  [--local-document-adapter anydoc|markitdown]
  [--ocr off|auto|force] [--ocr-engine rapidocr]
  [--ocr-language ch] [--ocr-dpi 300]
  [--ocr-max-long-edge 4096] [--ocr-min-confidence 0.5]
  [--enrich-images]
  [--libreoffice-path <program-directory-or-soffice.com>]
  [--config <config.json>] [--version]
```

Omitted `--formats` means `markdown,pdf`; v1 accepts exactly that normalized
set. Use the specialized commands for a singleton format. The timestamp is
resolved once per invocation and passed unchanged to every Markdown bundle.

The router acquires the source once, stages the existing Markdown bundle
emitter and PDF provider in one owned directory, then publishes that directory
only after both required artifacts validate. A publishable loss-aware Markdown
partial is still publishable when its PDF is valid. Any hard failure publishes
nothing for that item. Batch publication remains per item, not all-files
atomic. Under identical source, stem, timestamp, and content configuration, the
Markdown/JSON/source/assets bytes match standalone `markdown-conversion`; the
router adds only the PDF.
