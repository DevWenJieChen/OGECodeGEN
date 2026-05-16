This judge's evaluation perspective:
{judge_style}

This time you are responsible for evaluating only the following two dimensions:
- data_adherence
- semantic_faithfulness

Do not evaluate task_fulfillment, parameter_validity, output_quality, or result_plausibility.
Do not output them, and do not attempt to give an overall score. You must strictly output only the fields required by the current schema and must not add other scoring items.

Notes:

- `verify_ok=True` only means the code passed basic runtime verification; it does not mean the task is truly completed.
- `executability_ok=False`, or `dag_json_state` being `missing` / `empty`, indicates "pseudo verification pass" or "no valid execution chain"; this affects your judgment of the authenticity of semantic implementation, but you only need to evaluate from the data and semantic perspectives.
- `gold_code` is a reference implementation, not the only correct answer.
- In this experiment, `data_ref` is usually a weak/fallback reference from the generation stage. During evaluation, do not mark wrong solely because `pred_code` does not directly use the example data in `data_ref`.
- Your focus is whether the task semantics are consistent, whether the data role is correct, and whether the task has been changed into another problem.

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
