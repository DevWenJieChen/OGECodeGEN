### 当前信息

- observation：当前 PipelineState 的摘要信息（含 verify_report）
- history：最近执行历史（最多最近 6 条）
- lang：用户的语言模式（默认zh，zh为中文，en为英文）

【observation】
{observation}

【history】
{history}

【lang】
{user_lang}

【本次修复的action白名单】
本次修复中，允许的 ACTION 白名单：
{allow_actions}



### 任务要求

- 判断当前属于哪种错误类型（语法/参数/API/缺算子/缺数据/不确定）
- 输出下一次修复尝试（repair attempt）的 actions（单个或序列，必须是本次修复的）
- 输出必要的 params（遵循系统提示词的约束）
- 输出简短的reason
- reason一定是中文的，但如果用户的语言模式lang是en，请增加一个reason_en字段，这个字段的内容是reason字段的英文版。



请只输出一个严格 JSON 对象，禁止输出其他任何内容。

