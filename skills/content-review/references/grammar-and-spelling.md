# Grammar & Spelling Checks

Check for surface-level grammar and spelling issues.

## English

- Misspellings and typos
- Subject-verb agreement (e.g., "the team are" vs "the team is" — flag only if clearly wrong)
- Tense consistency within paragraphs (unintended shifts between past and present)
- Punctuation errors (missing commas, run-on sentences, comma splices)
- Article usage (a/an/the) — flag missing or incorrect articles
- Pronoun-antecedent agreement
- Dangling or misplaced modifiers

## Chinese

- Incorrect character usage (e.g., 的/地/得 confusion, 做/作)
- Commonly confused characters (e.g., 帐/账, 记/纪)
- Missing or incorrect particles
- Malformed sentences from translation artifacts

## Language-Agnostic

- Unintended mixed scripts (e.g., Latin characters in a Chinese-only paragraph, or vice versa, where it's clearly not a proper noun or technical term)
- Broken sentences from copy-paste errors (mid-sentence line breaks, truncated phrases)

## What Not to Flag

- Intentional informal language or colloquialisms
- Technical terms, jargon, or proper nouns that may be unfamiliar but are correctly spelled
- Ordinary MarkItDown conversion artifacts (see the canonical-source guidance
  in SKILL.md)
