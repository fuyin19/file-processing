# Translation Guidelines

Detailed rules for translating markdown documents. These guidelines ensure consistent, high-quality translations.

## What to Preserve Exactly

### Code and technical content
- Fenced code blocks (` ``` `): Do not translate anything inside them
- Inline code (` `code` `): Do not translate the code content
- URLs and file paths: Keep unchanged
- Variable names, function names, class names: Keep unchanged
- HTML tags and attributes: Keep unchanged
- Mathematical formulas and symbols: Keep unchanged

### Markdown structure
- Heading levels: A `##` heading stays `##` in the translation
- List markers: Numbered lists keep numbers, bullet lists keep `-` or `*`
- Table structure: Keep `|` delimiters, alignment markers (`:`), and row structure
- Links: Translate `[display text]` but keep `(url)` unchanged
- Images: Translate `![alt text]` but keep `(url)` unchanged
- Blockquotes: Keep `>` markers

### Frontmatter
- YAML frontmatter (`---` blocks): Keep the structure and field names in English
- Do not translate `source:`, `converted_at:`, `translated_at:`, etc.
- The `target_language:` value should match the actual target language code

## What to Translate

### Content
- All paragraph text
- All heading text (after the `#` markers)
- Table cell content
- List item text
- Link display text
- Image alt text
- Blockquote content

### Tone and style
- Match the formality level of the source text
- If the source is academic/formal, keep the translation formal
- If the source is casual/blog-style, keep it casual
- Preserve the author's voice and intent

## Language-Specific Rules

### English to Chinese (Simplified)
- Use Simplified Chinese (简体中文) unless explicitly asked for Traditional
- Technical terms: Use established Chinese translations where they exist
- Keep English terms in parentheses on first use if the Chinese term is uncommon
- Example: "机器学习 (machine learning)"
- Proper nouns: Transliterate common names, keep less common ones in English
- Numbers: Use Arabic numerals as in the original

### Chinese to English
- Use clear, idiomatic English
- Technical terms: Use standard English terminology
- Chinese idioms: Translate the meaning, not the literal characters
- Proper nouns: Use standard romanization (Pinyin) for names
- Respect the source text's formality level

## Edge Cases

### Mixed-language content
- If the source already contains both languages (e.g., a Chinese document with English technical terms), translate the non-target-language portions while keeping target-language terms as-is

### Tables
- Translate cell content but preserve the table structure
- Do not translate column headers if they are technical identifiers
- Keep numerical data exactly as-is

### Lists
- Maintain the same number of list items
- Keep the same list nesting level
- Preserve ordered list numbering style

### Cross-references and internal links
- Translate link text
- Keep anchor names (`#section-name`) unchanged unless the target section heading was also translated (in which case, update the anchor to match)

### Abbreviations and acronyms
- First occurrence: Translate and provide the original abbreviation in parentheses
- Subsequent: Use the target-language translation consistently
- Well-known acronyms (API, JSON, HTTP, etc.): Keep as-is

## Completeness Check

After translating, verify:
1. Every paragraph in the source has a corresponding paragraph in the translation
2. Every heading has a translated heading at the same level
3. Every table has the same number of rows and columns
4. No source-language text remains (except for code, URLs, and proper technical terms)
5. All list items are present
6. No content was accidentally omitted or added
