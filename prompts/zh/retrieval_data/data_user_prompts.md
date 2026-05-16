【用户原始问句】
{user_query}



【意图理解输出（JSON 原文）】
{intent_json}



【参考数据信息（data_info，可能为 null）】
{data_info}

说明：
- data_info 来自 benchmark 中给定的参考数据约束。
- 若 data_info 中包含 productID、coverageID、featureId、上传文件名或 CRS，应优先作为数据读取推荐依据。
- data_info 只用于确定数据读取方案，不改变用户任务目标。



【data_constraints（原样字符串或 null）】
{data_constraints}



【关键词命中结果（data_constraints 与 product_keyword.keyword 的相似匹配结果）】
{keyword_hits}



【候选数据产品知识（JSON 数组，包含产品自身 time_range 与 spatial_extent）】

（说明：下列数据中已包含可直接使用的 time_range 与 spatial_extent 数值，请优先直接使用，不要仅用概括性描述替代；产品中的name 字段是OGE平台代码中使用的 productID，product_id 字段仅为内部数值标识，不得用于代码生成文本）

{candidate_products}



【候选产品关联的景级数据（JSON 对象：product_name -> scenes[]，包含 scene 的 time_range 与 bbox，可能为空）】

（说明：若 scene 中给出了 time_range 与 bbox，请在推荐文本优先使用这些数值）

{candidate_scenes}



【用户任务区域 bbox（task_bbox，可能为 null；它表示用户请求区域，不等同于数据覆盖范围）】
{task_bbox}



【输出语言模式】
{user_lang}




---

请严格按照 system 指令输出 JSON，如果语言模式是en，说明是英文模式，那么输出的JSON请追加一个字段en_info，该字段与recommendations完全相同，不过里面的内容是英文，也就是用英文描述。