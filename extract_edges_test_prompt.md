# Extract Edges 测试 Prompt

## System Message

```
You are an expert fact extractor that extracts fact triples from text. 
1. Extracted fact triples should also be extracted with relevant date information.
2. Treat the CURRENT TIME as the time the CURRENT MESSAGE was sent. All temporal information should be extracted relative to this time.
```

## User Message

```
<FACT TYPES>
[
  {
    "fact_type_name": "LOCATED_IN",
    "fact_type_signature": ["Person", "Location"],
    "fact_type_description": "Represents a person being physically present at a location"
  },
  {
    "fact_type_name": "INTERACTS_WITH",
    "fact_type_signature": ["Person", "Person"],
    "fact_type_description": "Represents interaction between two people"
  },
  {
    "fact_type_name": "USES",
    "fact_type_signature": ["Person", "Device"],
    "fact_type_description": "Represents a person using a device or tool"
  },
  {
    "fact_type_name": "PERFORMS",
    "fact_type_signature": ["Person", "Activity"],
    "fact_type_description": "Represents a person performing an activity"
  },
  {
    "fact_type_name": "FOLLOWS",
    "fact_type_signature": ["Person", "Plan"],
    "fact_type_description": "Represents a person following a plan or program"
  },
  {
    "fact_type_name": "GUIDES",
    "fact_type_signature": ["Person", "Person"],
    "fact_type_description": "Represents one person guiding or instructing another"
  },
  {
    "fact_type_name": "TARGETS",
    "fact_type_signature": ["Activity", "Entity"],
    "fact_type_description": "Represents an activity targeting a specific body part or goal"
  }
]
</FACT TYPES>

<PREVIOUS_MESSAGES>
[
  "用户在大风天气中骑行一种交通工具，周围伴有持续的强风声、车辆颠簸声和轻微的马达声。"
]
</PREVIOUS_MESSAGES>

<CURRENT_MESSAGE>
用户进入健身房与教练会面。教练在确认用户身体状况后，提出了"三分化"训练方案，并指导用户使用泡沫轴进行胸椎部位的热身准备活动。

关键引用：
- [226.4s-240.0s] 教练说："可以把你分成三分化，然后肩背放在一起练，然后胸跟手臂放在一起练，腿跟核心放在一起练。"

SVO要点：
- 用户-进入-健身房
- 教练-与用户-讨论训练计划
- 教练-提出-"三分化健身训练计划"
- 教练-指导用户-使用泡沫轴进行热身
- 用户-进行-胸椎放松活动
</CURRENT_MESSAGE>

<ENTITIES>
[
  {"id": 0, "name": "用户", "entity_types": ["Entity", "Person"]},
  {"id": 1, "name": "教练", "entity_types": ["Entity", "Person"]},
  {"id": 2, "name": "健身房", "entity_types": ["Entity", "Location"]},
  {"id": 3, "name": "exercise", "entity_types": ["Entity", "Activity"]},
  {"id": 4, "name": "training", "entity_types": ["Entity", "Activity"]},
  {"id": 5, "name": "guided_instruction", "entity_types": ["Entity", "Activity"]},
  {"id": 6, "name": "热身准备活动", "entity_types": ["Entity", "Activity"]},
  {"id": 7, "name": "胸椎放松活动", "entity_types": ["Entity", "Activity"]},
  {"id": 8, "name": "泡沫轴", "entity_types": ["Entity", "Device"]},
  {"id": 9, "name": "训练计划", "entity_types": ["Entity", "Plan"]},
  {"id": 10, "name": "三分化健身训练计划", "entity_types": ["Entity", "Plan"]},
  {"id": 11, "name": "胸椎", "entity_types": ["Entity"]},
  {"id": 12, "name": "肩背", "entity_types": ["Entity"]}
]
</ENTITIES>

<REFERENCE_TIME>
2025-09-30T12:32:05Z  # ISO 8601 (UTC); used to resolve relative time mentions
</REFERENCE_TIME>

# TASK
Extract all factual relationships between the given ENTITIES based on the CURRENT MESSAGE.
Only extract facts that:
- involve two DISTINCT ENTITIES from the ENTITIES list,
- are clearly stated or unambiguously implied in the CURRENT MESSAGE,
    and can be represented as edges in a knowledge graph.
- Facts should include entity names rather than pronouns whenever possible.
- The FACT TYPES provide a list of the most important types of facts, make sure to extract facts of these types
- The FACT TYPES are not an exhaustive list, extract all facts from the message even if they do not fit into one
    of the FACT TYPES
- The FACT TYPES each contain their fact_type_signature which represents the source and target entity types.

You may use information from the PREVIOUS MESSAGES only to disambiguate references or support continuity.



# EXTRACTION RULES

1. **Entity ID Validation**: `source_entity_id` and `target_entity_id` must use only the `id` values from the ENTITIES list provided above.
   - **CRITICAL**: Using IDs not in the list will cause the edge to be rejected
2. Each fact must involve two **distinct** entities.
3. Use a SCREAMING_SNAKE_CASE string as the `relation_type` (e.g., FOUNDED, WORKS_AT).
4. Do not emit duplicate or semantically redundant facts.
5. The `fact` should closely paraphrase the original source sentence(s). Do not verbatim quote the original text.
6. Use `REFERENCE_TIME` to resolve vague or relative temporal expressions (e.g., "last week").
7. Do **not** hallucinate or infer temporal bounds from unrelated events.

# DATETIME RULES

- Use ISO 8601 with "Z" suffix (UTC) (e.g., 2025-04-30T00:00:00Z).
- If the fact is ongoing (present tense), set `valid_at` to REFERENCE_TIME.
- If a change/termination is expressed, set `invalid_at` to the relevant timestamp.
- Leave both fields `null` if no explicit or resolvable time is stated.
- If only a date is mentioned (no time), assume 00:00:00.
- If only a year is mentioned, use January 1st at 00:00:00.
```

