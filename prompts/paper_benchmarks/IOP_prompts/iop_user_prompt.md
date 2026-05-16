### [User Question]

{user_query}

==================================================

### [Available Data]

Description: The following is the available data product information, including:

- coverageID: string literal, the identifier of a scene/image (for example: LC08_L1GT_121039_20240325_20240403_02_T2)
- productID: string literal, the product name (for example `"LC08_C02_L1"`)

{data_info}

==================================================

### [Language of the User Question]

Description: `en` means English, and `zh` means Chinese.
LANG:
{user_lang}

==================================================

### [Code Generation Task Instructions]

Complete the following:

1) Choose a suitable data reading method based on [Available Data].
   - Other alternative data reading methods may also be written, but they must be commented out so that users can switch later.
2) When LANG is `en`, write comments for English-speaking users in English; otherwise, use Chinese comments by default.
3) Do not make the code overly complex. Prioritize clarity and directness, and do not compromise syntax checking or safety validation.

Output only the final OGE code according to the requirements.
