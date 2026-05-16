### [Repair Objective]

You need to make the minimum necessary modifications to `previous_code` according to `fix_instruction`, so that it passes verification by fixing the issues exposed by `verify_report`.

---

### [A. Current Repair Information (highest priority)]

1) fix_instruction (primary reference):
{fix_instruction}

2) verify_report (error basis; must be satisfied first):
{verify_report}

3) previous_code (must be minimally modified based on it):
```python
{previous_code}
```

---

### [B. Background Task Context (used to keep the target aligned; not for rewriting from scratch)]

1. User question

{user_query}

---
2. Task intent (only as the target, not to copy verbatim)

intent_json:
{intent_json}

---
3. Data retrieval recommendations (data reading constraints; band/data reading methods must follow this)

data_recommendations:
{data_recommendations}

---
### [C. Knowledge Basis (used to select/complete operators during repair; note priority and usage rules)]

Usage rules (strict constraints):

- You must prioritize the new operators/cases provided in "1" to repair the current error (if delta is non-empty).
- "2" is only the background candidate set and constraint supplement, to avoid missing key restrictions; do not rewrite from scratch just because `full` contains another approach.
- If delta and full are inconsistent in recommendations: use delta as the source of truth; however, do not violate `data_recommendations` or syntax rules.
- If delta is empty: then use full.

##### 1. Current newly added knowledge delta (use first; may be empty)

Task knowledge (delta, most recent addition):
{task_knowledge_delta}

Operator knowledge (delta, most recent addition):
{operator_knowledge_delta}

##### 2. Full knowledge (background reference only; truncated)

Task knowledge (full preview):
 {task_knowledge_full_preview}

Operator knowledge (full preview):
 {operator_knowledge_full_preview}

---
### [D. Repair Task (output requirements)]

Based on the information above, make the minimum necessary modifications to `previous_code` to fix the error. The user language mode is LANG={user_lang}

- Repair only: do not add unrelated extensions, and do not add processing steps unrelated to the repair.
- Minimum change: preserve the structure and variable names of `previous_code` as much as possible.
- If an API parameter/operator call needs to be replaced: change only that local part and ensure the overall code remains runnable.
- When LANG is `en`, this is English mode only for user-facing content; related knowledge and internal workflow logic should still mainly follow Chinese, while comments in the code should be in English. Otherwise, use Chinese mode by default.
- Output only the final repaired OGE code body. Do not output explanations, Markdown, or JSON.



According to the requirements, repair the code and output only the raw OGE code body. Do not output any explanations, titles, notes, prefixes, or suffixes, and do not put the code in a Markdown code block.
