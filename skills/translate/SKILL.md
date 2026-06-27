---
name: translate
description: |
  Translate files to a target language with optional reference-guided terminology.
  Use this skill whenever the user wants to:
  - Translate a document from one language to another (e.g., English to Chinese or vice versa)
  - Convert a markdown file, PDF, DOCX, or other document to another language
  - Translate content using reference materials for consistent terminology
  - Generate a glossary from reference files and apply it during translation
  - Check translation quality and completeness
  Even if they don't say "translate", if they mention converting content to another language,
  making a Chinese/English version, or localizing a document, use this skill.
metadata:
  version: 1.0.0
---

# Translate

Translate files to a target language. Supports direct translation and reference-guided translation with automatic glossary generation. Produces a translated file with structural QA verification.

## Commands

### `/file-processing:translate <filepath> --language <lang> [options]`

Translate a file to the specified language. Performs a structured workflow: prepare → translate → QA → write.

**Arguments:**
- `filepath` (required): Path to a local file to translate
- `--language, -l` (required): Target language code (e.g., `zh`, `en`, `zh-CN`, `ja`)
- `--references, -r`: One or more reference files or directories (triggers auto-glossary generation)
- `--glossary, -g`: Pre-made glossary files in JSON, CSV/TSV, or MD table format
- `--glossary-output`: Path to save the auto-generated glossary JSON (default: alongside input, e.g., `report.glossary.zh.json`)
- `--output, -o`: Custom output path (default: auto-generated with language suffix)
- `--no-frontmatter`: Skip adding YAML frontmatter
- `--overwrite`: Overwrite existing output file
- `--rename`: Rename output if file exists (append timestamp)

**Supported file types:** `.md`, `.txt`, and all formats supported by markdown-conversion (`.pdf`, `.docx`, `.html`, etc. — non-.md files are extracted via markitdown first).

**Examples:**
```
/file-processing:translate ~/Documents/report.md --language zh
/file-processing:translate ~/Downloads/article.pdf -l en
/file-processing:translate ~/Notes/meeting.md -l zh --references ~/Notes/meeting-zh-summary.md
/file-processing:translate ~/Documents/paper.md -l zh --references ~/Docs/reference-folder/ --glossary-output ~/Docs/paper.glossary.zh.json
/file-processing:translate ~/Documents/report.md -l en --glossary ~/Docs/terms.json --overwrite
/file-processing:translate ~/Downloads/slides.pptx -l zh --no-frontmatter
```

## Workflow

### Direct Translation (no references)

#### Step 1: Prepare source

```bash
python scripts/translate_pipeline.py prepare \
  --input "<filepath>" \
  --language "<target_lang>"
```

Gate: exit code 0 = success. The script outputs the source text with translation instructions.

#### Step 2: Translate

Read the source text from the prepare output and translate it following the guidelines below. Write the translation to a temporary file (e.g., alongside the source, with a `.tmp.<lang>.md` suffix).

#### Step 3: QA check

```bash
python scripts/translate_pipeline.py qa \
  --source "<filepath>" \
  --translation "<temp_translation_file>" \
  --language "<target_lang>"
```

Gate: check the output for issues. If issues found, fix the translation and re-run QA.

#### Step 4: Write output

```bash
python scripts/translate_pipeline.py write \
  --input "<filepath>" \
  --translation "<temp_translation_file>" \
  --language "<target_lang>" \
  [--overwrite | --rename]
```

Gate: exit code 0 = success, 1 = error. Report the `[OK]` line to the user.

### Reference-Guided Translation (primary workflow)

When `--references` is provided, an auto-glossary is generated from the reference materials before translation.

#### Step 1: Prepare source + references

```bash
python scripts/translate_pipeline.py prepare \
  --input "<filepath>" \
  --language "<target_lang>" \
  --references <ref1> [ref2 ...] \
  [--glossary-output "<glossary_path>"]
```

References can be individual files or directories. Directories are scanned recursively for all supported file types. Non-.md files are converted via markitdown.

Gate: exit code 0 = success. The script outputs source text, reference content, and instructions.

#### Step 2: Generate comprehensive glossary

This is a critical step. Analyze the source text and ALL reference content to extract a comprehensive terminology glossary:

1. **Exhaustive extraction**: Scan ALL reference files for domain-specific terminology, proper nouns, technical jargon, abbreviations, and recurring expressions
2. **Source-target mapping**: Match each term in the source language to its equivalent in the target language as used in the references
3. **Context notes**: Where the reference uses a specific translation for an ambiguous term, note the context
4. **Save the glossary**: Write to the path specified by `--glossary-output` (or the default path alongside the input file)

**Glossary format** (JSON):
```json
{
  "machine learning": "机器学习",
  "neural network": "神经网络",
  "gradient descent": "梯度下降",
  "backpropagation": "反向传播"
}
```

**Why comprehensive matters**: A partial glossary leads to inconsistent translations. Extract ALL terminology, not just the most obvious terms. Err on the side of including more entries rather than fewer.

#### Step 3: Translate using glossary

Translate the source text using the generated glossary for consistent terminology. Write the translation to a temp file.

#### Step 4-5: QA + Write

Same as direct translation (Steps 3-4 above), but also pass `--glossary` to the QA step to verify glossary term coverage:

```bash
python scripts/translate_pipeline.py qa \
  --source "<filepath>" \
  --translation "<temp_file>" \
  --language "<target_lang>" \
  --glossary "<glossary_path>"
```

### Success

Report the final output path and a brief summary:
- File written to: `<output_path>`
- Language: `<target_lang>`
- References used: <count> files (if applicable)
- Glossary: <count> terms (if applicable)
- QA: <passed / issues fixed>

## Translation Guidelines

### Preserve

- **Markdown structure**: Keep all headings, lists, tables, links, and formatting intact
- **Code blocks**: Never translate content inside fenced (```) or inline (`) code blocks
- **URLs and file paths**: Keep as-is
- **Variable names and technical identifiers**: Keep as-is
- **Frontmatter**: If present, keep the structure but do not translate field names

### Translate

- **Headings**: Translate heading text, keep the `#` markers and level
- **Table content**: Translate cell text, keep the `|` structure and alignment
- **Link text**: Translate the display text, keep the URL unchanged
- **Image alt text**: Translate alt descriptions
- **List items**: Translate the text, keep the numbering/bullet markers

### Quality

- **Consistency**: Use the same translation for the same term throughout
- **Natural language**: Produce fluent, natural-sounding output in the target language
- **Completeness**: Translate every paragraph and section — do not skip or summarize
- **Accuracy**: Preserve the original meaning without adding or removing information

For detailed translation rules and edge cases, see `references/translation-guidelines.md`.

## Glossary Auto-Generation

The auto-glossary is a first-class output artifact:

- **Saved to disk** as a JSON file (e.g., `report.glossary.zh.json`)
- **Reviewable**: Users can inspect, edit, and refine the glossary before translation
- **Reusable**: Pass the same glossary to future translations with `--glossary`
- **Merged**: Pre-made glossaries (`--glossary`) are combined with auto-generated ones

## Prerequisites

- `markitdown` — format conversion for non-.md files (auto-installed if missing)
- No other external dependencies for the pipeline scripts

## Configuration

Settings are stored in `scripts/config.json`:

| Setting | Default | Description |
|---------|---------|-------------|
| `default_target_language` | `zh` | Default target language when not specified |
