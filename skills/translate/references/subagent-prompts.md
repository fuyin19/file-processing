# Sub-agent Prompts — translate

Single source of truth for how the orchestrator spawns translate sub-agents and
what JSON each returns. The pipeline (`scripts/translate_pipeline.py`) does the
deterministic work (chunking, passage selection, glossary slicing, structural
QA); sub-agents do the linguistic work. **Accuracy is the top priority; speed and
token cost are secondary.**

> **Runtime contract.** All four agents are *narrow* and *exhaustive*. The
> orchestrator (main agent) writes every file and validates every payload. An
> agent never decides whether to proceed; it only returns structured JSON.

The orchestrator preflights `runtime_mode`. In `orchestrated` mode it runs the
two-phase glossary + chunked translation below. In `legacy_single_agent` it falls
back to the original single-pass translate (no sub-agents) and stamps the output.

---

## Pipeline shape (what the orchestrator runs)

```
prepare (python) → CHUNK PLAN + PASSAGE MANIFEST
  │
  ├─ G1: source-term-extractor (one per source chunk)  → candidate terms
  ├─ G2: reference-grounder   (batched over terms)      → grounded glossary
  │     (Python pre-selects reference passages per term via select_reference_passages)
  ├─ merge → structured glossary {"terms":[...]} (saved to derive_glossary_path)
  │
  ├─ translator-chunk (one per source chunk, glossary sliced per chunk)
  │     → {translated_markdown, self_audit}; orchestrator writes chunk_NNN.<lang>.md
  ├─ assemble + qa (python) → per-occurrence forced application + fix map
  └─ (optional) consistency-QA over the assembled translation
```

---

## Shared rules for every agent

- Return **ONLY** the JSON object described — no prose, no markdown fences.
- Be exhaustive. Do not summarize, do not skip, do not say "etc.".
- Empty is legitimate where applicable (return an empty list, not prose).
- For every claimed mapping/translation, cite evidence (a passage id from the
  PASSAGE MANIFEST, or note `confidence:"none"` when no reference basis exists).

---

## Agent 1 — `source-term-extractor` (G1, one per source chunk)

**Reads:** one chunk file (`<workspace>/chunk_NNN.md`, document lines `start`–`end`)
+ the chunk index.

**Mandate:** exhaustively enumerate candidate terms that appear in THIS chunk —
proper nouns, common nouns, multi-word expressions, technical jargon, acronyms,
and recurring specific phrases. **Err hard toward inclusion** (the grounder and
slicing will filter); a missed term here is a missed translation later.

**Prompt template:**

```
You are a terminology extractor. Scan ONLY the chunk below and extract EVERY
candidate term that a translator would need a glossary entry for. Be exhaustive.

Read the chunk: <chunk_path> (document lines <start>-<end>, chunk index <chunk>).

Extract: proper nouns, common nouns, multi-word phrases, technical jargon,
acronyms, and recurring specific expressions. Include terms even if you are
unsure — the grounder will filter. For each, record type, a short context
sentence, an approximate frequency in this chunk, and the line number(s).

Return ONLY this JSON (no prose):
{
  "chunk": <chunk>,
  "terms": [
    {"source": "<term>", "type": "proper-noun|common-noun|phrase|jargon|acronym",
     "context": "<short context>", "frequency": <int>,
     "occurrences": [{"line": <whole-doc line>}]}
  ]
}
```

The orchestrator merges all chunks' `terms` into one candidate list, attaching
`source_chunks` from each extractor's chunk index.

---

## Agent 2 — `reference-grounder` (G2, batched over terms)

**Reads:** a batch of candidate terms + the reference passages Python
pre-selected for each (via `select_reference_passages`), from the PASSAGE
MANIFEST. The orchestrator passes, per term, the top-K passage ids and their file
paths.

**Mandate:** for each term, find its established translation in the references and
cite the passage. If a term has no basis in any provided passage, widen once to
neighboring passages (the orchestrator can re-select); if still none, mark
`confidence:"none"` — do NOT invent a translation.

**Prompt template:**

```
You are a terminology grounder. For each term, find how the REFERENCES translate
it, and cite the passage. Accuracy matters; do not guess.

Terms to ground (batch):
<JSON list of {source, context, passages:[{id, path, score}]}>

For each term:
- Read the provided passage files. Find the established target-language rendering.
- If the term is ambiguous (different translations in different contexts), record
  alternatives + a context_note explaining when to use which.
- If NO passage supports the term, set target:null, confidence:"none",
  status:"unresolved" — do not fabricate.

Cite evidence as the passage id (e.g. "ref2#p12").

Return ONLY this JSON (no prose):
{
  "results": [
    {"source": "<term>", "target": "<target or null>",
     "alternatives": ["..."], "context_note": "...",
     "evidence": "<passage id>", "confidence": "high|medium|none",
     "source_chunks": [<chunk indexes where the source term occurs>]}
  ]
}
```

The orchestrator merges results into the structured glossary. `confidence:"none"`
entries are kept (`target:null`) for the translator to handle and the QA to verify
non-vacuously.

---

## Agent 3 — `translator-chunk` (one per source chunk)

