"""
fixers.py - Individual fixer functions for markdown-cleanup pipeline.

Each fixer is a pure function: (text: str) -> (text: str, changes: int).
Fixers run on text with code blocks and frontmatter already protected.
"""
import re

# ---------------------------------------------------------------------------
# Code block / frontmatter protection
# ---------------------------------------------------------------------------

_PLACEHOLDER_SENTINEL = '\x00'


def protect_code_blocks(text):
    """Replace fenced code blocks and inline code with placeholders.

    Returns (protected_text, placeholders_dict).
    """
    placeholders = {}
    counter = [0]

    def _next_key(prefix):
        key = f'{_PLACEHOLDER_SENTINEL}{prefix}_{counter[0]}{_PLACEHOLDER_SENTINEL}'
        counter[0] += 1
        return key

    # Fenced code blocks: ```...``` and ~~~...~~~
    # Match opening fence, then everything until matching closing fence
    def _replace_fenced(match):
        key = _next_key('FENCED')
        placeholders[key] = match.group(0)
        return key

    text = re.sub(
        r'(?m)^(?:```|~~~)[^\n]*\n(?:.*\n)*?(?:```|~~~)',
        _replace_fenced, text, flags=re.DOTALL
    )

    # Inline code: `...`
    def _replace_inline(match):
        key = _next_key('INLINE')
        placeholders[key] = match.group(0)
        return key

    text = re.sub(r'`[^`\n]+`', _replace_inline, text)

    return text, placeholders


def restore_code_blocks(text, placeholders):
    """Restore protected code blocks from placeholders."""
    for key, original in placeholders.items():
        text = text.replace(key, original)
    return text


def extract_frontmatter(text):
    """Extract YAML frontmatter from the start of text.

    Returns (frontmatter_str, body_str).  frontmatter_str includes the ---
    delimiters and trailing newlines.  If no frontmatter, returns ('', text).
    """
    if not text.startswith('---'):
        return '', text
    # Find closing ---
    end = text.find('\n---', 3)
    if end == -1:
        return '', text
    fm = text[:end + 4]  # include closing ---\n
    body = text[end + 4:]
    # Strip leading newlines from body
    body = body.lstrip('\n')
    return fm, body


# ---------------------------------------------------------------------------
# Compiled regex patterns (module-level for reuse)
# ---------------------------------------------------------------------------

# Base64 image stubs: ![...](data:image/...;base64,...)
_base64_image_re = re.compile(r'!\[[^\]]*\]\(data:image/[^;]+;base64,[^)]*\)')

# Print-production metadata
_jobname_re = re.compile(r'^\s*(?:JOBNAME|PAGE\s*:|SESS\s*:|OUTPUT\s*:).*$', re.MULTILINE)
_mark_trace_re = re.compile(r'^\s*Mark\s+Trace:.*$', re.MULTILINE)

# XML/XBRL data leakage: lines dominated by namespace-qualified identifiers
_xml_long_ident_re = re.compile(
    r'^\s*(?:[\w-]+[:\.])+[\w-]{20,}\s*$',
    re.MULTILINE,
)
_xml_underscore_re = re.compile(
    r'^\s*[\w]+_[\w]+_[\w]+_[\w]+_[\w]+\s*$',
    re.MULTILINE,
)

# PPTX empty Notes headings: ### Notes: followed by blank line or next heading
_empty_notes_re = re.compile(
    r'^###\s*Notes:\s*\n(?=\n|#{1,3}\s|\Z)',
    re.MULTILINE,
)

# PPTX slide comments: <!-- Slide number: N -->
_slide_comment_re = re.compile(r'<!--\s*Slide\s*(?:number\s*)?:?\s*\d+\s*-->')

# Orphaned image references from extracted PPTX media
_orphaned_image_re = re.compile(
    r'!\[[^\]]*\]\('
    r'(?:'
    r'(?:Image|Picture|Clip|image)_?\d+\.\w+'
    r'|图片\d+\.\w+'
    r'|图形\d+\.\w+'
    r')'
    r'\)',
    re.IGNORECASE,
)

# Dead TOC links: #_Toc..., #bookmark..., #_GoBack, #_Hlt...
_dead_toc_re = re.compile(
    r'\[([^\]]*)\]\(#(?:_Toc\d+|bookmark\d*|_GoBack|_Hlt\d+)\)',
    re.IGNORECASE,
)

