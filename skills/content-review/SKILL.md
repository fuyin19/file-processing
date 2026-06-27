---
name: content-review
description: |
  Review and verify files for grammatical, logical, factual, and stylistic issues.
  Use this skill whenever the user wants to:
  - Check a document for grammar, spelling, or typo errors
  - Review writing quality, style, or readability
  - Find logical issues, contradictions, or inconsistencies in a document
  - Verify a document against reference materials (fact-checking)
  - Check if content goes beyond or contradicts its source references
  - Audit a file for content quality
  Even if they don't say "content review", if they mention proofreading, fact-checking,
  verifying against sources, checking writing quality, or finding issues in a document, use this skill.
metadata:
  version: 1.1.0
---

# Content Review

Review files for grammatical, logical, stylistic, and factual issues. Supports standalone review and reference-based verification. Produces a structured markdown report with a unified diff of suggested fixes.

## Markitdown Artifact Awareness

Many `.md` files originate from markitdown conversion. The following are **known conversion artifacts** and should NOT be reported as issues:

- Orphaned image filenames on standalone lines (e.g., `image1.png`, `photo.jpg`)
- Markdown image syntax (`![...](...)`)
- Broken or malformed tables (merged cells, misaligned columns)
- YAML frontmatter blocks (`---` delimited with `source:`, `converted_at:`, `converted_by:` fields)
- Extra blank lines or collapsed sections from image removal
- Encoding artifacts already handled by the pipeline

Only flag these if the user explicitly asks about formatting or conversion quality.

## Commands

### `/file-processing:content-review <filepath-or-url> [options]`

Review a file for content quality issues. Performs an automatic two-pass review (surface issues first, then deep analysis). When `--references` is provided, adds cross-reference analysis for factual consistency, scope violations, and omissions.

**Arguments:**
- `filepath-or-url` (required): Path to a local file or URL to review
- `--references, -r`: One or more reference files to verify against (triggers verification mode)
- `--language, -l`: Language of the content (default: auto-detect). Affects grammar rules and spelling expectations.
- `--focus grammar|style|logic|consistency|all`: What to focus on (default: all)
- `--output, -o`: Write the review report to a file instead of displaying inline

**Supported file types:** `.md`, `.txt`, `.html`, `.rtf`, `.docx` (via markdown-conversion extraction), `.pdf` (via markdown-conversion extraction). Unsupported types are skipped with a warning.

**Verification mode** (when `--references` is provided): In addition to all standalone checks, performs:
1. **Factual consistency**: Claims that contradict the reference materials
2. **Scope violations**: Content not supported by the references (potential fabrication or hallucination)
3. **Omissions**: Important information present in references but missing from the file
4. **Logical coherence**: Whether conclusions follow logically from the referenced premises

**Examples:**
```
/file-processing:content-review ~/Documents/report.md
/file-processing:content-review ~/Downloads/article.pdf --focus grammar
/file-processing:content-review ~/Notes/meeting-notes.docx --language zh
/file-processing:content-review https://example.com/page.html --output review-report.md
/file-processing:content-review ~/Documents/summary.md --references ~/Documents/source-report.pdf
/file-processing:content-review ~/Documents/draft.md --references ~/Docs/ref1.md ~/Docs/ref2.md --focus consistency
/file-processing:content-review ~/Notes/meeting-summary.docx --references ~/Notes/transcript.txt --language zh
```

## Reference Files

Detailed check criteria live in separate reference files. Read the relevant file when performing each check area:

- **Grammar & Spelling**: `references/grammar-and-spelling.md`
- **Style**: `references/style.md`
- **Logic & Consistency**: `references/logic-and-consistency.md`

The report template is in `assets/report-template.md`.

## Workflow

### Standalone Review (no `--references`)

#### Step 1: Read the file

Read the file content using the Read tool. For `.docx` and `.pdf` files, use the markdown-conversion skill to extract text first, then review the extracted markdown.

For URLs, use WebFetch to retrieve the content.

If the file type is not supported, inform the user and stop.

#### Step 2: Pass 1 — Surface review

Read `references/grammar-and-spelling.md` and `references/style.md` for detailed check criteria. Check for:

- **Grammar & Spelling** — misspellings, agreement, tense, punctuation, article usage
- **Style** — spacing anomalies (double spaces, spaces before punctuation), inconsistent punctuation variants, formatting inconsistencies
- **Formatting** (ignore markitdown artifacts) — structural issues not caused by conversion

If `--language` is set, apply language-specific rules (e.g., Chinese character usage, English article rules).

#### Step 3: Pass 2 — Deep review

Read `references/logic-and-consistency.md` for detailed check criteria. Check for:

- **Internal Logic** — contradictions, unsupported conclusions, missing connections
- **Consistency** — inconsistent terminology, numerical data, entity naming

If `--focus` is set to a specific area, only check that area across both passes.

#### Step 4: Produce the report

Read `assets/report-template.md` and use the **Standalone Review** template. Fill in the sections that have issues — omit any section with zero issues.

#### Step 5: Generate diff

After the report, produce a unified diff showing all suggested text fixes:

```diff
--- a/filename.md
+++ b/filename.md
@@ -40,7 +40,7 @@
 The quarterly report shows that we
-recieved
+received
 the target metrics on schedule.
```

Include fixable text changes in the diff (typos, grammar, style fixes like double spaces). Do not include subjective suggestions or structural changes that require human judgment.

If `--output` is specified, write the report + diff to that file path. Otherwise, display inline.

### Verify Review (with `--references`)

#### Step 1: Read all files

Read the target file and all reference files. For `.docx` and `.pdf` files, use the markdown-conversion skill to extract text first.

If any file cannot be read, inform the user and continue with the remaining files.

#### Step 2: Pass 1 — Surface review (same as standalone)

Perform the full surface review on the target file (grammar, style, formatting).

#### Step 3: Pass 2 — Deep review + Cross-reference analysis

Perform all deep review checks (internal logic, consistency) **plus** cross-reference analysis:

- **Factual Consistency** — verify claims against references, flag contradictions
- **Scope Check** — identify content with no basis in references (potential fabrication)
- **Omissions** — identify key information in references absent from the target

If `--focus` is set to a specific area, only check that area.

#### Step 4: Produce the report

Read `assets/report-template.md` and use the **Verification Review** template. Fill in the sections that have issues — omit any section with zero issues.

#### Step 5: Generate diff

Same diff format as standalone mode — only fixable text changes, not subjective suggestions.
