# Extract Node Attributes 测试 Prompt

## 场景 1: 提取实体属性 (extract_attributes)

### System Message
```
You are a helpful assistant that extracts entity properties from the provided text.
```

### User Message
```
Given the MESSAGES and the following ENTITY, update any of its attributes based on the information provided in MESSAGES. Use the provided attribute descriptions to better understand how each attribute should be determined.

Guidelines:
1. Do not hallucinate entity property values if they cannot be found in the current context.
2. Only use the provided MESSAGES and ENTITY to set attribute values.

<MESSAGES>
["用户在大风天气中骑行一种交通工具，周围伴有持续的强风声、车辆颠簸声和轻微的马达声。"]
["用户进入健身房与教练会面。教练在确认用户身体状况后，提出了"三分化"训练方案，并指导用户使用泡沫轴进行胸椎部位的热身准备活动。关键引用：[226.4s-240.0s] 教练说："可以把你分成三分化，然后肩背放在一起练，然后胸跟手臂放在一起练，腿跟核心放在一起练。"SVO要点：用户-进入-健身房；教练-与用户-讨论训练计划；教练-提出-"三分化健身训练计划"；教练-指导用户-使用泡沫轴进行热身；用户-进行-胸椎放松活动"]
</MESSAGES>

<ENTITY>
{
  "name": "教练",
  "entity_types": ["Entity", "Person"],
  "attributes": {
    "specialization": null,
    "training_style": null,
    "communication_style": null
  }
}
</ENTITY>
```

### 期望的响应格式 (JSON Schema)

假设我们定义了一个 `Trainer` 实体类型：

```python
class Trainer(BaseModel):
    """健身教练实体"""
    specialization: str | None = Field(None, description="教练的专业领域，如力量训练、有氧训练、康复训练等")
    training_style: str | None = Field(None, description="训练风格，如严格、温和、科学化等")
    communication_style: str | None = Field(None, description="沟通方式，如详细解释、简洁直接、鼓励型等")
```

---

## 场景 2: 提取实体摘要 (extract_summary)

### System Message
```
You are a helpful assistant that extracts entity summaries from the provided text.
```

### User Message
```
Given the MESSAGES and the ENTITY, update the summary that combines relevant information about the entity from the messages and relevant information from the existing summary.

Guidelines:
1. Output only factual content. Never explain what you're doing, why, or mention limitations/constraints. 
2. Only use the provided messages, entity, and entity context to set attribute values.
3. Keep the summary concise and to the point. STATE FACTS DIRECTLY IN UNDER 250 CHARACTERS.

Example summaries:
BAD: "This is the only activity in the context. The user listened to this song. No other details were provided to include in this summary."
GOOD: "User played 'Blue Monday' by New Order (electronic genre) on 2024-12-03 at 14:22 UTC."
BAD: "Based on the messages provided, the user attended a meeting. This summary focuses on that event as it was the main topic discussed."
GOOD: "User attended Q3 planning meeting with sales team on March 15."
BAD: "The context shows John ordered pizza. Due to length constraints, other details are omitted from this summary."
GOOD: "John ordered pepperoni pizza from Mario's at 7:30 PM, delivered to office."

<MESSAGES>
["用户在大风天气中骑行一种交通工具，周围伴有持续的强风声、车辆颠簸声和轻微的马达声。"]
["用户进入健身房与教练会面。教练在确认用户身体状况后，提出了"三分化"训练方案，并指导用户使用泡沫轴进行胸椎部位的热身准备活动。关键引用：[226.4s-240.0s] 教练说："可以把你分成三分化，然后肩背放在一起练，然后胸跟手臂放在一起练，腿跟核心放在一起练。""]
</MESSAGES>

<ENTITY>
{
  "name": "教练",
  "entity_types": ["Entity", "Person"],
  "summary": "",
  "attributes": {}
}
</ENTITY>
```

### 期望的响应格式 (JSON Schema)

```python
class EntitySummary(BaseModel):
    summary: str = Field(..., description="Concise entity summary under 250 characters")
```