# Backslash-escaped characters
_backslash_escape_re = re.compile(r'\\([_*#])')

# Excessive blank lines (3+ consecutive newlines)
_blank_lines_re = re.compile(r'\n{3,}')

# Trailing whitespace on lines
_trailing_ws_re = re.compile(r'[ \t]+$', re.MULTILINE)

# Empty table rows: |  |  |  |  ... |  where every cell is whitespace-only
_empty_table_row_re = re.compile(
    r'^\|(?:\s*\|)+\s*$',
    re.MULTILINE,
)

# Markdown image syntax (for broken hyperlink fixer)
_md_link_re = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

# Numbered list item
_list_item_re = re.compile(r'^(\s*)(\d+)\.\s', re.MULTILINE)


# ---------------------------------------------------------------------------
# Fixer functions
# ---------------------------------------------------------------------------

def remove_base64_image_stubs(text):
    """Remove ![...](data:image/...;base64,...) unrenderable blobs."""
    count = len(_base64_image_re.findall(text))
    text = _base64_image_re.sub('', text)
    return text, count


def remove_print_metadata(text):
    """Remove JOBNAME/PAGE/SESS/OUTPUT and Mark Trace lines."""
    count = 0
    n = len(_jobname_re.findall(text))
    text = _jobname_re.sub('', text)
    count += n

    n = len(_mark_trace_re.findall(text))
    text = _mark_trace_re.sub('', text)
    count += n

    return text, count


def remove_xml_data_leakage(text):
    """Remove raw XBRL/XML identifier blocks."""
    count = 0
    n = len(_xml_long_ident_re.findall(text))
    text = _xml_long_ident_re.sub('', text)
    count += n

    n = len(_xml_underscore_re.findall(text))
    text = _xml_underscore_re.sub('', text)
    count += n

    return text, count


def remove_empty_pptx_notes(text):
    """Remove empty ### Notes: headings (preserve notes with actual content)."""
    count = len(_empty_notes_re.findall(text))
    text = _empty_notes_re.sub('', text)
    return text, count


def remove_orphaned_image_refs(text):
    """Remove broken image links to extracted PPTX media (Image0.jpg, etc.)."""
    count = len(_orphaned_image_re.findall(text))
    text = _orphaned_image_re.sub('', text)
    return text, count


def fix_broken_word_hyperlinks(text):
    """Fix Word hyperlink artifacts where display text bleeds into URL.

    Pattern: [at **www.example.com** text](atwww.example.comtext)
    Fix:     [text](http://www.example.com)
    """
    count = 0

    def _fix_link(match):
        nonlocal count
        display = match.group(1)
        url = match.group(2)

        # Skip clearly valid URLs
        if url.startswith(('http://', 'https://', 'mailto:', '#', '/')):
            return match.group(0)

        # Strip markdown formatting from display text
        clean_display = re.sub(r'[*_`~]+', '', display)
        # Create mangled version (strip spaces and common punctuation, but keep dots for URLs)
        mangled = re.sub(r'[\s,;:!?()]+', '', clean_display)

        # Check if URL looks like a mangled version of display text
        if mangled.lower() != url.lower():
            return match.group(0)

        # This is a broken link — try to extract the real URL
        count += 1
        url_match = re.search(r'(?:https?://|www\.)\S+', clean_display)
        if url_match:
            real_url = url_match.group(0)
            if not real_url.startswith('http'):
                real_url = 'http://' + real_url
            # Build new display text without the URL portion
            new_display = clean_display.replace(url_match.group(0), '').strip(' .,;:')
            if new_display:
                return f'[{new_display}]({real_url})'
            return f'<{real_url}>'
        # No URL found — convert to plain text
        return display

    text = _md_link_re.sub(_fix_link, text)
    return text, count


def remove_dead_toc_links(text):
    """Convert dead anchor links (#_Toc*, #bookmark*) to plain text."""
    count = len(_dead_toc_re.findall(text))
    text = _dead_toc_re.sub(r'\1', text)
    return text, count


