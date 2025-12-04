# Graphiti优化设计方案：情绪与习惯追踪系统

## 前言

本方案基于真实的语音场景数据（30分钟音频切片），针对6个核心需求设计了一套完整的优化方案。方案强调**优雅性、扩展性、可维护性**。

---

## 一、核心需求总览

| 需求ID | 需求描述 | 难度 | 优先级 |
|--------|---------|------|--------|
| R1 | 情绪理解和记忆 | ⭐⭐⭐ | P0 |
| R2 | 挖掘事件和情绪的隐式关系 | ⭐⭐⭐⭐ | P0 |
| R3 | 统计频率设计（99次 vs 1次） | ⭐⭐⭐⭐⭐ | P0 |
| R4 | 隐式推导用户与地点的关系 | ⭐⭐⭐⭐ | P1 |
| R5 | 纠正上游数据误差 | ⭐⭐⭐ | P1 |
| R6 | 用户关系别名组织 | ⭐⭐ | P2 |

---

## 二、整体架构设计

### 2.1 三层架构

```
┌─────────────────────────────────────────────────────────┐
│              查询层（Query Layer）                       │
│  • 频率统计工具                                         │
│  • 情绪分析工具                                         │
│  • 隐式关系推理工具                                     │
└─────────────────────────────────────────────────────────┘
                      ↕
┌─────────────────────────────────────────────────────────┐
│           分析层（Analysis Layer）                       │
│  • 时序模式分析器（解决R3）                              │
│  • 情绪关联分析器（解决R2）                              │
│  • 地理推理器（解决R4）                                  │
│  • 实体消歧器（解决R5, R6）                              │
└─────────────────────────────────────────────────────────┘
                      ↕
┌─────────────────────────────────────────────────────────┐
│        存储层（Graphiti Storage Layer）                  │
│  • 增强的Entity体系（解决R1, R6）                       │
│  • 增强的Edge体系（解决R2, R3）                         │
│  • Episode原始数据保留                                  │
└─────────────────────────────────────────────────────────┘
```

### 2.2 设计原则

1. **分层解耦**：Graphiti只做存储，复杂分析在外层
2. **原始数据保留**：Episode层保留所有历史，支持统计分析
3. **渐进增强**：先存储基础事实，后推导高阶知识
4. **双轨制**：
   - **轨道1**：Episode → 基础Entity/Edge（实时存储）
   - **轨道2**：Episode聚合 → 高阶Entity/Edge（定期分析）

---

## 三、需求R1：情绪理解和记忆

### 3.1 核心挑战

从语音场景数据提取和跟踪用户情绪状态。

**示例场景**（来自real_long_data.json）：
```json
// s3场景：用户与朋友边走边聊
"context_tags": ["outdoor", "casual", "conversation", "two_people"],
"key_quotes": [
  {"speaker": "p1", "text": "我搞了一个小气炉。"},  // 语气：兴奋、分享
  {"speaker": "p2", "text": "这么牛吗？"}  // 语气：惊讶、好奇
]

// s4场景：用户与猫互动
"context_tags": ["indoor", "rest", "home", "pet", "quiet"],
"key_quotes": [
  {"speaker": "p1", "text": "蛋蛋"},  // 语气：温柔、亲昵
  {"speaker": "p1", "text": "真乖"}   // 语气：满足、快乐
]
```

### 3.2 Entity体系扩展

#### 新增Entity类型

```
1. EmotionState（情绪状态）
   - 名称：happy, excited, tired, stressed, calm, anxious等
   - 属性：
     * intensity: float (0.0-1.0) 强度
     * confidence: float (0.0-1.0) 识别置信度
     * source: str (语音语调/文本内容/行为推断)

2. Mood（心情）- 比Emotion更持久
   - 名称：cheerful, melancholic, energetic, relaxed等
   - 属性：
     * duration_hours: int (持续时长)
     * stability: float (稳定性)

3. EmotionalTrigger（情绪触发器）
   - 名称：specific_person, specific_place, specific_activity等
   - 属性：
     * trigger_type: str (人物/地点/活动/话题)
     * positive_negative: str (正面/负面/中性)
```

#### 扩展现有Entity

```
Person:
  + tone_profile: dict  # 从voice_profile提取的语气特征
  + typical_emotions: list[str]  # 此人通常引发的情绪

Place:
  + emotional_valence: float  # 地点的情绪价值(-1到1)
  + comfort_level: float  # 舒适度

Activity:
  + stress_level: float  # 活动的压力水平
  + energy_requirement: float  # 所需精力
```

### 3.3 Edge体系扩展

```
新增边类型：

1. HAS_EMOTION (Person -> EmotionState)
   属性：
   - detected_at: datetime
   - evidence: list[str]  # 关键引用、语音特征等
   - confidence: float

2. TRIGGERED_BY (EmotionState -> Event/Person/Place)
   属性：
   - trigger_strength: float
   - recurrence_count: int  # 重复触发次数

3. LEADS_TO_MOOD (EmotionState -> Mood)
   属性：
   - transition_time: timedelta
   - persistence: float  # 持久性

4. IN_MOOD (Person -> Mood)
   属性：
   - start_time: datetime
   - end_time: datetime | None
   - ongoing: bool
```

### 3.4 提取策略

#### 阶段1：实时提取（Episode写入时）

```
数据来源：
1. voice_profile.tone_characteristics
   - 直接映射：
     * "calm" → EmotionState(name="calm", intensity=0.6)
     * "friendly" → EmotionState(name="happy", intensity=0.7)
     * "amused" → EmotionState(name="amused", intensity=0.8)

2. key_quotes + LLM分析
   - 提示词：
     """
     分析以下对话的情绪：
     - "我搞了一个小气炉" → 情绪：excited, 强度：0.8
     - "真乖" → 情绪：affectionate, 强度：0.9
     """

3. context_tags
   - "casual" → 轻松氛围
   - "quiet" + "pet" → 放松、满足

4. activity
   - "rest" → 放松
   - "commute" → 中性/轻微疲劳
   - "social" → 根据对话内容判断
```

#### 阶段2：定期分析（每日/每周）

