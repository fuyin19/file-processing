# Frontmatter Template

Converted files include YAML frontmatter injected by `scripts/pipeline.py`.

## Template

```yaml
---
source: "{source}"
converted_at: "{converted_at}"
converted_by: "markitdown"
---

[Converted markdown content...]
```

## Field Definitions

| Field | Value | Example |
|-------|-------|---------|
| `source` | Absolute path to original file, forward slashes | `C:/Users/user/Downloads/report.pdf` |
| `converted_at` | ISO 8601 timestamp | `2026-03-21T14:30:00` |
| `converted_by` | Fixed: `markitdown` | `markitdown` |

Skip frontmatter with `--no-frontmatter`.
