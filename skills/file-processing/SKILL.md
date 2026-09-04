---
name: file-processing
description: Inspect and explain the shared local runtime used by the markdown-conversion, pdf-conversion, and file-conversion skills. Use when diagnosing an incomplete or relocated unified file-processing installation; this is a read-only runtime carrier, not a conversion command.
metadata:
  version: 1.1.0
---

# File-processing shared runtime

This skill carries the shared Python runtime for the three conversion skills. It
has no unified conversion CLI. Use the dedicated conversion skill for actual
file processing.

The supported layout places every dependency under one skills root:

- markdown-conversion requires sibling file-processing; bundle output also
  requires the independent sibling anti-entropy-core.
- pdf-conversion requires siblings file-processing and
  markdown-conversion; bundle output also requires anti-entropy-core.
- file-conversion requires siblings file-processing,
  markdown-conversion, and anti-entropy-core.

## Read-only installation diagnosis

Derive the skills root from this file's parent directory. Inspect that root
without copying, repairing, installing, or running a conversion:

1. Confirm this SKILL.md reports version 1.1.0.
2. Confirm scripts/ contains ordinary, non-link files named
   runtime_layout.py, native_paths.py, conversion_runtime.py,
   libreoffice_pdf.py, pdf_validation_worker.py, and
   anti_entropy_core_adapter.py.
3. Confirm each required sibling above has an ordinary SKILL.md under the
   same skills root. Check anti-entropy-core only for routes that emit a
   knowledge-unit bundle.
4. Report the actual skills root, this carrier version, each expected path, and
   whether the entry is an ordinary file. Treat a symlink, Windows reparse
   point, missing file, directory in place of a file, or resolved path outside
   that skills root as an incomplete installation.

When any check fails, restore the complete unified installation. Do not search
the current directory, another checkout, PYTHONPATH, or another skills root
for replacement runtime files.