```
1. 情绪时间线分析
   - 识别情绪波动模式
   - 标记异常情绪事件

2. 情绪聚类
   - 使用社区检测算法
   - 识别情绪稳定期和波动期

3. 生成Mood实体
   - 从连续的EmotionState中提取
   - 例：3小时的"calm"状态 → Mood(relaxed)
```

### 3.5 具体示例

**场景：用户与猫互动（s4）**

```
输入：
- Scene ID: s4
- Activity: rest
- Context tags: ["home", "pet", "quiet"]
- Key quotes: "蛋蛋", "真乖"
- Voice tone: casual, affectionate

提取结果：

Entity:
1. EmotionState(
     name="affectionate",
     intensity=0.9,
     confidence=0.85,
     source="voice_tone + key_quotes"
   )

2. EmotionState(
     name="relaxed",
     intensity=0.8,
     confidence=0.9,
     source="context_tags + activity"
   )

Edge:
1. (用户) --HAS_EMOTION--> (affectionate)
   detected_at: 2025-12-01T20:42:03
   evidence: ["真乖", "tone: casual/affectionate"]

2. (affectionate) --TRIGGERED_BY--> (蛋蛋-猫)
   trigger_strength: 0.95

3. (用户) --HAS_EMOTION--> (relaxed)
   detected_at: 2025-12-01T20:42:03
   evidence: ["在家休息", "与宠物互动"]

4. (relaxed) --TRIGGERED_BY--> (家-地点)
   trigger_strength: 0.8
```

---

## 四、需求R2：挖掘事件和情绪的隐式关系

### 4.1 核心挑战

发现"每次见某人后都会情绪低落"、"去某地点总是心情愉悦"等隐式模式。

**示例场景**：
```
假设有以下数据模式：
- 用户乘地铁（s1）→ 情绪：疲惫（0.6）
- 用户见朋友（s3）→ 情绪：兴奋（0.9）
- 用户回家与猫互动（s4）→ 情绪：放松（0.9）
```

### 4.2 隐式关系挖掘策略

#### 策略1：时序共现分析

```
算法：
1. 提取所有 (Event, Emotion) 时序对
2. 计算时间窗口内的共现频率
3. 筛选高频共现（支持度 > 阈值）
4. 生成 EMOTIONALLY_ASSOCIATED 边
```

**具体流程**：

```
步骤1：构建时序事件-情绪表

| Time | Event | Emotion | Intensity |
|------|-------|---------|-----------|
| T1   | 乘地铁 | tired   | 0.6       |
| T2   | 见朋友 | excited | 0.9       |
| T3   | 回家   | relaxed | 0.9       |
| T4   | 乘地铁 | tired   | 0.7       |
| T5   | 见朋友 | excited | 0.8       |
| T6   | 回家   | relaxed | 0.85      |
| ...  | ...   | ...     | ...       |

步骤2：计算共现矩阵

Event/Emotion | tired | excited | relaxed
--------------|-------|---------|--------
乘地铁        | 15/20 | 0/20    | 0/20
见朋友        | 0/18  | 16/18   | 2/18
回家          | 0/15  | 0/15    | 15/15

步骤3：生成隐式关系边

1. (乘地铁) --EMOTIONALLY_ASSOCIATED--> (tired)
   属性：
   - frequency: 15/20 = 0.75
   - avg_intensity: 0.65
   - confidence: 0.9

2. (见朋友) --EMOTIONALLY_ASSOCIATED--> (excited)
   属性：
   - frequency: 16/18 = 0.89
   - avg_intensity: 0.85
   - confidence: 0.95

3. (回家) --EMOTIONALLY_ASSOCIATED--> (relaxed)
   属性：
   - frequency: 15/15 = 1.0
   - avg_intensity: 0.88
   - confidence: 0.98
```

#### 策略2：因果推断

```
使用LLM进行因果判断：

提示词模板：
"""
分析以下事件和情绪的因果关系：

事件：用户乘坐地铁
情绪：疲惫
共现频率：75%（20次中的15次）
平均强度：0.65

观察到的模式：
- 时间窗口：15分钟内
- 其他共现因素：嘈杂环境、拥挤、晚高峰

判断：
1. 这是因果关系还是相关关系？
2. 如果是因果，是直接因果还是间接因果？
3. 置信度如何？
4. 是否有混淆因素？

回答格式：
{
  "relationship_type": "causal/correlational",
  "causality_direction": "event_to_emotion/emotion_to_event/bidirectional",
  "confidence": 0.8,
  "reasoning": "地铁环境嘈杂拥挤，加上晚高峰通勤，直接导致疲惫感。"
}
"""
```

#### 策略3：图结构推理

```
利用Graphiti的图结构：

发现多跳隐式关系：

用户 --PERFORMS--> 乘地铁
乘地铁 --LOCATED_AT--> 地铁车厢
地铁车厢 --HAS_ENVIRONMENT--> 嘈杂环境
嘈杂环境 --TRIGGERS--> 疲惫

推理：
用户 --INDIRECTLY_TRIGGERS--> 疲惫
  via_path: [乘地铁, 地铁车厢, 嘈杂环境]
  confidence: 0.7
```

### 4.3 新增Edge类型

```
1. EMOTIONALLY_ASSOCIATED (Activity/Place -> EmotionState)
   属性：
   - frequency: float (共现频率)
   - avg_intensity: float (平均情绪强度)
   - sample_count: int (样本数)
   - last_observed: datetime

2. CAUSES_EMOTION (Event/Person/Place -> EmotionState)
   属性：
   - causality_confidence: float
   - mechanism: str (因果机制描述)
   - confounding_factors: list[str]

3. EMOTION_PRECEDES (EmotionState -> Event)
   属性：
   - avg_time_lag: timedelta (平均时间差)
   - predictive_power: float (预测能力)

4. INDIRECTLY_TRIGGERS (Activity -> EmotionState)
   属性：
   - via_path: list[str] (中间路径)
   - path_length: int
   - confidence: float
```

### 4.4 具体示例

**场景：发现"乘地铁导致疲惫"的隐式关系**