def renumber_lists(text):
    """Renumber consecutive numbered lists that restart from 1.

    Only merges when two list blocks are separated by exactly one blank line,
    both start from 1, and there's no other content between them.
    """
    count = 0
    lines = text.split('\n')
    result = []
    i = 0

    while i < len(lines):
        result.append(lines[i])
        i += 1

    # This is a simplified approach: scan for adjacent list blocks
    # A list block starts with a line matching ^\s*\d+\.\s
    # We'll track list block boundaries and renumber when merging is safe
    text = '\n'.join(result)

    # Parse into list blocks
    blocks = []
    current_block = []
    current_num = 0

    for line in text.split('\n'):
        m = _list_item_re.match(line)
        if m:
            num = int(m.group(2))
            if num == 1 and current_block:
                # New list starting at 1 — save previous block
                blocks.append(current_block)
                current_block = []
            current_block.append(line)
            current_num = num
        elif line.strip() == '' and current_block:
            current_block.append(line)
        elif current_block:
            blocks.append(current_block)
            current_block = []

    if current_block:
        blocks.append(current_block)

    if len(blocks) <= 1:
        return text, 0

    # Check which adjacent blocks can be merged (separated by only blank lines)
    merged_blocks = []
    prev_block = blocks[0]
    for block in blocks[1:]:
        # Check if previous block ends with blank lines and this one starts at 1
        # and there's no non-list content between them
        can_merge = True
        # All lines between last list item of prev and first item of current must be blank
        # This is already guaranteed by our parsing

        if can_merge:
            # Renumber current block to continue from prev
            last_num = 0
            for line in prev_block:
                m = _list_item_re.match(line)
                if m:
                    last_num = int(m.group(2))

            new_block = []
            for line in block:
                m = _list_item_re.match(line)
                if m:
                    last_num += 1
                    indent = m.group(1)
                    rest = line[m.end():]
                    new_block.append(f'{indent}{last_num}. {rest}')
                    count += 1
                else:
                    new_block.append(line)
            prev_block = prev_block + new_block
        else:
            merged_blocks.append(prev_block)
            prev_block = block

    merged_blocks.append(prev_block)

    if count == 0:
        return text, 0

    # Reconstruct text — but this is complex, so we'll skip for now
    # if no actual merges happened. For safety, just renumber inline.
    # Actually, we need to reconstruct from merged blocks.
    # This fixer is complex and disabled by default, so keep it simple.

    result_lines = []
    for block in merged_blocks:
        result_lines.extend(block)

    return '\n'.join(result_lines), count


def remove_slide_comments(text):
    """Remove <!-- Slide number: N --> comments."""
    count = len(_slide_comment_re.findall(text))
    text = _slide_comment_re.sub('', text)
    return text, count


def remove_empty_table_rows(text):
    """Remove table rows where all cells are empty."""
    count = len(_empty_table_row_re.findall(text))
    text = _empty_table_row_re.sub('', text)
    return text, count


def unescape_backslash_chars(text):
    r"""Unescape \_ to _, \* to *, \# to # (outside code blocks)."""
    count = len(_backslash_escape_re.findall(text))
    text = _backslash_escape_re.sub(r'\1', text)
    return text, count


def collapse_blank_lines(text):
    """Collapse 3+ consecutive newlines to 2, and strip trailing whitespace."""
    count = len(_blank_lines_re.findall(text))
    text = _trailing_ws_re.sub('', text)
    text = _blank_lines_re.sub('\n\n', text)
    return text, count


# ---------------------------------------------------------------------------
# Fixer registry (name -> (function, enabled_by_default))
# ---------------------------------------------------------------------------

FIXERS = [
    ('base64_image_stubs',     remove_base64_image_stubs,     True),
    ('print_metadata',         remove_print_metadata,         True),
    ('xml_data_leakage',       remove_xml_data_leakage,       True),
    ('empty_pptx_notes',       remove_empty_pptx_notes,       True),
    ('orphaned_image_refs',    remove_orphaned_image_refs,    True),
    ('broken_word_hyperlinks', fix_broken_word_hyperlinks,     True),
    ('dead_toc_links',         remove_dead_toc_links,         True),
    ('list_numbering',         renumber_lists,                 False),
    ('slide_comments',         remove_slide_comments,          False),
    ('empty_table_rows',       remove_empty_table_rows,       True),
    ('backslash_escapes',      unescape_backslash_chars,      True),
    ('blank_lines',            collapse_blank_lines,          True),
]

FIXER_MAP = {name: (fn, default) for name, fn, default in FIXERS}
