# Logic & Consistency Checks

Check for deeper issues that affect the soundness and coherence of the content.

## Internal Logic

- Self-contradictory statements within the document (claims that conflict with earlier statements)
- Unsupported conclusions (leaps of reasoning without evidence or connecting argument)
- Missing logical connections between sections (abrupt topic shifts without transitions)
- Circular reasoning or tautologies presented as arguments
- Causal claims without supporting evidence (post hoc reasoning)

## Consistency

- Inconsistent terminology for the same concept (e.g., using "revenue", "turnover", and "sales" interchangeably without defining them as equivalent)
- Inconsistent numerical data or statistics (e.g., "12%" in one place, "15%" for the same metric elsewhere)
- Inconsistent naming of the same entities (e.g., "Acme Corp" vs "Acme Corporation" vs "ACME")
- Inconsistent formatting of the same data type (e.g., dates as "Jan 2025" vs "2025-01" vs "January 2025")
- Inconsistent level of detail across comparable sections

## Verification Mode (with `--references`)

When references are provided, also check:

### Factual Consistency
- Identify claims, data points, and assertions in the target file
- Verify each against the reference materials
- Flag any claim that directly contradicts the references

### Scope Check
- Identify content in the target file not supported by any reference material
- Distinguish between: (a) reasonable synthesis or inference from references, and (b) content with no basis in references
- Flag category (b) as potential fabrication or hallucination

### Omissions
- Identify key information in the references absent from the target file
- Only flag significant omissions, not minor details

### Logical Coherence
- Whether conclusions follow logically from the referenced premises
- Whether the target file's narrative is consistent with the reference narrative

## What Not to Flag

- Differences of opinion or interpretation (unless the target file claims to be a faithful summary)
- Reasonable paraphrasing or synthesis of reference material
- Information that is common knowledge and doesn't need referencing