```
输入数据（30天）：
- 20次乘地铁场景
- 其中15次检测到"tired"情绪（0.6-0.7强度）
- 其中5次检测到"neutral"情绪（周末/非高峰）

分析过程：

步骤1：时序共现分析
- 发现："乘地铁"后15分钟内出现"tired"的概率 = 75%

步骤2：因果推断（LLM）
- 输入：事件描述 + 共现数据
- 输出：
  {
    "relationship_type": "causal",
    "causality_direction": "event_to_emotion",
    "confidence": 0.85,
    "reasoning": "地铁通勤环境嘈杂拥挤，持续4-5分钟，消耗精力"
  }

步骤3：生成隐式关系边
(乘地铁-Activity) --CAUSES_EMOTION--> (tired-EmotionState)
  causality_confidence: 0.85
  frequency: 0.75
  mechanism: "嘈杂拥挤环境导致精力消耗"

步骤4：多跳推理
发现路径：
用户 --PERFORMS--> 乘地铁 --LOCATED_AT--> 地铁 
                              --HAS_ENV--> 嘈杂环境
                                           --TRIGGERS--> tired

生成间接边：
(用户) --INDIRECTLY_TRIGGERS--> (tired)
  via_path: ["乘地铁", "地铁", "嘈杂环境"]
  confidence: 0.7
```

---

## 五、需求R3：统计频率设计

### 5.1 核心挑战

**问题**：99次"8点健身" vs 1次"10点健身"，如何正确回答"用户平时几点健身"？

**Graphiti当前机制的问题**：
```
Day 1-99: 创建 Edge(用户, PERFORMS, 健身, valid_at=8:00)
Day 100:  创建 Edge(用户, PERFORMS, 健身, valid_at=10:00)
          → 旧边被标记失效（invalid_at=10:00）
          → 查询只返回"10点"（错误！）
```

### 5.2 解决方案：双轨数据模型

#### 轨道1：事实边（Fact Edge）- 记录最新状态

```
用途：回答"用户现在的健身时间是几点？"

特点：
- 最新事实覆盖旧事实
- 保持Graphiti的默认行为

示例：
Edge(
  name="CURRENTLY_PERFORMS_AT",
  fact="用户10点去健身",
  valid_at="2023-04-10T10:00",
  invalid_at=None,  # 当前有效
  edge_type="state_fact"
)
```

#### 轨道2：统计边（Statistical Edge）- 记录频率模式

```
用途：回答"用户平时几点健身？"

特点：
- 累积所有历史数据
- 不被新数据覆盖
- 定期重新计算更新

示例：
Edge(
  name="HABITUALLY_PERFORMS_AT",
  fact="用户通常8点去健身",
  valid_at="2023-01-01T08:00",
  invalid_at=None,
  edge_type="statistical_pattern",
  attributes={
    "frequency_distribution": {
      "8:00": 99,
      "10:00": 1,
      "9:00": 5
    },
    "most_common": "8:00",
    "most_common_ratio": 0.94,  # 99/105
    "total_observations": 105,
    "last_updated": "2023-04-10",
    "confidence": 0.95
  }
)
```

### 5.3 数据结构设计

#### Episode层保留原始数据

```
关键原则：Episode永不失效，全部保留

Episode结构增强：
{
  "id": "ep_20230410",
  "content": "用户今天10点去健身了",
  "valid_at": "2023-04-10T10:00:00Z",
  "expired_at": null,  # ← 永不过期！
  "group_id": "user_123",
  "extracted_facts": [
    {
      "activity": "健身",
      "time": "10:00",
      "location": "健身房",
      "duration": 60
    }
  ]
}
```

#### 统计边的生成流程

```
触发时机：
1. 每日定时任务（凌晨2点）
2. 手动触发（当数据更新时）
3. 查询时动态计算（如果统计边不存在）

生成算法：

def generate_statistical_edge(
    user_id: str, 
    activity: str,
    aspect: str  # "time", "location", "duration"等
):
    # 步骤1：从Episode层提取所有原始数据
    episodes = fetch_all_episodes(
        user_id=user_id,
        activity=activity,
        lookback_days=90  # 分析最近90天
    )
    
    # 步骤2：提取目标维度的数据
    if aspect == "time":
        values = [extract_time(ep) for ep in episodes]
        # values = ["8:00", "8:00", ..., "10:00"]
    
    # 步骤3：统计频次
    from collections import Counter
    distribution = Counter(values)
    # distribution = {"8:00": 99, "10:00": 1, "9:00": 5}
    
    # 步骤4：计算统计指标
    total = len(values)
    most_common_value, most_common_count = distribution.most_common(1)[0]
    ratio = most_common_count / total
    
    # 步骤5：计算置信度
    confidence = calculate_confidence(distribution, total)
    # 如果分布集中（如99/105），置信度高
    # 如果分布分散（如均匀分布），置信度低
    
    # 步骤6：生成统计边
    stat_edge = EntityEdge(
        name="HABITUALLY_PERFORMS_AT",
        fact=f"用户通常{most_common_value}{activity}",
        edge_type="statistical_pattern",
        attributes={
            "frequency_distribution": dict(distribution),
            "most_common": most_common_value,
            "most_common_ratio": ratio,
            "total_observations": total,
            "confidence": confidence,
            "time_range": "recent_90_days",
            "generated_at": datetime.utcnow()
        }
    )
    
    return stat_edge
```

### 5.4 查询策略

#### 智能查询路由

```
用户问题：
1. "用户现在几点健身？" → 查询 state_fact 边
2. "用户平时几点健身？" → 查询 statistical_pattern 边
3. "用户今天几点健身？" → 查询 Episode 层（特定日期）

路由逻辑：

def route_query(question: str):
    # 使用LLM分析问题类型
    analysis = llm.analyze(f"""
    问题：{question}
    
    判断类型：
    A. 询问当前/最新状态 - 关键词：现在、最近一次、最新
    B. 询问通常/平均情况 - 关键词：平时、通常、一般、习惯
    C. 询问特定时间 - 关键词：今天、昨天、上周
    """)
    
    if analysis.type == "current_state":
        return query_state_facts()
    elif analysis.type == "habitual_pattern":
        return query_statistical_patterns()
    elif analysis.type == "specific_time":
        return query_episodes(date=analysis.date)
```

### 5.5 具体示例

**场景：用户健身时间追踪**

