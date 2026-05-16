### 【修复目标】

你需要根据 fix_instruction对previous_code做最小必要修改，使其通过验证（修复 verify_report 暴露的问题）。

---

### 【A.本轮修复信息（最高优先级）】

1) fix_instruction（优先参考）：
{fix_instruction}

2) verify_report（错误依据，必须优先满足）：
{verify_report}

3) previous_code（必须基于它做最小改动）：
```python
{previous_code}
```

---

### 【B.背景任务上下文（用于保持目标不跑偏，不能用于推倒重写）】

1.用户问题

{user_query}

---
2.任务意图（只作为目标，不是逐字照抄）

intent_json：
{intent_json}

---
3.数据检索建议（数据读取约束；涉及波段/数据读取方式必须以此为准）

data_recommendations：
{data_recommendations}

---
### 【C.知识依据（用于修复时选择/补齐算子；注意优先级与使用规则）】

使用规则（强约束）：

- 你必须优先使用“ 1”中提供的新增算子/案例来修复本轮错误（如果 delta 非空）。
- “2”仅作为背景候选集合与约束补充，避免遗漏关键限制；不要因为 full 中有别的方案就推倒重写。
- 若 delta 与 full 在建议上不一致：以 delta 为准；但不得违反 data_recommendations 与语法规则。
- 若 delta 为空：再使用 full。

##### 1.本轮新增知识 delta（优先使用；可能为空）

任务知识（delta，最近一次新增）：
{task_knowledge_delta}

算子知识（delta，最近一次新增）：
{operator_knowledge_delta}

##### 2.全量知识 full（仅做背景参考；已做截断）

任务知识（full 预览）：
 {task_knowledge_full_preview}

算子知识（full 预览）：
 {operator_knowledge_full_preview}

---
### 【D.修复任务（输出要求）】

 请基于上述信息，对 previous_code 做最小必要修改以修复错误。用户语言模式是LANG={user_lang}

- 仅修复：不要无关扩展；不要新增与修复无关的处理步骤。
- 最小改动：尽量保留 previous_code 的结构与变量命名。
- 若需要替换某个 API 参数/算子调用：只改动该局部，并保证整体代码仍可运行。
- 当用户语言模式LANG为en的时候，说明是英文模式（仅面向用户展示，但是相关知识与内部流程逻辑还是以中文为主），代码中的注释需要是英文的；否则默认是中文模式
- 只输出最终修复后的 OGE 代码正文；不要解释、不要 Markdown、不要 JSON。



请按照要求，修复代码，必须只输出纯OGE代码正文，不得输出任何解释、标题、说明、前后缀，不得将代码放入Markdown代码块。

