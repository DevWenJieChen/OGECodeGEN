This judge's evaluation perspective:
{judge_style}

This time you are responsible for evaluating only the following two dimensions:
- parameter_validity
- result_plausibility

Do not evaluate task_fulfillment, data_adherence, semantic_faithfulness, or output_quality.
Do not output them, and do not attempt to give an overall score. You must strictly output only the fields required by the current schema and must not add other scoring items.

Notes:

- `verify_ok=True` only means the code passed basic runtime verification; it does not mean parameters and results are necessarily reasonable.
- `executability_ok=False`, or `dag_json_state` being `missing` / `empty`, means result credibility should be significantly lowered.
- `gold_code` is a reference implementation, not the only correct answer.
- In this experiment, `data_ref` is usually a weak/fallback reference from the generation stage. During evaluation, do not mark wrong solely because `pred_code` does not directly use the example data in `data_ref`.
- Your focus is whether bands, formulas, product levels, scale/offset, thresholds, windows, and temporal phases are reasonable, and whether the result appears credible.
- If you cannot see the actual map, image, or statistical output, do not assert without evidence that the result is empty, single-colored, or abnormal. Judge result credibility only based on provable issues in the code logic.

Information to evaluate:

=== Task Description ===
{description}

=== Reference Data ===
{data_ref}

=== verify_ok ===
{verify_ok}

=== executability_ok ===
{executability_ok}

=== dag_json_state ===
{dag_json_state}

=== verify_report ===
{verify_report}

=== Reference Code ===
{gold_code}

=== Generated Code ===
{pred_code}
