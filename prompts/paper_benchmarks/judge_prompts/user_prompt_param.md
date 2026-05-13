本评委的评审视角：
{judge_style}

你这次只负责评价以下两个维度：
- parameter_validity
- result_plausibility

不要评价 task_fulfillment、data_adherence、semantic_faithfulness、output_quality，
不要输出它们，也不要尝试给 overall 总分。你必须严格只输出当前 schema 要求的字段，不得补充其他评分项。

注意：

- verify_ok=True 只表示代码通过了基本运行校验，不代表参数和结果一定合理。
- executability_ok=False，或 dag_json_state 为 missing / empty，说明结果可信性应显著下调。
- gold_code 是参考实现，不是唯一正确答案。
- data_ref 在本实验中通常是生成阶段的弱参考/保底参考；评测时不得仅因 pred_code 未直接采用 data_ref 中的示例数据就判错。
- 你的重点是：band、公式、产品层级、scale/offset、阈值、窗口、时相是否合理，结果是否看起来可信。
- 若无法看到实际地图、图像或统计输出，不得凭空断言结果为空、单色或反常；只能根据代码逻辑中可证明的问题判断结果可信性。

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