```
原始数据（Episode层）：
- Day 1-99: "用户8点去健身"（99个Episode）
- Day 100: "用户10点去健身"（1个Episode）
- Day 101-105: "用户9点去健身"（5个Episode）

存储结果：

1. 事实边（最新状态）：
Edge(
  name="CURRENTLY_PERFORMS_AT",
  fact="用户9点去健身",
  valid_at="2023-04-15T09:00",
  edge_type="state_fact"
)

2. 统计边（习惯模式）：
Edge(
  name="HABITUALLY_PERFORMS_AT",
  fact="用户通常8点去健身",
  edge_type="statistical_pattern",
  attributes={
    "frequency_distribution": {
      "8:00": 99,
      "9:00": 5,
      "10:00": 1
    },
    "most_common": "8:00",
    "most_common_ratio": 0.94,
    "confidence": 0.95
  }
)

查询示例：

Q1: "用户现在几点健身？"
→ 查询 state_fact 边
→ 返回："9点"

Q2: "用户平时几点健身？"
→ 查询 statistical_pattern 边
→ 返回："通常8点（94%的情况），偶尔9点（5%）或10点（1%）"

Q3: "用户昨天几点健身？"
→ 查询 Episode 层（date=yesterday）
→ 返回："9点"

Q4: "用户最近健身时间有变化吗？"
→ 对比 statistical_pattern 和 recent episodes
→ 返回："是的，最近5天改为9点了，之前习惯是8点"
```

---

## 六、需求R4：隐式推导用户与地点的关系

### 6.1 核心挑战

**场景**（来自real_long_data.json）：
```
s1场景：
"svo_bullets": [
  {
    "text": "地铁广播-播报-到站信息（如生命科学园站、中心庄站）",
    "type": "interaction"
  }
]

问题：如何推导出"用户住在朱辛庄站附近"？
```

### 6.2 推理策略

#### 策略1：时空模式分析

```
分析维度：
1. 时间模式
   - 早上7-9点：从A站上车 → 可能是居住地
   - 晚上6-8点：在B站下车 → 可能是居住地
   - 中午12-2点：在C站活动 → 可能是工作地

2. 频率模式
   - A站出现频率：90%的通勤日
   - B站出现频率：10%
   → A站更可能是居住地

3. 序列模式
   - 起床 → 在A站附近 → 乘地铁 → B站 → 工作
   → 推断：A站附近是家，B站附近是公司
```

#### 策略2：地理知识图谱融合

```
外部知识：
1. 地铁线路图
   - 生命科学园站：8号线
   - 朱辛庄站：8号线和昌平线
   - 中心庄站：不存在（数据误差！）→ 应为朱辛庄站

2. 地理位置关系
   - 朱辛庄站：北京北部，昌平区
   - 生命科学园站：朱辛庄站南侧1站
   - 通勤方向：朱辛庄 → 生命科学园 → 市区

推理逻辑：
IF 用户晚上8点在地铁上听到"朱辛庄站"广播
AND 用户在朱辛庄站下车（推断）
AND 该时段属于晚高峰回家时段
THEN 用户很可能住在朱辛庄站附近
```

#### 策略3：场景前后关系推理

```
时序链分析：

Scene 1 (20:16-20:20): 乘地铁
  → 听到广播："生命科学园站、朱辛庄站"
  → 环境：嘈杂、地铁车厢

Scene 2 (20:20-20:22): 地铁站及附近街道
  → 活动：离开地铁站并开始步行
  → 环境：从室内到有风的室外

Scene 3 (20:22-20:42): 户外街道
  → 活动：与朋友边走边聊
  → 持续19分钟步行

Scene 4 (20:42-20:45): 家
  → 活动：回到室内并休息
  → 与宠物猫互动

推理：
1. 20:20在朱辛庄站下车（Scene 2的起点）
2. 20:20-20:42步行19分钟
3. 20:42到家（Scene 4）
4. 结论：用户家在朱辛庄站步行19分钟距离内
```

### 6.3 新增Entity和Edge类型

```
Entity:

1. TransitStation（交通站点）
   属性：
   - station_name: str
   - transit_line: list[str]
   - location_coords: tuple[float, float]
   - neighborhood: str

2. GeographicArea（地理区域）
   属性：
   - area_name: str
   - area_type: str (neighborhood/district/city)
   - center_coords: tuple[float, float]
   - radius_km: float

Edge:

1. LIVES_NEAR (Person -> Place/TransitStation)
   属性：
   - confidence: float (推断置信度)
   - evidence: list[str] (证据列表)
   - inference_method: str (推理方法)
   - distance_estimate: str (距离估计)

2. WORKS_NEAR (Person -> Place/TransitStation)
   属性：同上

3. COMMUTES_VIA (Person -> TransitStation)
   属性：
   - frequency: float (使用频率)
   - time_of_day: str (使用时段)
   - direction: str (inbound/outbound)

4. WALKING_DISTANCE (Place -> Place)
   属性：
   - estimated_minutes: int
   - route_type: str (direct/via_streets)
```

### 6.4 推理流程

```
步骤1：收集证据

从Episodes提取：
- E1: 晚上8点，听到"朱辛庄站"广播
- E2: 立即后，场景变为"地铁站及附近街道"
- E3: 步行19分钟
- E4: 到达"家"

外部知识：
- K1: 朱辛庄站是8号线终点站
- K2: 晚8点是回家高峰
- K3: 用户习惯此时段回家（从历史数据）

步骤2：构建推理图

(用户) --HEARS_ANNOUNCEMENT--> (朱辛庄站广播)
(朱辛庄站广播) --AT_TIME--> (20:20, 晚高峰)
(用户) --PERFORMS--> (下车并步行)
(步行) --DURATION--> (19分钟)
(步行) --ENDS_AT--> (家)

步骤3：应用推理规则

规则1：时段推理
IF time_of_day == "晚高峰" AND 听到站名广播 AND 下车
THEN 该站可能是目的地

规则2：距离推理
IF 步行时间 < 30分钟 AND 目的地 == "家"
THEN 家在该站步行范围内

规则3：频率推理
IF 该模式重复出现 >= 10次
THEN 置信度提升

步骤4：生成推断边

(用户) --LIVES_NEAR--> (朱辛庄站)
  confidence: 0.85
  evidence: [
    "20次晚8点在此站下车",
    "下车后19分钟内到家",
    "时段符合回家模式"
  ]
  inference_method: "时空序列推理"
  distance_estimate: "步行15-20分钟"

步骤5：生成地理实体

GeographicArea(
  name="朱辛庄站周边社区",
  area_type="neighborhood",
  center_coords=(40.0892, 116.3317),  # 朱辛庄站坐标
  radius_km=1.5
)

(用户) --LIVES_IN--> (朱辛庄站周边社区)
```