---

## 期望的响应格式 (JSON Schema)

```json
{
  "edges": [
    {
      "relation_type": "LOCATED_IN",
      "source_entity_id": 0,
      "target_entity_id": 2,
      "fact": "用户进入健身房与教练会面",
      "valid_at": "2025-09-30T12:32:05Z",
      "invalid_at": null
    },
    {
      "relation_type": "INTERACTS_WITH",
      "source_entity_id": 0,
      "target_entity_id": 1,
      "fact": "用户与教练在健身房会面并讨论训练计划",
      "valid_at": "2025-09-30T12:32:05Z",
      "invalid_at": null
    },
    {
      "relation_type": "GUIDES",
      "source_entity_id": 1,
      "target_entity_id": 0,
      "fact": "教练指导用户使用泡沫轴进行热身准备活动",
      "valid_at": "2025-09-30T12:32:05Z",
      "invalid_at": null
    },
    {
      "relation_type": "FOLLOWS",
      "source_entity_id": 0,
      "target_entity_id": 10,
      "fact": "教练向用户提出三分化健身训练计划",
      "valid_at": "2025-09-30T12:32:05Z",
      "invalid_at": null
    },
    {
      "relation_type": "USES",
      "source_entity_id": 0,
      "target_entity_id": 8,
      "fact": "用户使用泡沫轴进行热身",
      "valid_at": "2025-09-30T12:32:05Z",
      "invalid_at": null
    },
    {
      "relation_type": "PERFORMS",
      "source_entity_id": 0,
      "target_entity_id": 7,
      "fact": "用户进行胸椎放松活动",
      "valid_at": "2025-09-30T12:32:05Z",
      "invalid_at": null
    },
    {
      "relation_type": "TARGETS",
      "source_entity_id": 7,
      "target_entity_id": 11,
      "fact": "胸椎放松活动针对胸椎部位",
      "valid_at": null,
      "invalid_at": null
    }
  ]
}
```

---

## 使用说明

1. **复制 System Message 和 User Message** 到你的 LLM 测试界面
2. 确保 LLM 配置为返回 JSON 格式
3. 响应应该符合 `ExtractedEdges` 的 Pydantic schema
4. 检查所有的 `source_entity_id` 和 `target_entity_id` 必须在 0-12 范围内（对应 ENTITIES 列表）

## 关键验证点

- ✅ 所有实体 ID 必须存在于 ENTITIES 列表中
- ✅ source 和 target 必须是不同的实体
- ✅ relation_type 使用 SCREAMING_SNAKE_CASE
- ✅ fact 是自然语言描述，不是原文引用
- ✅ 时间使用 ISO 8601 格式（如果适用）
- ✅ 只提取 CURRENT_MESSAGE 中明确或暗示的关系

