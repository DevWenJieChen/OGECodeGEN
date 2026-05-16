[User Original Question]
{user_query}



[Intent Understanding Output (raw JSON)]
{intent_json}



[Reference Data Information (`data_info`, may be null)]
{data_info}

Description:
- `data_info` comes from the reference data constraints given in the benchmark.
- If `data_info` contains productID, coverageID, featureId, uploaded file name, or CRS, it should be used preferentially as the basis for data reading recommendations.
- `data_info` is only used to determine the data reading plan and does not change the user task objective.



[`data_constraints` (raw string or null)]
{data_constraints}



[Keyword Hit Results (similarity matching between `data_constraints` and `product_keyword.keyword`)]
{keyword_hits}



[Candidate Data Product Knowledge (JSON array, including the product's own `time_range` and `spatial_extent`)]

(Description: The following data already contains directly usable `time_range` and `spatial_extent` values. Please use these values directly first, rather than replacing them with only generic descriptions. The `name` field in a product is the productID used in OGE platform code; the `product_id` field is only an internal numeric identifier and must not be used in code generation text.)

{candidate_products}



[Scene-Level Data Associated with Candidate Products (JSON object: product_name -> scenes[], containing scene `time_range` and `bbox`; may be empty)]

(Description: If a scene provides `time_range` and `bbox`, prefer using these values in the recommendation text.)

{candidate_scenes}



[User Task-Region bbox (`task_bbox`, may be null; it represents the user-requested task area and is not the same as data coverage)]
{task_bbox}



[Output Language Mode]
{user_lang}




---

Strictly follow the system instructions and output JSON. If the language mode is `en`, this indicates English display mode; append an `en_info` field to the output JSON. The main `recommendations` field should remain the primary Chinese recommendation result, while `en_info` should be exactly the same as `recommendations` except that its content is described in English.
