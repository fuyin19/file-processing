---
name: pdf-conversion
description: |
  Convert supported local PDF and Office files or directories to one native, high-fidelity multipage PDF per input using an identity-bound source snapshot and a private LibreOffice process. Use for PDF-preserving copy/validation and Word, PowerPoint, or Excel PDF export.
metadata:
  version: 1.0.0
---

# Convert local files to PDF

Run `scripts/pipeline.py`. This command accepts only local `.pdf`, Word
(`.doc/.docx/.docm`), PowerPoint (`.ppt/.pptx/.pptm/.pps/.ppsx`), and Excel
(`.xls/.xlsx/.xlsm/.xlsb`) inputs. It rejects URLs, template formats, `.ppsm`,
encrypted/password-prompt inputs, and unsupported file types.

```text
/file-processing:pdf-conversion <file-or-directory>
  [--output-mode bundle|pdf]
  [--output-dir <directory> | --output-path <file.pdf>]
  [--overwrite | --rename]
  [--types pdf,docx,xlsx] [--no-recursive]
  [--libreoffice-path <program-directory-or-soffice.com>]
  [--config <config.json>] [--version]
```

`bundle` is the default and writes `<stem>/<stem>.pdf` plus
`<stem>/src/<original-basename>`. For one input, an `--output-path` ending in
`.pdf` implies direct PDF mode; direct mode writes exactly one PDF. Batch
`--output-path` is a deprecated alias for `--output-dir`.

Office conversion runs one no-shell LibreOffice process with a private profile,
work/output/temp directories, a deadline, bounded diagnostics, macro security
set to Very High, online update disabled, and automatic Writer/Calc link update
disabled where supported. These controls reduce exposure but are not an OS
sandbox and do not promise that a document cannot access external resources.
The pipeline neither installs nor repairs LibreOffice.

Every output passes a separate bounded structural validation worker. PDF input
bypasses LibreOffice and is copied exactly from the acquired source snapshot.
Publication uses an identity-aware no-replace move; overwrite retains the
documented two-move caveat and is not crash-atomic.