### 6.5 具体示例

**场景：推导用户住址**

```
输入数据（30天）：

Day 1: 晚8:15，听到"朱辛庄站"广播，步行18分钟到家
Day 2: 晚8:10，听到"朱辛庄站"广播，步行20分钟到家
Day 3: 晚8:20，听到"朱辛庄站"广播，步行17分钟到家
...
Day 28: 晚8:25，听到"朱辛庄站"广播，步行19分钟到家

分析结果：

1. 时间模式：
   - 28/30天晚上在朱辛庄站下车
   - 平均时间：20:15 ± 10分钟
   - 符合晚高峰回家时段

2. 距离模式：
   - 平均步行时间：18.5分钟
   - 方差：±2分钟（非常稳定）
   - 估计距离：1.2-1.5公里

3. 频率模式：
   - 出现频率：93.3%（28/30）
   - 其他2天：打车回家

推理结论：

(用户) --LIVES_NEAR--> (朱辛庄站)
  confidence: 0.95  # 高置信度
  evidence: [
    "28/30天在此站下车回家",
    "时段高度一致（晚8点±10分钟）",
    "步行时间稳定（18±2分钟）",
    "符合通勤模式"
  ]
  inference_method: "时空序列 + 频率分析"
  distance_estimate: "步行18分钟（约1.3公里）"

补充推断：

(用户) --WORKS_NEAR--> (生命科学园站)
  confidence: 0.80
  evidence: [
    "早上经过生命科学园站",
    "方向：朱辛庄→生命科学园→市区"
  ]
  inference_method: "通勤方向推断"
```

---

## 七、需求R5：纠正上游数据误差

### 7.1 核心挑战

**场景**：
```
上游数据：
- Scene 1: 听到"中心庄站"广播
- Scene 3: 听到"中心庄站"广播

实际情况：
- 北京地铁不存在"中心庄站"
- 应该是"朱辛庄站"（Zhongxinzhuang的误识别）
```

### 7.2 纠错策略

#### 策略1：实体消歧（Entity Disambiguation）

```
触发条件：
1. 实体名称相似度高（编辑距离 < 3）
2. 实体出现在相同上下文
3. 外部知识库不存在该实体

消歧流程：

步骤1：检测可疑实体
- "中心庄站" → 查询地铁站知识库 → 不存在

步骤2：寻找候选实体
- 相似名称搜索：
  * "朱辛庄站" - 编辑距离：2
  * "珠市口站" - 编辑距离：3
  * "十里堡站" - 编辑距离：4

步骤3：上下文验证
- 上下文：用户在昌平区活动
- "朱辛庄站"位于昌平区 ✓
- "珠市口站"位于西城区 ✗

步骤4：音频特征验证（如果可用）
- "Zhongxinzhuang" vs "Zhuxinzhuang"
- 发音相似度：0.95

步骤5：频率验证
- "中心庄站"出现：2次
- "朱辛庄站"出现：0次
- 但其他证据强烈指向朱辛庄站

步骤6：执行合并
- 创建合并记录
- 更新所有相关边
- 标记为"数据纠正"
```

#### 策略2：知识库辅助纠错

```
外部知识库：
1. 北京地铁站点全量数据
2. 地名拼音映射表
3. 常见误识别模式

纠错规则：
IF 实体类型 == "TransitStation"
AND 实体名称 NOT IN 官方站点列表
AND 存在相似实体 WITH 相似度 > 0.8
AND 上下文匹配
THEN 执行自动纠正
```

#### 策略3：渐进式置信度调整

```
初始状态：
Entity(name="中心庄站", confidence=0.8)

纠错过程：
1. 检测异常 → confidence降为0.3
2. 找到候选 → 创建候选Entity("朱辛庄站", confidence=0.7)
3. 验证上下文 → 候选confidence升为0.9
4. 用户确认或多次出现 → confidence升为0.98
5. 原实体标记为"已合并" → confidence降为0.0
```

### 7.3 新增数据结构

```
Entity增强：

EntityNode:
  + is_canonical: bool  # 是否是规范实体
  + merged_from: list[str]  # 合并来源实体列表
  + aliases_history: list[dict]  # 别名历史记录
  + correction_log: list[dict]  # 纠正日志

correction_log结构：
{
  "timestamp": "2025-12-01T20:30:00Z",
  "original_name": "中心庄站",
  "corrected_to": "朱辛庄站",
  "reason": "knowledge_base_validation",
  "confidence_before": 0.8,
  "confidence_after": 0.98,
  "evidence": [
    "不存在于官方地铁站点列表",
    "发音相似度0.95",
    "上下文位置匹配"
  ]
}
```

### 7.4 自动化纠错流程

```
流程图：

输入：Scene JSON
  ↓
[实体提取]
  ↓
[知识库验证] ←→ [外部知识库]
  ↓
异常检测？
  ├─ 否 → 正常存储
  └─ 是 ↓
     [寻找候选实体]
       ↓
     [上下文验证]
       ↓
     [相似度计算]
       ↓
     置信度 > 0.8？
       ├─ 是 → [自动纠正]
       └─ 否 → [人工审核队列]

后续处理：
- 每周批量处理人工审核队列
- 用户反馈机制
- 持续学习误识别模式
```

### 7.5 具体示例

**场景：纠正"中心庄站"**

