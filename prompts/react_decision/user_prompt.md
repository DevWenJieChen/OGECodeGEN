### Current Information

- observation: summary information of the current PipelineState (including verify_report)
- history: recent execution history (up to the most recent 6 records)
- lang: user's language mode (default zh; zh means Chinese, en means English)

[observation]
{observation}

[history]
{history}

[lang]
{user_lang}

[Action whitelist for this repair]
Allowed ACTION whitelist for this repair:
{allow_actions}



### Task Requirements

- Determine the current error type (syntax / parameter / API / missing operator / missing data / uncertain)
- Output the actions for the next repair attempt (single action or sequence; must be from this repair's whitelist)
- Output necessary params (following the system prompt constraints)
- Output a brief `reason`
- `reason` must be in Chinese. If the user's language mode `lang` is `en`, add a `reason_en` field whose content is the English version of the `reason` field.



Output only one strict JSON object. Do not output anything else.
