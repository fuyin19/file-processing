---
name: pdf-conversion
description: |
  Convert supported local PDF and Office files or directories to one native, high-fidelity multipage PDF per input using an identity-bound source snapshot and a private LibreOffice process. Use for PDF-preserving copy/validation and Word, PowerPoint, or Excel PDF export.
metadata:
  version: 2.0.2
---

# Convert local files to PDF

The conversion runtime is installed as the ordinary sibling
file-processing skill under the same skills root. The pipeline accepts only
that sibling's ordinary, non-link runtime files and never falls back to the
current directory, a source checkout, PYTHONPATH, or another skills root.
Restore the complete unified installation when this preflight reports a
missing, linked, reparsed, or escaping dependency.

Bundle routes additionally require the independently installed
anti-entropy-core skill, exactly Core 1.2.1 with ABI
anti-entropy-core.runner/v1. By default the pipeline selects only
anti-entropy-core/scripts/knowledge_unit_runner.py under the same skills root.
ANTI_ENTROPY_CORE_RUNNER is an optional absolute-path override for a different
installation root; an explicit empty, invalid, nonordinary, ABI-mismatched or
version-mismatched value fails without fallback. Bundle Core preflight happens
before explicit config loading, providers or stages; conversion never creates a
config file, and one invocation keeps the same runner.
Each conversion skill carries a local standard-library Core client. Core
completes only caller-owned disposable stages; conversion and publication
remain in the conversion skills.

The PDF runtime also requires the sibling markdown-conversion skill for safe bundle target selection.

Direct PDF mode and help/version do not require Core.


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

Omitting `--config` uses a fresh independent copy of the built-in defaults in
memory and does not read or create any fixed config file. An explicit config
path may be relative or absolute and may be inside or outside the skill
directory; a stable path outside the installed skill is recommended across
upgrades. The explicit path must already name an ordinary regular non-link/
reparse file containing strict UTF-8 JSON with an object root. Invalid paths,
content, `pdf_conversion`, or its `validation` block fail with exit code `1`,
without fallback or a config write. Partial blocks merge over defaults, unknown
keys are preserved, and CLI values take precedence. `--version` validates an
explicit config by the same loader contract; omission and `--help` perform no
config-file read or creation.

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
Publication uses a no-replace move for absent targets. Overwrite has no backup
or automatic rollback: regular files use the OS replace operation, while
bundle directories remove the selected target before moving the completed
stage into place. A failed publication retains the owned stage at the reported
path for manual handling.