**Reads:** one chunk file + that chunk's **glossary slice** (produced by
`slice_glossary_for_chunk`: the chunk's occurrence terms + global protected terms,
capped by `max_terms_per_chunk_prompt`) + `references/translation-guidelines.md`.

**Mandate:** translate the chunk completely and apply every glossary entry it
contains. Return the translation + a self-audit listing, for each glossary term
encountered, how it was rendered. For `confidence:none` terms you MUST still pick a
rendering (do not skip); mark `human_confirm:true`.

**Artifact integrity:** return the translation text in the payload. The
orchestrator writes it to `chunk_NNN.<lang>.md` after validating chunk id, lines,
non-empty UTF-8, and code-block preservation. (Only if a payload is too large may
the translator write the file, and only inside the workspace path it is given; the
orchestrator re-validates size/encoding/hash before assembling.)

**Prompt template:**

```
You are a translator. Translate ONLY the chunk below into <target_lang>. Apply the
glossary exactly. Follow references/translation-guidelines.md.

Read the chunk: <chunk_path> (document lines <start>-<end>, chunk index <chunk>).

Glossary slice for this chunk (apply every entry whose source appears here):
<JSON: slice_glossary_for_chunk output>

Rules:
- Preserve ALL markdown structure: heading levels, list markers, table | layout,
  links (translate [text], keep (url)), code blocks/inline code/URLs/paths
  UNCHANGED.
- For every glossary term whose source appears in this chunk, use its target. For
  confidence:none terms, choose the best rendering yourself (do NOT leave the
  source untranslated) and set human_confirm:true in the self-audit.
- Translate every paragraph/heading/cell. Do not skip or summarize.

Return ONLY this JSON (no prose):
{
  "translated_markdown": "<full translation of this chunk>",
  "self_audit": {
    "chunk": <chunk>,
    "lines": "<start>-<end>",
    "headings": <count in this chunk>,
    "paragraphs": <count>,
    "code_blocks_preserved": <count>,
    "glossary_applied": [
      {"source": "<term>", "rendered": "<target form used>",
       "occurrences": [<line numbers>], "confidence": "high|medium|none",
       "human_confirm": <true only for confidence:none>}
    ]
  }
}
```

---

## Agent 4 — `consistency-QA` (optional, over the assembled translation)

**Reads:** the assembled translation (all chunks in order) + the structured
glossary.

**Mandate:** catch what regex QA cannot — cross-chunk terminology drift, dropped
sections, fluency, register consistency. Return a report; the orchestrator fixes
`error`-level items. **This step is optional:** failure does not block `write`,
but must be recorded as a warning in the QA report.

**Prompt template:**

```
You are a translation consistency reviewer. Read the assembled translation and the
glossary, and report cross-chunk issues only (structural counts are checked by the
pipeline).

Read the assembled translation: <assembled_path>
Read the glossary: <glossary_path>

Check:
- Terminology drift: same glossary source term rendered differently in different
  chunks without a context_note justification.
- Dropped or added sections vs. the source structure.
- Fluency/register consistency across chunk boundaries (the seams where chunks
  join).

Return ONLY this JSON (no prose):
{
  "issues": [
    {"severity": "error|warning", "chunk": <index or null>,
     "issue": "...", "suggestion": "..."}
  ]
}
```

---

## How the orchestrator uses these

1. `python scripts/translate_pipeline.py prepare --input <file> [--references …]
   [--glossary …] [--language <lang>]` → prints SOURCE/REFERENCES/INSTRUCTIONS
   (legacy) + **CHUNK PLAN** + **PASSAGE MANIFEST**, writes chunk + passage files.
2. **G1**: one `source-term-extractor` per chunk (concurrent). Merge → candidate
   list with `source_chunks`.
3. **G2**: `select_reference_passages` (Python) pre-selects passages per term;
   `reference-grounder` agents batched over terms. Merge → structured glossary;
   `save_glossary_structured` to `derive_glossary_path` (and `--glossary-output`).
4. **Translator**: `slice_glossary_for_chunk` per chunk; one `translator-chunk`
   per chunk. Orchestrator validates (`validate_translator_payload`) and writes
   `chunk_NNN.<lang>.md` + `self_audit_NNN.json`. Re-dispatch up to 2× on
   invalid/missing; still failing → `FAILED` (blocks `write`).
5. **Assemble + QA**: orchestrator concatenates chunks in order → temp file →
   `python scripts/translate_pipeline.py qa --source … --translation <temp>
   --language <lang> [--workspace …]`. qa auto-discovers the glossary, enforces
   per-occurrence application (convergence-gated), checks confidence:none
   consistency, and prints a **FIX MAP** (`term → chunk`).
6. **Fix loop**: for each fix-map entry, re-translate that chunk with a forced
   prompt listing the required term; re-assemble; re-qa. Cap **2** re-translates
   per chunk; remaining issues → human-handoff list. If qa has `error`s or a
   required stage `FAILED`, `write` is blocked unless the user accepts a partial
   artifact.
7. `python scripts/translate_pipeline.py write --input … --translation <final>
   --language <lang>` → `<stem>.<lang>.md`.
