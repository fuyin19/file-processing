# Frontmatter Template

Converted files include this legacy provenance frontmatter when neither
`--no-frontmatter` nor `--okf` is selected. OKF mode stages the converted body
and delegates reviewed semantic metadata to `/file-processing:okf-frontmatter`.

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

Do not treat this three-field template as OKF-ready: OKF/Cortex preparation also
requires semantic authoring fields such as `type`, `title`, `description`, and
`timestamp`, plus any active Cortex policy fields.
