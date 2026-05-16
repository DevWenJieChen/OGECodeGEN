本评委的评审视角：
{judge_style}

你这次只负责评价以下两个维度：
- task_fulfillment
- output_quality

不要评价 data_adherence、semantic_faithfulness、parameter_validity、result_plausibility，
不要输出它们，也不要尝试给 overall 总分。你必须严格只输出当前 schema 要求的字段，不得补充其他评分项。

注意：

- verify_ok=True 只表示代码通过了基本运行校验，不代表任务一定真正完成。
- executability_ok=False，或 dag_json_state 为 missing / empty，通常意味着代码没有形成有效处理链；这类情况不得按“任务基本完成”处理。
- gold_code 是参考实现，不是唯一正确答案。
- 你的重点是：任务是否真正闭环完成，是否产出了用户可用的最终结果。

需要评价的信息如下：

=== 任务描述 ===
{description}

=== verify_ok ===
{verify_ok}

=== executability_ok ===
{executability_ok}

=== dag_json_state ===
{dag_json_state}

=== verify_report ===
{verify_report}

=== 参考代码 ===
{gold_code}

=== 生成的代码 ===
{pred_code}