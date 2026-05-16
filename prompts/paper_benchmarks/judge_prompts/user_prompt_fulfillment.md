This judge's evaluation perspective:
{judge_style}

This time you are responsible for evaluating only the following two dimensions:
- task_fulfillment
- output_quality

Do not evaluate data_adherence, semantic_faithfulness, parameter_validity, or result_plausibility.
Do not output them, and do not attempt to give an overall score. You must strictly output only the fields required by the current schema and must not add other scoring items.

Notes:

- `verify_ok=True` only means the code passed basic runtime verification; it does not mean the task is truly completed.
- `executability_ok=False`, or `dag_json_state` being `missing` / `empty`, usually means the code did not form a valid processing chain. Such cases must not be treated as "the task is basically completed".
- `gold_code` is a reference implementation, not the only correct answer.
- Your focus is whether the task is truly completed end-to-end and whether it produces a final result usable by the user.

Information to evaluate:

=== Task Description ===
{description}

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
