# Draft Frontmatter Template

Converted files use this exact five-field YAML draft by default.

## Template

```yaml
---
type: ""
title: "<local input source stem>"
description: ""
tags: []
timestamp: "<timezone-aware conversion time>"
---

[Converted Markdown content...]
```

## Field definitions

| Field | Conversion behavior |
|---|---|
| `type` | Empty string. |
| `title` | For local inputs, the original filename without its final extension; literal filename characters are preserved. URL inputs use the first H1 with a URL-derived stem/slug fallback. |
| `description` | Empty string. |
| `tags` | Empty list. |
| `timestamp` | Timezone-aware conversion time, or the exact valid `--timestamp` value. |

The converter never writes `resource`. `--timestamp` accepts an ISO date or an
RFC3339 timezone-aware datetime (`T`, seconds, and `Z` or `±HH:MM`); naive
datetimes are rejected. Use
`--no-frontmatter` only when a consumer explicitly requires body-only Markdown.
In bundle mode this flag does not remove canonical document metadata from JSON.
