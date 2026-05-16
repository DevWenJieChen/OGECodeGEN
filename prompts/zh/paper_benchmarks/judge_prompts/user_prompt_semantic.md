本评委的评审视角：
{judge_style}

你这次只负责评价以下两个维度：
- data_adherence
- semantic_faithfulness

不要评价 task_fulfillment、parameter_validity、output_quality、result_plausibility，
不要输出它们，也不要尝试给 overall 总分。你必须严格只输出当前 schema 要求的字段，不得补充其他评分项。

注意：

- verify_ok=True 只表示代码通过了基本运行校验，不代表任务一定真正完成。
- executability_ok=False，或 dag_json_state 为 missing / empty，说明存在“伪验证通过”或“无有效执行链”；这会影响你对语义实现真实性的判断，但你只需从数据与语义角度评价。
- gold_code 是参考实现，不是唯一正确答案。
- data_ref 在本实验中通常是生成阶段的弱参考/保底参考；评测时不得仅因 pred_code 未直接采用 data_ref 中的示例数据就判错。
- 你的重点是：任务语义是否一致、数据角色是否正确、是否把任务改成了另一个问题。

需要评价的信息如下：

=== 任务描述 ===
{description}

=== 参考的数据 ===
{data_ref}

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