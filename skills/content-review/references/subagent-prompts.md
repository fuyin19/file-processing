# Sub-agent Prompts — content-review

This file is the **single source of truth** for how the main agent (orchestrator)
spawns review sub-agents and what JSON each one must return. The main agent reads
the matrix plan from `scripts/review_plan.py plan`, dispatches one sub-agent per
cell using the prompt templates below, writes each result to the workspace as
`<dimension>__<chunk:03d>.json`, then runs `scripts/review_plan.py assemble`.

> **Runtime contract.** Each reviewer is a *narrow, exhaustive* sub-agent: it
> reviews ONE dimension on ONE chunk. Narrow scope + a forced structured return
> is the anti-skip mechanism — a reviewer cannot "do everything at once and
> shortcut". The main agent, not the reviewer, decides adoption and writes files.

---

## Shared output contract (every reviewer returns EXACTLY this)

Return **ONLY** a JSON object, no prose, no markdown fences:

```json
{
  "cell": {"dimension": "<dimension>", "chunk": <chunk index>},
  "checked_thoroughly": true,
  "findings": [
    {
      "severity": "high | medium | low",
      "line": <1-indexed line within the WHOLE document, taken from the chunk header>,
      "quote": "<the offending text, verbatim>",
      "category": "spelling | grammar | style | logic | consistency | fact",
      "issue": "<one-sentence description>",
      "suggestion": "<concrete fix, or '' if none>",
      "fixable": <true if a mechanical text change fixes it; false for subjective/structural>
    }
  ]
}
```

Rules:
- `cell.dimension` and `cell.chunk` MUST match the cell you were assigned — the
  assembler rejects mismatched identity as `FAILED`.
- **Exhaustive, no summarizing.** Report EVERY issue. Do not collapse multiple
  issues into one. Do not say "see above" or "etc.".
- **Empty is legitimate.** If you checked thoroughly and found nothing, return
  `findings: []` with `checked_thoroughly: true`. An empty result is NOT a
  failure; only a missing/unparseable/mismatched result is `FAILED`.
- **Line numbers are whole-document line numbers**, not chunk-relative. The chunk
  file's header states the chunk's `start`–`end` range; add the offset yourself.
- Do **not** flag markitdown conversion artifacts (orphan image filenames,
  `![...](...)`, broken tables from merged cells, YAML frontmatter, encoding
  artifacts). See SKILL.md "Markitdown Artifact Awareness".

---

## Dimension 1 — `grammar-style` reviewer

**Reads:** the chunk file + `references/grammar-and-spelling.md` + `references/style.md`.

**Prompt template** (main agent fills `<…>`):

```
You are a grammar & style reviewer. Review ONLY the chunk below, for grammar,
spelling, and style issues. Be exhaustive; do not summarize or skip.

Read the chunk file at <chunk_path> (it covers document lines <start>-<end>).
Also read the criteria: references/grammar-and-spelling.md and references/style.md.

You are checking ONE dimension on ONE chunk:
- Spelling, subject-verb agreement, tense, punctuation, articles, pronouns,
  modifiers (English); character usage / particles (Chinese); mixed-script and
  copy-paste breaks (language-agnostic).
- Style: spacing (double spaces, space before/after punctuation, CJK/Latin
  spacing), dash/quote/ellipsis/list-marker consistency, header capitalization
  and bold/italic consistency, date/number formatting consistency.

For each issue, set category to spelling|grammar|style. Set fixable=true for
mechanical fixes (typos, double spaces); fixable=false for subjective calls.
Line numbers are whole-document lines (chunk covers <start>-<end>).

Do NOT flag markitdown artifacts. If you find nothing after a thorough check,
return findings: [] with checked_thoroughly: true.

Return ONLY this JSON (no prose):
{ "cell": {"dimension": "grammar-style", "chunk": <chunk>}, "checked_thoroughly": true,
  "findings": [ {severity, line, quote, category, issue, suggestion, fixable}, ... ] }
```

---

## Dimension 2 — `logic-consistency` reviewer