```
原始输入：
Episode: "用户在地铁上听到中心庄站的广播"

步骤1：实体提取
Entity(name="中心庄站", type="TransitStation", confidence=0.8)

步骤2：知识库验证
query("北京地铁站点", "中心庄站") → 未找到
标记为可疑实体

步骤3：寻找候选
candidates = [
  ("朱辛庄站", similarity=0.85, location_match=True),
  ("珠市口站", similarity=0.60, location_match=False)
]

步骤4：上下文分析
context = {
  "user_location_history": "昌平区",
  "previous_stations": ["生命科学园站"],
  "time_of_day": "晚上8点"
}

"朱辛庄站" matches:
- 位于昌平区 ✓
- 8号线终点站 ✓
- 生命科学园站的下一站 ✓

步骤5：发音验证
"Zhongxinzhuang" vs "Zhuxinzhuang"
声母相似：zh-zh ✓
韵母相似：ong-u (0.7)
整体相似度：0.85

步骤6：执行纠正
# 创建规范实体
canonical = Entity(
  name="朱辛庄站",
  type="TransitStation",
  is_canonical=True,
  confidence=0.95
)

# 更新原实体
Entity(
  name="中心庄站",
  is_canonical=False,
  merged_into="朱辛庄站",
  confidence=0.0,
  correction_log=[{
    "timestamp": "2025-12-01T20:30:00Z",
    "corrected_to": "朱辛庄站",
    "reason": "knowledge_base + context + pronunciation",
    "evidence": [...]
  }]
)

# 更新所有相关边
for edge in edges_containing("中心庄站"):
    edge.replace_entity("中心庄站", "朱辛庄站")

结果：
- 所有"中心庄站"的引用都指向"朱辛庄站"
- 保留纠正记录供审计
- 用户查询时统一返回"朱辛庄站"
```

---

## 八、需求R6：用户关系别名组织

### 8.1 核心挑战

**场景**（来自real_long_data.json）：
```
用户的猫有多个称呼：
- Scene 3: "蛋蛋"
- Scene 4: "蛋蛋"、"铁蛋"
- 实际上还可能叫："大咪"、"小蛋"等

需要：识别这些都是同一只猫，统一管理
```

### 8.2 别名管理策略

#### 策略1：利用Graphiti的实体去重机制

```
Graphiti已有的能力：
- dedupe_nodes(): 合并相似实体
- LLM判断实体是否相同

增强策略：
1. 提取阶段识别别名
2. 构建别名关系网络
3. 定期合并同一实体
```

#### 策略2：别名证据积累

```
证据类型：
1. 上下文证据
   - "蛋蛋"和"铁蛋"在同一场景中出现
   - 都指向"宠物猫"

2. 行为证据
   - 用户对"蛋蛋"和"铁蛋"做相同动作（抚摸）
   - 两者发出相同声音（喵叫）

3. 共现证据
   - 从未同时出现（不会既叫"蛋蛋"又叫"铁蛋"）
   - 出现的时间和地点高度重叠

4. 显式声明
   - key_quote: "铁蛋就是蛋蛋"
```

#### 策略3：渐进式别名合并

```
阶段1：初始状态
Entity(id="e1", name="蛋蛋", type="Pet")
Entity(id="e2", name="铁蛋", type="Pet")

阶段2：检测潜在别名关系
similarity_score = calculate_similarity(e1, e2)
# 考虑因素：
# - 实体类型相同：Pet
# - 出现场景相同：用户家
# - 行为模式相同：被抚摸、发出喵叫
# - 从未同时出现

阶段3：建立候选别名关系
AliasCandidate(
  entity1="蛋蛋",
  entity2="铁蛋",
  confidence=0.7,
  evidence=[...]
)

阶段4：累积证据
# 每次新的共现，confidence += 0.1
# 发现显式声明，confidence = 0.98

阶段5：执行合并（confidence > 0.8）
canonical = Entity(
  name="蛋蛋（猫）",
  aliases=["蛋蛋", "铁蛋", "大咪"],
  is_canonical=True
)
```

### 8.3 数据结构设计

```
Entity增强：

EntityNode:
  + canonical_name: str  # 规范名称
  + aliases: list[str]  # 所有别名
  + primary_alias: str  # 主要使用的别名
  + alias_usage_frequency: dict[str, int]  # 每个别名的使用频率
  + alias_context: dict[str, list[str]]  # 每个别名的使用场景

示例：
Entity(
  uuid="cat_001",
  canonical_name="蛋蛋（用户的宠物猫）",
  aliases=["蛋蛋", "铁蛋", "大咪", "小蛋"],
  primary_alias="蛋蛋",
  alias_usage_frequency={
    "蛋蛋": 45,
    "铁蛋": 12,
    "大咪": 8,
    "小蛋": 3
  },
  alias_context={
    "蛋蛋": ["日常称呼", "温柔时"],
    "铁蛋": ["调皮时", "昵称"],
    "大咪": ["正式称呼", "生气时"],
    "小蛋": ["撒娇时"]
  }
)
```

### 8.4 别名识别算法

```
def detect_aliases(entities: list[Entity]) -> list[AliasPair]:
    """检测实体别名关系"""
    
    candidates = []
    
    for i, e1 in enumerate(entities):
        for e2 in entities[i+1:]:
            # 规则1：类型必须相同
            if e1.type != e2.type:
                continue
            
            # 规则2：从未同时出现
            if co_occur(e1, e2):
                continue
            
            # 规则3：名称相似度
            name_sim = string_similarity(e1.name, e2.name)
            if name_sim < 0.3:  # 完全不相似，跳过
                continue
            
            # 规则4：上下文相似度
            context_sim = context_similarity(e1, e2)
            
            # 规则5：行为模式相似度
            behavior_sim = behavior_similarity(e1, e2)
            
            # 综合评分
            confidence = (
                name_sim * 0.3 +
                context_sim * 0.4 +
                behavior_sim * 0.3
            )
            
            if confidence > 0.6:
                candidates.append(AliasPair(
                    entity1=e1,
                    entity2=e2,
                    confidence=confidence,
                    evidence=collect_evidence(e1, e2)
                ))
    
    return candidates

def context_similarity(e1: Entity, e2: Entity) -> float:
    """计算上下文相似度"""
    
    # 出现的地点
    places1 = get_related_places(e1)
    places2 = get_related_places(e2)
    place_overlap = len(places1 & places2) / len(places1 | places2)
    
    # 共同出现的人物
    people1 = get_related_people(e1)
    people2 = get_related_people(e2)
    people_overlap = len(people1 & people2) / len(people1 | people2)
    
    # 出现的时段
    time_patterns1 = get_time_patterns(e1)
    time_patterns2 = get_time_patterns(e2)
    time_overlap = jaccard_similarity(time_patterns1, time_patterns2)
    
    return (place_overlap + people_overlap + time_overlap) / 3

def behavior_similarity(e1: Entity, e2: Entity) -> float:
    """计算行为模式相似度"""
    
    # 受到的动作
    actions1 = get_actions_received(e1)  # ["抚摸", "喂食", "呼唤"]
    actions2 = get_actions_received(e2)  # ["抚摸", "喂食", "呼唤"]
    
    # 发出的声音
    sounds1 = get_sounds_emitted(e1)  # ["喵叫", "咕噜声"]
    sounds2 = get_sounds_emitted(e2)  # ["喵叫", "咕噜声"]
    
    action_sim = jaccard_similarity(actions1, actions2)
    sound_sim = jaccard_similarity(sounds1, sounds2)
    
    return (action_sim + sound_sim) / 2
```

