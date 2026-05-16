### User requirement:
{user_query}

### Language mode (zh means Chinese, en means English):

{user_lang}

Note that, based on the system instructions, you need to decide whether to append an additional field according to the language. The rules are:

- If the mode is zh / Chinese, generate normally according to the requirements. The main intent JSON should use Chinese textual values, except for fixed enum values or domain identifiers such as `task_type`, product IDs, and terms like NDVI. Example:
  - User input: 计算2024年5月武汉地区的NDVI情况。
  - Model output (JSON):
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
- If the mode is en / English, the main intent recognition result should still be in Chinese, while an English display version must be provided by appending an `en_info` field. The fields inside `en_info` are the same as the intent structure, but with `user_query_zh` and `user_query_en` added to represent the user description in Chinese and English. Descriptive domain phrases may be translated, such as translating “洪涝淹没范围” as “Flood inundation extent”; however, domain proper nouns such as NDVI do not need translation. Example:
  - User input: Analyze the flood inundation extent in the Wuhan area during May 2024
  - Model output (JSON):
    {{
      "time_range": "2024-05-01 00:00 ~ 2024-05-31 23:59",
      "space_region": "武汉",
      "object_type": "洪涝淹没范围",
      "task_type": "quantitative_remote_sensing",
      "data_constraints": null,
      "required_outputs": [
        "洪涝淹没范围图"
      ],
      "en_info": {{
          "user_query_zh": "分析2024年5月武汉地区的洪涝淹没范围情况。",
          "user_query_en": "Analyze the flood inundation extent in the Wuhan area during May 2024",
          "time_range": "2024-05-01 00:00 ~ 2024-05-31 23:59",
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

Output the task JSON that satisfies the requirements. No other content is needed; the content must be in the required JSON format.