**Reads:** the chunk file + `references/logic-and-consistency.md` (the Internal
Logic and Consistency sections — NOT the Verification Mode section, which is the
fact-check reviewer's domain).

**Prompt template:**

```
You are a logic & consistency reviewer. Review ONLY the chunk below, for internal
logic and consistency issues. Be exhaustive; do not summarize or skip.

Read the chunk file at <chunk_path> (document lines <start>-<end>).
Also read the criteria: references/logic-and-consistency.md (Internal Logic +
Consistency sections only).

You are checking ONE dimension on ONE chunk:
- Internal logic: self-contradictions, unsupported conclusions, missing
  connections, circular reasoning, unsupported causal claims.
- Consistency: inconsistent terminology / numbers / entity names / date-number
  formatting / level of detail — BUT only flag what is visible WITHIN this chunk
  (cross-chunk consistency is handled by the final consistency pass; if you
  suspect a cross-chunk issue, still record it with category=consistency).

For each issue, set category to logic|consistency. These are almost always
fixable=false (they need human judgment). Line numbers are whole-document lines.

Do NOT flag differences of opinion or reasonable paraphrase. If you find nothing
after a thorough check, return findings: [] with checked_thoroughly: true.

Return ONLY this JSON (no prose):
{ "cell": {"dimension": "logic-consistency", "chunk": <chunk>}, "checked_thoroughly": true,
  "findings": [ {severity, line, quote, category, issue, suggestion, fixable}, ... ] }
```

> Note: each reviewer sees only its chunk, so a *contradiction between two chunks*
> is not visible to either alone. The orchestrator's final consistency pass (or a
> dedicated cross-chunk reviewer when `--references` is used) handles that. Do not
> try to verify claims you cannot see.

---

## Dimension 3 — `fact-check` reviewer (only when `--references` is given)

**Reads:** the chunk file + ALL reference files + the Verification Mode section of
`references/logic-and-consistency.md`.

**Prompt template:**

```
You are a fact-check reviewer. Verify ONLY the claims in the chunk below against
the reference materials. Be exhaustive; do not summarize or skip.

Read the chunk file at <chunk_path> (document lines <start>-<end>).
Read every reference file: <reference_path_1>, <reference_path_2>, ...
Also read references/logic-and-consistency.md (Verification Mode section).

You are checking ONE dimension on ONE chunk:
- Factual consistency: flag claims in the chunk that CONTRADICT the references.
- Scope check: flag chunk content with NO basis in any reference (potential
  fabrication) — distinguish reasonable synthesis (do not flag) from unsupported
  content (flag).
- Omissions: flag only SIGNIFICANT reference information absent from the chunk.

For each issue, set category=fact. Put the supporting reference location in
"suggestion" (e.g. "ref2.md line 45: '2026 launch'") so the report's
Cross-Reference Issues column can cite it. fixable=false for fact issues.

If you find nothing after a thorough check, return findings: [] with
checked_thoroughly: true.

Return ONLY this JSON (no prose):
{ "cell": {"dimension": "fact-check", "chunk": <chunk>}, "checked_thoroughly": true,
  "findings": [ {severity, line, quote, category:"fact", issue, suggestion, fixable:false}, ... ] }
```

---

## How the orchestrator uses these

1. `python scripts/review_plan.py plan --input <file> [--references …] [--focus …]`
   → prints the matrix plan JSON, writes `.review-workspace/chunk_NNN.md`.
2. For **each** cell in `plan.cells`, spawn one sub-agent with the matching prompt
   template above (pass the chunk path, the chunk's `start`-`end` range, the
   chunk index, and the reference paths for fact-check). Run them concurrently
   (batched to the runtime's concurrency limit).
3. Write each sub-agent's JSON result to
   `.review-workspace/<dimension>__<chunk:03d>.json`.
4. A sub-agent that returns non-JSON, the wrong shape, or a mismatched `cell` is
   re-dispatched up to **2** times. Still failing → leave the file missing; the
   assembler marks the cell `FAILED`.
5. `python scripts/review_plan.py assemble --plan <plan.json> --cells-dir <ws>`
   → validates every cell, dedupes findings, fills the report, emits a unified
   diff for `fixable` findings. If any required cell is `FAILED`, the report is
   marked **incomplete** and no diff is produced unless `--accept-partial` is set.
