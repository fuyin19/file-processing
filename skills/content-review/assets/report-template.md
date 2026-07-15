# Content Review Report Renderer

This asset is the authoritative user-facing renderer source. The assembler
selects blocks by resolved report language and substitutes `{{...}}` tokens.
Omit an optional block rather than leaving an empty heading.

## Report blocks

<!-- REPORT:ZH -->
# 内容审阅：{{filename}}

{{status_notice}}{{summary_line}}{{findings}}{{diff_section}}
<!-- /REPORT:ZH -->

<!-- REPORT:EN -->
# Content Review: {{filename}}

{{status_notice}}{{summary_line}}{{findings}}{{diff_section}}
<!-- /REPORT:EN -->

## Complete-run summary blocks

<!-- SUMMARY:ZH -->
发现 {{finding_count}} 个需要修改的问题。

<!-- /SUMMARY:ZH -->

<!-- SUMMARY:EN -->
Found {{finding_count}} issue(s) that require changes.

<!-- /SUMMARY:EN -->

## Finding blocks

<!-- FINDING:ZH -->
## 问题 {{index}}

- **原文位置：** {{location}}
- **原文：** {{original_text}}
- **修改后文本：** {{revised_text}}
- **具体改动：** {{change}}
- **改动原因：** {{reason}}

<!-- /FINDING:ZH -->

<!-- FINDING:EN -->
## Issue {{index}}

- **Original location:** {{location}}
- **Original text:** {{original_text}}
- **Revised text:** {{revised_text}}
- **Exact change:** {{change}}
- **Reason for change:** {{reason}}

<!-- /FINDING:EN -->

## No-finding blocks (complete runs only)

<!-- NO_FINDINGS:ZH -->
未发现需要修改的问题。
<!-- /NO_FINDINGS:ZH -->

<!-- NO_FINDINGS:EN -->
No issues requiring changes were found.
<!-- /NO_FINDINGS:EN -->

## Partial-run warning blocks

<!-- PARTIAL:ZH -->
> **审阅未完成。** 以下范围未被完整核验：{{uncovered_scope}}。本报告中的零问题或问题数量不代表完整审阅结论。

<!-- /PARTIAL:ZH -->

<!-- PARTIAL:EN -->
> **Review incomplete.** The following scope was not fully verified: {{uncovered_scope}}. Zero findings or the count below is not a complete-review conclusion.

<!-- /PARTIAL:EN -->

For a partial run, render the partial warning, then render any validated findings.
Do not render `SUMMARY` or `NO_FINDINGS` when the partial run has zero findings.
Do not expose coverage tables, severity, categories, cell state, or orchestration
details beyond the short `uncovered_scope` notice.

## Optional diff blocks

Render these only when the user passed `--diff` and the direct input is `.md`,
`.markdown`, or `.txt`. Never present a canonical-conversion diff as a patch for
DOCX, PDF, HTML, RTF, or URL input.

<!-- DIFF:ZH -->
## 建议修改 Diff

```diff
{{diff}}
```
<!-- /DIFF:ZH -->

<!-- DIFF:EN -->
## Suggested Diff

```diff
{{diff}}
```
<!-- /DIFF:EN -->

For inapplicable converted or URL inputs, keep `diff_section` empty and record
`diff_status:not_applicable` plus its reason in structured output rather than in
the default report.

## Field normalization

- `original_text` must be verbatim. Use `[原文缺失]` / `[Missing from source]`
  (selected by document language) only for a document-level omission.
- `revised_text` is complete replacement or insertion text. For deletion use
  `[删除该文本]` / `[Delete this text]`.
- When safe final wording is unavailable, use `需作者确认；建议方向：…` /
  `Author confirmation required; suggested direction: ...`.
- `change` states the concrete delta only; `reason` explains why.
- Reference reasons include the validated file and passage/page/line citation.
