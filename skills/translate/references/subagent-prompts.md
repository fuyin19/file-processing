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
two-phase glossary + chunked translation below. Without that runtime it must use
report-only mode and cannot publish a formal translation.

## V3 artifact envelope

For v3 runs, every agent return is wrapped before publication with:

```json
{
  "schema_version": "3.0",
  "run_id": "<run manifest run_id>",
  "stage_input_hash": "<Python-computed hash for this stage and its upstream artifacts>",
  "attempt": 1,
  "...": "stage payload"
}
```

The orchestrator validates this envelope, the stage-specific schema, and the
complete coverage universe before publishing it with
`translate_pipeline.py publish-stage`; agents never write a manifest directly.
The translator payload additionally includes `translation_sha256` and one chunk
record per planned chunk; each carries the complete occurrence-ID set it
acknowledged. A v3 QA pass rejects missing, duplicate, unknown, or stale IDs.
The semantic-QA payload is published only after deterministic QA and carries the
same assembled translation hash, complete occurrence-ID set, and mandatory
Python-derived task_coverage evidence for every local/seam/shared-term check.
Without an orchestrated runtime,
use report-only mode; the legacy single-agent fallback cannot write a formal
translation.

---

## Pipeline shape (what the orchestrator runs)

```
prepare (python) → CHUNK PLAN + PASSAGE MANIFEST
  │
  ├─ G1: source-term-extractor (one per source chunk)  → candidate terms
  ├─ G2: reference-grounder   (batched over terms)      → grounded glossary
  │     (Python pre-selects reference passages per term via select_reference_passages)
  ├─ merge → structured glossary {"terms":[...]} (private in this run; export only on request)
  │
  ├─ translator-chunk (one per source chunk, glossary sliced per chunk)
  │     → {translated_markdown, self_audit}; orchestrator validates then publishes a manifest-bound translation artifact
  ├─ assemble + qa (python) → per-occurrence forced application + fix map
  └─ required bounded local/seam/shared-term consistency-QA tasks
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

**Reads:** one chunk file + its complete relevance ledger. If it exceeds a prompt
budget, the orchestrator uses deterministic, exhaustive batches and records all
occurrence ids; it never silently truncates constraints.

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

Relevance batch for this chunk (apply every listed occurrence):
<JSON: deterministic batch output>

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
    ],
    "occurrence_ids": ["<every v3 occurrence id acknowledged in this chunk>"]
  }
}
```

---

## Agent 4 — `consistency-QA` (one deterministic bounded task)

Read only the chunks/occurrences in the Python-generated task descriptor and
relevant bounded reference/style rules. Each task has one or two chunks. Local
tasks cover reference expression, register/style, context rules and source
residual; seam tasks cover every adjacent pair; term tasks compare consecutive
occurrences of each shared term, exhaustively including scheduling boundaries.

```text
You are a translation consistency reviewer. Read the source and translated
chunks named in <task descriptor>, and <relevant glossary/reference context>.
Verify every required check. Catch meaning loss, terminology drift, unsupported
reference expressions, register changes, context-rule violations and untranslated
source. For seams, inspect both sides in order. For shared-term comparisons,
compare the specified occurrences and justify context-dependent differences.
Do not claim a check passed if input is missing or your request budget was exceeded.
Return the exact task identity fields and schema_version:"3.0", attempt,
status:"completed" only after checking all inputs; checks maps every required
check to "pass" or "error"; issues includes every error/warning with chunk,
issue and suggestion. Preserve checked_chunk_ids, checked_occurrence_ids,
translation_sha256, stage_input_hash and task_input_hash exactly.
```

## Orchestration boundary

Prepare emits only counts and local plan/manifest paths. Use the automatic group
loop in SKILL.md: all groups of at most max_chunks, no continuation question.
Keep private terminology processing and reference selection; do not export a
glossary unless the user requested --glossary-output.

For translation, derive `expected_partial_tasks(manifest, 'translation')` after
source matching. Merge each one-chunk translator's validated self-audit into its
exact descriptor, add schema_version, attempt, status, translated_markdown and
top-level occurrence_ids, then write only its prescribed partial path. For QA,
derive the semantic task descriptors after deterministic QA and write each
bounded reviewer result to its prescribed path. Never invent the expected task
set or claim whole-document coverage from sampled groups.

Use `assemble --manifest ... --stage translation|semantic_qa` only after all
expected partials are present; it validates identities/coverage and writes the
full-stage JSON for existing publish-stage. Translation assembly also writes
assembled.translation.md for deterministic QA and write. Retry invalid or missing
responses at most twice (or configured agent_retry_limit), then leave the run
incomplete. Re-publishing a correction invalidates downstream QA; regenerate its
hash-bound task descriptors. Semantic task_coverage is mandatory even for direct
full-stage publication and strict write; aggregate flags alone are insufficient.

Final `write --manifest ...` defaults to ordered bilingual .json; explicit
--output-format markdown produces .md. Export only a requested glossary through
the formal writer. Do not send one model request all assembled source/translation.