---

## 场景 3: 完整的节点属性提取流程

### 测试实体：用户

#### Step 1: Extract Attributes

**System**: You are a helpful assistant that extracts entity properties from the provided text.

**User**:
```
Given the MESSAGES and the following ENTITY, update any of its attributes based on the information provided in MESSAGES.

<MESSAGES>
["用户在大风天气中骑行一种交通工具，周围伴有持续的强风声、车辆颠簸声和轻微的马达声。"]
["用户进入健身房与教练会面。教练在确认用户身体状况后，提出了"三分化"训练方案，并指导用户使用泡沫轴进行胸椎部位的热身准备活动。"]
</MESSAGES>

<ENTITY>
{
  "name": "用户",
  "entity_types": ["Entity", "Person"],
  "attributes": {
    "fitness_level": null,
    "training_frequency": null,
    "health_concerns": null
  }
}
</ENTITY>
```

假设定义了 `User` 实体：

```python
class User(BaseModel):
    """用户实体"""
    fitness_level: str | None = Field(None, description="健身水平：初学者、中级、高级")
    training_frequency: str | None = Field(None, description="训练频率")
    health_concerns: str | None = Field(None, description="健康问题或关注点")
```


#### Step 2: Extract Summary

**System**: You are a helpful assistant that extracts entity summaries from the provided text.

**User**:
```
Given the MESSAGES and the ENTITY, update the summary that combines relevant information about the entity from the messages.

Guidelines:
1. Output only factual content.
2. Keep the summary concise and to the point. STATE FACTS DIRECTLY IN UNDER 250 CHARACTERS.

<MESSAGES>
["用户在大风天气中骑行一种交通工具。"]
["用户进入健身房与教练会面，接受三分化训练方案指导，使用泡沫轴进行胸椎热身。"]
</MESSAGES>

<ENTITY>
{
  "name": "用户",
  "entity_types": ["Entity", "Person"],
  "summary": "",
  "attributes": {
    "fitness_level": "初学者或中级",
    "training_frequency": "需要系统化训练计划",
    "health_concerns": "胸椎需要热身准备"
  }
}
</ENTITY>
```

---

## 场景 4: 复杂实体 - 训练计划

#### Extract Attributes

**System**: You are a helpful assistant that extracts entity properties from the provided text.

**User**:
```
<MESSAGES>
["教练提出了"三分化"训练方案，并说："可以把你分成三分化，然后肩背放在一起练，然后胸跟手臂放在一起练，腿跟核心放在一起练。""]
</MESSAGES>

<ENTITY>
{
  "name": "三分化健身训练计划",
  "entity_types": ["Entity", "TrainingPlan"],
  "attributes": {
    "split_type": null,
    "muscle_groups": null,
    "frequency_per_week": null,
    "target_level": null
  }
}
</ENTITY>
```

假设定义：
```python
class TrainingPlan(BaseModel):
    """训练计划实体"""
    split_type: str | None = Field(None, description="分化类型：全身、上下、推拉腿、三分化等")
    muscle_groups: list[str] | None = Field(None, description="训练肌群列表")
    frequency_per_week: int | None = Field(None, description="每周训练频率")
    target_level: str | None = Field(None, description="目标水平：初学者、中级、高级")
```

#### Extract Summary


---

## 使用说明

1. **复制 System Message 和 User Message** 到你的 LLM 测试界面（如 ChatGPT、Claude）
2. 确保 LLM 配置为返回 JSON 格式
3. 检查返回的属性是否符合 Pydantic 模型定义
4. 验证摘要长度 < 250 字符
5. 确认没有幻觉内容（只基于提供的 MESSAGES）

## 关键验证点

- ✅ 属性值必须能从 MESSAGES 中找到证据
- ✅ 摘要必须简洁直接（< 250 字符）
- ✅ 不能添加猜测或推断的信息
- ✅ 返回的 JSON 必须符合 Pydantic schema
- ✅ 空值用 `null` 而非空字符串

