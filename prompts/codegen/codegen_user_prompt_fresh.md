### [User Question]

{user_query}

==================================================

### [Intent Understanding Result (task intent, JSON string)]

Description: The following is the structured output from the upstream "intent understanding module" (usually a JSON string). It is used to clarify task objectives, target objects, spatiotemporal constraints, etc.
INTENT JSON:
{intent_json}

==================================================

### [Data Retrieval Result (data reading recommendations)]

Description: The following is the recommendation result from the upstream "data retrieval module" (usually a JSON string). Each recommendation corresponds to a candidate data product and includes:

- product_id / product_name
- sample_data_text (a `getCoverage` scheme if a concrete coverageID can be determined)
- collection_data_text (fallback scheme such as `getCoverageCollection`)
- bands (band information for the data product)

DATA RECOMMENDATIONS:
{data_recommendations}

==================================================

### [Task Knowledge (case/experience reference, not the planning result)]

Description: The following is case/experience knowledge retrieved by the "knowledge retrieval module" that is similar to the current task. It can provide possible processing-step order and method references, but must be selected and adapted according to the intent and data constraints.
TASK KNOWLEDGE (REFERENCE):
{task_knowledge}

==================================================

### [Task-Step Reference (general steps, not implementation details)]

Description: This is the general step list produced by the knowledge retrieval module during operator retrieval after task decomposition. It is only used to help organize the structure and order of the processing chain.
Strict constraints:

- Steps must not be used as a basis for hard-coded implementation
- Do not infer or hard-code any concrete code parameters from the steps, such as band numbers/names/operator APIs

TASK STEPS (REFERENCE):
{task_steps}

==================================================

### [Operator Knowledge (candidate operator set)]

Description: The following is the retrieved potentially relevant operator knowledge, including operator names, function descriptions, inputs/outputs, and key parameter hints.
You may only select and compose from these candidate operators. **Creating new operators out of thin air is strictly prohibited.**

OPERATOR KNOWLEDGE (CANDIDATES):
{operator_knowledge}

==================================================

### [Language of the User Question]

Description: `en` means English, and `zh` means Chinese.
LANG:
{user_lang}

==================================================

### [Code Generation Task Instructions]

Complete the following:

1) Choose a suitable data reading method by combining INTENT JSON and DATA RECOMMENDATIONS.
   - Other alternative data reading methods may be mentioned only when necessary.
2) Use TASK STEPS and TASK KNOWLEDGE to organize the processing chain (adapt or simplify as needed), ensuring a clear data flow:
   - Use clear variable names; the output of the previous step should be the input of the next step
   - Add comments for key steps (such as data loading, clipping/masking, index calculation, statistical analysis, export/rendering)
   - Select only steps explicitly required by the description; do not perform unrequested processing (such as extra indices, extra statistics, or extra exports)
3) When LANG is `en`, write comments for English-speaking users in English; otherwise, use Chinese comments by default.
4) Select suitable operators from the OPERATOR KNOWLEDGE candidate set to implement key steps:
   - Do not use operator names outside the candidate set
   - Prefer operators that match the current data product/data type and satisfy the task objective
   - If the candidate set is insufficient for a step, first try composition; if still impossible, use a brief Chinese comment to describe the capability gap, and never write guessed operator names
   - Operator parameter semantics and input/output structures must follow the operator knowledge base definitions
5) Do not make the code overly complex. Prioritize clarity and directness, and do not compromise syntax checking or safety validation.
6) Comments must be concise and not verbose.
7) Band information must strictly follow the `bands` field in DATA RECOMMENDATIONS; otherwise execution may fail.
8) The whole code must strictly follow the task requirements. Follow the minimum-sufficient principle: implement only the minimal necessary steps that satisfy the task; do not add extra indices, extra visualizations, extra exports, extra explanatory code, etc.

Output only the raw OGE code body according to the requirements. Do not output explanations, titles, notes, prefixes, or suffixes, and do not wrap the code in a Markdown code block.
