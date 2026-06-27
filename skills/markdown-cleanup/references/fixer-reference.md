# Fixer Reference

Detailed documentation for each fixer in the markdown-cleanup pipeline.

## Enabled by Default

### base64_image_stubs

**Removes:** `![...](data:image/...;base64,...)` unrenderable image blobs.

**Why:** markitdown sometimes extracts embedded images as base64 data URIs. These are unrenderable in Obsidian (too large for inline display) and bloat the file. A single file can have 400+ instances.

**Example:**
```markdown
Before: Some text ![](data:image/jpeg;base64,/9j/4AAQ...) more text
After:  Some text  more text
```

---

### empty_pptx_notes

**Removes:** `### Notes:` headings that have no content (followed by blank line or next heading).

**Preserves:** `### Notes:` headings that have actual text content underneath.

**Why:** PPTX conversions always include a `### Notes:` heading per slide, but the notes are almost always empty. Removing the empty headings declutters. If a slide has actual speaker notes, they are preserved.

**Example:**
```markdown
Before:
### Notes:

## Next Slide Heading

After:
## Next Slide Heading

Before (preserved):
### Notes:
Remember to mention the Q3 targets here.
```

---

### orphaned_image_refs

**Removes:** Broken image links to extracted PPTX media files that don't exist in the vault.

**Patterns:** `Image0.jpg`, `Picture20.jpg`, `图片201.jpg`, `图形39.jpg`, `Clip5.png`, `image_10.png`, etc.

**Preserves:** Image links to files that could be real attachments (e.g., `company-logo.png`, `diagram.png`).

**Example:**
```markdown
Before: ![图标 AI 生成的内容可能不正确。](图片201.jpg)
After:  (removed)
```

---

### print_metadata

**Removes:** Print-production system traces: `JOBNAME:`, `PAGE:`, `SESS:`, `OUTPUT:`, `Mark Trace:` lines.

**Why:** Typeset documents (prospectuses, regulatory filings) include typesetting system metadata that gets extracted as text.

**Example:**
```markdown
Before: JOBNAME: HIP25080021\_E\_Reborn PAGE: 1 SESS: 103 OUTPUT: Fri Mar 20 16:51:12 2026
After:  (removed)
```

---

### xml_data_leakage

**Removes:** Raw XBRL/XML identifier blocks (long namespace-qualified identifiers, 5+ underscore segments).

**Why:** SEC filings and HTML conversions can dump structured data layer as raw text.

**Example:**
```markdown
Before: ifrs-full:MeasurementBasisSummaryDescriptionOfBasisOfMeasurement
After:  (removed)
```

---

### empty_table_rows

**Removes:** Table rows where every cell is empty (only whitespace between pipes).

**Preserves:** Rows with any content, header rows, separator rows.

**Example:**
```markdown
Before:
| a | b |
| --- | --- |
|  |  |
| c | d |

After:
| a | b |
| --- | --- |
| c | d |
```

---

### dead_toc_links

**Converts:** Dead anchor links (`#_Toc*`, `#bookmark*`, `#_GoBack`, `#_Hlt*`) to plain text.

**Why:** Word-generated internal hyperlinks have anchors that don't exist in the converted markdown.

**Example:**
```markdown
Before: [Introduction 6](#_Toc221481040)
After:  Introduction 6
```

---

### backslash_escapes

**Unescapes:** `\_` → `_`, `\*` → `*`, `\#` → `#` (outside code blocks).

**Why:** markitdown sometimes over-escapes these characters.

**Example:**
```markdown
Before: HIP25080021\_E\_Reborn
After:  HIP25080021_E_Reborn
```

---

### blank_lines

**Collapses:** 3+ consecutive newlines → 2 (one blank line). Also strips trailing whitespace from lines.

**Why:** Other fixers and the conversion process can leave excessive blank lines.

**Example:**
```markdown
Before:
Paragraph 1



Paragraph 2

After:
Paragraph 1

Paragraph 2
```

---

## Disabled by Default

### broken_word_hyperlinks

**Fixes:** Word hyperlink artifacts where display text bleeds into the URL portion.

**Why disabled:** The display text may be meaningful as-is. Fixing changes the visible content.

**Example:**
```markdown
Before: [at **www.platinumlife.cn**. If you](atwww.platinumlife.cn.Ifyou)
After:  [If you](http://www.platinumlife.cn)
```

---

### list_numbering

**Merges:** Consecutive numbered lists that restart from 1 into one continuous sequence.

**Why disabled:** In Q&A documents and regulatory filings, the restart-from-1 pattern is intentional — it signals separate questions or sections.

**Example:**
```markdown
Before:
1. Question A
Answer A
1. Question B
Answer B

After (with fixer):
1. Question A
Answer A
2. Question B
Answer B
```

---

### slide_comments

**Removes:** `<!-- Slide number: N -->` HTML comments that mark PPTX slide boundaries.

**Why disabled:** These comments ARE the slide structure. Removing them means you can no longer tell where one slide ends and another begins. Only use this if you want to flatten the slide structure entirely.

**Example:**
```markdown
Before:
<!-- Slide number: 1 -->
Title

<!-- Slide number: 2 -->
Content

After (with fixer):
Title

Content
```
