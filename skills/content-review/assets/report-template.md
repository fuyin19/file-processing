# Content Review Report Templates

Use the appropriate template based on the review mode.

## Standalone Review

```markdown
# Content Review: <filename>

## Summary
- **Overall quality:** Good / Needs attention / Poor
- **Issues found:** X grammar, Y logic, Z consistency, W style
- **Passes completed:** 2 (surface + deep)

## Grammar & Spelling
| # | Location | Issue | Suggestion |
|---|----------|-------|------------|
| 1 | Line 42 | "recieve" | "receive" |

## Style
| # | Location | Issue | Suggestion |
|---|----------|-------|------------|
| 1 | Line 15 | Double space: "the  report" | "the report" |

## Logic & Consistency
| # | Location | Issue | Suggestion |
|---|----------|-------|------------|
| 1 | Line 15 vs Line 87 | "revenue grew 12%" vs "revenue was flat" | Reconcile conflicting statements |
```

If a section has zero issues, omit it entirely.

## Verification Review (with `--references`)

```markdown
# Content Review: <filename>
## Verification against: <ref1>, <ref2>

## Summary
- **Overall quality:** Good / Needs attention / Poor
- **Issues found:** X grammar, Y logic, Z consistency, W style
- **Cross-reference:** A confirmed, B conflicts, C unsupported
- **Passes completed:** 2 (surface + deep + cross-reference)

## Grammar & Spelling
| # | Location | Issue | Suggestion |
|---|----------|-------|------------|
| 1 | Line 42 | "recieve" | "receive" |

## Style
| # | Location | Issue | Suggestion |
|---|----------|-------|------------|
| 1 | Line 15 | Double space: "the  report" | "the report" |

## Logic & Consistency
| # | Location | Issue | Suggestion |
|---|----------|-------|------------|
| 1 | Line 15 vs Line 87 | Conflicting revenue figures | Reconcile |

## Cross-Reference Issues
| # | Location | Issue | Reference Source |
|---|----------|-------|-----------------|
| 1 | Line 23 | "2025 launch" contradicts ref | ref1.md Line 45: "2026 launch" |
| 2 | Line 56 | Claim has no basis in references | Not found in any reference |
| 3 | (omission) | Key metric from ref not mentioned | ref2.pdf: "Q3 revenue: $4.2M" |
```

If a section has zero issues, omit it entirely.
