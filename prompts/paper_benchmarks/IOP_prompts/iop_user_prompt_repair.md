### [Repair Objective]

You need to make the minimum necessary modifications to `previous_code` according to `verify_report`, so that it passes verification by fixing the issues exposed by `verify_report`.

---

### [A. Current Repair Information (highest priority)]

1) verify_report (error basis; must be satisfied first):
  {verify_report}

2) previous_code (must be minimally modified based on it):

```python
{previous_code}
```

---

### [B. Background Task Context (used to keep the target aligned; not for rewriting from scratch)]

1. User question

{user_query}

---
2. Data recommendations
{data_info}

---
---
### [D. Repair Task (output requirements)]

Based on the information above, make the minimum necessary modifications to `previous_code` to fix the error. The user language mode is LANG={user_lang}

- Repair only: do not add unrelated extensions, and do not add processing steps unrelated to the repair.
- Minimum change: preserve the structure and variable names of `previous_code` as much as possible.
- If an API parameter/operator call needs to be replaced: change only that local part and ensure the overall code remains runnable.
- When LANG is `en`, this is English mode only for user-facing content; related knowledge and internal workflow logic should still mainly follow Chinese, while comments in the code should be in English. Otherwise, use Chinese mode by default.
- Output only the final repaired OGE code. Do not output explanations, Markdown, or JSON. Add a comment near the modified location to explain the reason.