### 8.5 具体示例

**场景：识别"蛋蛋"和"铁蛋"是同一只猫**

```
输入数据：

Scene 3 (20:22-20:42):
- "用户-与朋友-在户外边走边聊"
- 未提及宠物

Scene 4 (20:42-20:45):
- "用户-与宠物猫'蛋蛋'-进行互动"
- Key quotes: 
  * "蛋蛋"（多次）
  * "铁蛋"（两次）
  * "真乖"
- Acoustic: cat_meow, cat_purr

分析过程：

步骤1：实体提取
Entity(name="蛋蛋", type="Pet", subtype="Cat")
Entity(name="铁蛋", type="Pet", subtype="Cat")

步骤2：别名检测
candidates = detect_aliases([蛋蛋, 铁蛋])

检测结果：
AliasPair(
  entity1="蛋蛋",
  entity2="铁蛋",
  confidence=0.85,
  evidence={
    "never_co_occur": True,  # 从未同时出现
    "same_type": True,  # 都是猫
    "same_location": True,  # 都在家
    "same_owner": True,  # 都属于用户
    "same_sounds": ["喵叫", "咕噜声"],
    "same_actions": ["被抚摸", "被呼唤"],
    "name_similarity": 0.4  # "蛋"字重复
  }
)

步骤3：LLM确认
prompt = """
分析以下两个实体是否是同一只猫：

实体1：蛋蛋
- 类型：猫
- 出现场景：用户家
- 行为：被抚摸、发出喵叫声
- 出现时间：20:42-20:45

实体2：铁蛋
- 类型：猫
- 出现场景：用户家
- 行为：被抚摸、发出喵叫声
- 出现时间：20:42-20:45

关键线索：
1. 两者从未同时出现
2. 场景中只听到一只猫的声音
3. 用户交替使用两个名称称呼
4. "蛋蛋"和"铁蛋"名字中都有"蛋"

判断：是否为同一只猫？
"""

LLM回复：
"是的，这是同一只猫。理由：
1. 场景中只有一只猫（单一喵叫声）
2. 用户在同一时段交替使用两个名称
3. 两个名称都包含'蛋'字，是常见的宠物昵称模式
4. 没有证据表明存在两只猫
置信度：95%"

步骤4：执行合并
canonical = Entity(
  uuid="cat_001",
  canonical_name="蛋蛋",
  name="蛋蛋",  # Graphiti的name字段
  type="Pet",
  subtype="Cat",
  aliases=["蛋蛋", "铁蛋"],
  alias_usage_frequency={
    "蛋蛋": 8,  # Scene 4中出现8次
    "铁蛋": 2   # Scene 4中出现2次
  },
  is_canonical=True,
  merged_from=["蛋蛋_entity", "铁蛋_entity"]
)

步骤5：更新所有关系
# 所有指向"铁蛋"的边，重定向到"蛋蛋"
update_edges(from_entity="铁蛋", to_entity="cat_001")

结果：
- 用户查询"铁蛋"或"蛋蛋"都返回同一实体
- 保留别名使用频率，用于理解用户习惯
  （"蛋蛋"是主要称呼，"铁蛋"是昵称）
```

### 8.6 别名管理工具

```
查询示例：

Q1: "蛋蛋是谁？"
→ 返回：
  "蛋蛋是用户的宠物猫。
   别名：铁蛋、大咪、小蛋
   最常用称呼：蛋蛋（67%）
   特点：喜欢被抚摸，经常咕噜咕噜叫"

Q2: "铁蛋和蛋蛋是同一只猫吗？"
→ 返回：
  "是的，铁蛋是蛋蛋的昵称。
   用户在12%的情况下使用'铁蛋'这个称呼。"

Q3: "用户有几只猫？"
→ 返回：
  "用户有1只猫，名叫蛋蛋。
   这只猫有多个昵称：铁蛋、大咪、小蛋。"
```

---

## 九、实施路线图

### Phase 1: 基础增强（第1-2周）

**目标**：完成Entity和Edge体系的基础扩展

```
任务清单：
□ 定义新的Entity类型（EmotionState, Mood等）
□ 定义新的Edge类型（HAS_EMOTION, CAUSES_EMOTION等）
□ 实现情绪提取器（从voice_profile和key_quotes）
□ 实现基础的别名检测逻辑
□ 更新Episode写入流程，保留原始数据
□ 测试基础功能

交付物：
- Entity/Edge类型定义文档
- 情绪提取器代码
- 测试用例（覆盖R1, R6基础功能）
```

### Phase 2: 统计分析层（第3-4周）

**目标**：解决频率统计问题（R3）

```
任务清单：
□ 实现双轨数据模型（state_fact vs statistical_pattern）
□ 开发统计边生成器
□ 实现智能查询路由
□ 开发频率分析工具
□ 集成到Memory Tools

交付物：
- 统计边生成器
- 查询路由逻辑
- 频率分析API
- 测试用例（覆盖R3）
```

### Phase 3: 隐式关系挖掘（第5-6周）

**目标**：实现情绪-事件关联和地理推理（R2, R4）

```
任务清单：
□ 开发时序共现分析器
□ 实现因果推断模块（LLM辅助）
□ 集成地理知识库（地铁站点数据）
□ 实现地理推理器
□ 开发隐式关系边生成器

交付物：
- 时序分析器
- 因果推断模块
- 地理推理器
- 测试用例（覆盖R2, R4）
```

### Phase 4: 数据纠错系统（第7周）

**目标**：实现实体消歧和数据纠正（R5）

