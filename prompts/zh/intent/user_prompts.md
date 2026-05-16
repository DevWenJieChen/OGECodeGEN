### 用户需求如下：
{user_query}

### 语言模式如下(zh为中文，en为英文)：

{user_lang}

需要注意，在system指令的基础上，需要根据语言来判断是否追加一个字段，规则如下：

- 如果是zh中文，那么按照要求正常生成即可，示例如下：
  - 用户输入：计算2024年5月武汉地区的NDVI情况。
  - 模型输出(JSON)：
    {{
      "time_range": "2024-05-01 00:00 ~ 2024-05-31 23:59",
      "space_region": "武汉",
      "object_type": "NDVI",
      "task_type": "quantitative_remote_sensing",
      "data_constraints": null,
      "required_outputs": [
        "NDVI结果图"
      ]
    }}
- 如果是en英文，意图识别的结果还是中文，但是提供一个英文版本的信息，也就是需要追加一个字段en_info（里面的字段就是意图理解的结构，但是里面增加user_query_zh与user_query_en，表示中英文的用户描述user_query），用于展示给用户看；翻译时，面向描述可以翻译，如洪涝淹没范围可以翻译为Flood inundation extent；但是注意领域专有名词，如ndvi等，就不用翻译。示例如下：
  - 用户输入：Analyze the flood inundation extent in the Wuhan area during May 2024
  - 模型输出(JSON)：
    {{
      "time_range": "2024-05-01 00:00 ~ 2024-05-31 23:59",
      "space_region": "武汉",
      "object_type": "洪涝淹没范围",
      "task_type": "quantitative_remote_sensing",
      "data_constraints": null,
      "required_outputs": [
        "洪涝淹没范围图"
      ],
      "en_info":{{
          "user_query_zh": "分析2024年5月武汉地区的洪涝淹没范围情况。",
          "user_query_en": "Analyze the flood inundation extent in the Wuhan area during May 2024"，
          "time_range": "2020-05-01 00:00 ~ 2020-05-31 23:59",
          "space_region": "Wuhan",
          "object_type": "Flood inundation extent",
          "task_type": "quantitative_remote_sensing",
         "data_constraints": null,
         "required_outputs": [
            "Flood inundation extent map"
          ]
          }}
    }}

---

请输出符合要求的任务 JSON，注意不需要任何其他内容，必须是内容必须是要求的JSON格式。