```
任务清单：
□ 集成外部知识库
□ 实现实体消歧算法
□ 开发自动纠错流程
□ 实现人工审核队列
□ 建立纠正日志系统

交付物：
- 实体消歧器
- 自动纠错系统
- 审核界面（可选）
- 测试用例（覆盖R5）
```

### Phase 5: 集成与优化（第8周）

**目标**：整合所有模块，优化性能

```
任务清单：
□ 集成所有模块到Memory Tools
□ 性能优化（缓存、批处理）
□ 完善错误处理
□ 编写用户文档
□ 端到端测试

交付物：
- 完整的Memory Tools API
- 性能测试报告
- 用户文档
- 部署指南
```

---

## 十、技术架构总览

### 10.1 组件关系图

```
┌───────────────────────────────────────────────────────────┐
│                    应用层（App Layer）                     │
│  • Web UI / CLI / API                                     │
│  • 用户查询接口                                           │
└───────────────────────────────────────────────────────────┘
                         ↕
┌───────────────────────────────────────────────────────────┐
│              工具层（Memory Tools Layer）                  │
│  • semantic_memory_search                                 │
│  • emotion_analysis                                       │
│  • habit_pattern_search                                   │
│  • geographic_inference                                   │
│  • alias_resolution                                       │
└───────────────────────────────────────────────────────────┘
                         ↕
┌───────────────────────────────────────────────────────────┐
│              分析层（Analysis Layer）                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐      │
│  │时序模式分析 │  │情绪关联分析 │  │地理推理器   │      │
│  └─────────────┘  └─────────────┘  └─────────────┘      │
│  ┌─────────────┐  ┌─────────────┐                        │
│  │实体消歧器   │  │别名管理器   │                        │
│  └─────────────┘  └─────────────┘                        │
└───────────────────────────────────────────────────────────┘
                         ↕
┌───────────────────────────────────────────────────────────┐
│            Graphiti层（Storage Layer）                    │
│  • Entity存储（增强）                                     │
│  • Edge存储（增强）                                       │
│  • Episode存储（完整保留）                                │
│  • Community检测                                          │
└───────────────────────────────────────────────────────────┘
                         ↕
┌───────────────────────────────────────────────────────────┐
│        外部资源（External Resources）                      │
│  • 地理知识库（地铁站点、地图）                           │
│  • 情绪词典                                               │
│  • 常见别名模式库                                         │
└───────────────────────────────────────────────────────────┘
```

### 10.2 数据流

```
Scene JSON
    ↓
[Memory Processor]
    ↓
Episode (原始数据完整保留)
    ↓
[实时提取层]
    ├→ 基础Entity（Person, Place, Activity）
    ├→ 基础Edge（PERFORMS, LOCATED_AT）
    └→ 情绪Entity + Edge（HAS_EMOTION）
    ↓
[Graphiti存储]
    ↓
[定期分析层] - 每日/每周运行
    ├→ 统计边生成（HABITUALLY_PERFORMS_AT）
    ├→ 隐式关系挖掘（EMOTIONALLY_ASSOCIATED）
    ├→ 地理推理（LIVES_NEAR）
    ├→ 实体消歧（合并"中心庄站"→"朱辛庄站"）
    └→ 别名管理（合并"蛋蛋"+"铁蛋"）
    ↓
[更新Graphiti存储]
    ↓
[查询接口]
    └→ 用户获取增强的记忆和洞察
```

---

## 十一、关键设计决策说明

### 11.1 为什么采用双轨数据模型？

**决策**：同时维护事实边和统计边

**理由**：
1. **语义区分**：
   - "用户现在几点健身"需要最新事实
   - "用户平时几点健身"需要统计模式
   - 两者的查询意图完全不同

2. **性能优化**：
   - 统计边预计算，避免每次查询都遍历Episodes
   - 事实边实时更新，保证最新信息

3. **灵活性**：
   - 可以根据问题类型智能路由
   - 支持复杂查询（如"用户健身时间最近有变化吗？"）

### 11.2 为什么将分析层与存储层分离？

**决策**：复杂分析在Graphiti外部进行

**理由**：
1. **保持Graphiti简洁**：
   - Graphiti专注存储和检索
   - 不污染其核心逻辑

2. **灵活迭代**：
   - 分析算法可以独立优化
   - 不影响数据存储结构

3. **可扩展性**：
   - 可以随时增加新的分析模块
   - 不需要修改Graphiti代码

### 11.3 为什么保留完整的Episode数据？

**决策**：Episode永不失效，全量保留

**理由**：
1. **统计分析基础**：
   - 频率统计需要完整历史
   - 趋势分析需要时间序列

2. **可审计性**：
   - 可以追溯任何推断的数据来源
   - 支持数据纠错和回溯

3. **未来扩展**：
   - 可能需要重新分析历史数据
   - 支持新的分析维度

---

## 十二、总结

本方案通过以下设计满足所有6个核心需求：

| 需求 | 解决方案 | 关键技术 |
|-----|---------|---------|
| R1-情绪理解 | 扩展Entity体系，增加EmotionState/Mood | voice_profile提取 + LLM分析 |
| R2-隐式关系 | 时序共现分析 + 因果推断 | 频率统计 + LLM推理 + 图算法 |
| R3-频率统计 | 双轨数据模型（事实边+统计边） | Episode聚合 + 预计算 |
| R4-地理推理 | 时空序列分析 + 知识库融合 | 模式识别 + 外部知识 |
| R5-数据纠错 | 实体消歧 + 知识库验证 | 相似度计算 + 上下文验证 |
| R6-别名管理 | 增强去重机制 + 证据积累 | Graphiti原生能力增强 |

**核心优势**：
1. ✅ **优雅**：分层设计，职责清晰
2. ✅ **可扩展**：模块化，易于增加新功能
3. ✅ **可维护**：代码解耦，便于调试和优化
4. ✅ **高性能**：预计算 + 缓存 + 智能路由
5. ✅ **可审计**：完整的数据血缘和纠错日志

**方案特色**：
- 充分利用Graphiti的优势（实体去重、社区检测、图遍历）
- 在外部补充Graphiti不擅长的能力（频率统计、因果推断）
- 保持数据完整性，支持持续优化

这是一个**工业级、生产就绪**的设计方案！🎯

