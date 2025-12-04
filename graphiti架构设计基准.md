看到你的真实数据和清晰的思路，我非常认真地为你设计了一套完整的方案。这个方案将Graphiti的优势（结构化存储、关系理解）与你的场景需求（用户习惯、隐性模式）深度结合。

## 一、整体架构设计

```
┌──────────────────────────────────────────────────────────┐
│          上游：Scene JSON生成                             │
│  (20250930-203000_60781c48.chunk.json)                   │
└──────────────────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────────┐
│      记忆处理进程（Memory Processor）                     │
│  • 解析scene JSON                                        │
│  • 提取关键字段（减少token）                              │
│  • 构建Graphiti Episode                                  │
└──────────────────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────────┐
│         Graphiti存储层                                    │
│  • add_episode()：精确时间管理的核心数据                  │
│  • Entity去重：人物、地点、活动统一                       │
│  • Edge时序关系：为模式发现预留结构                       │
└──────────────────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────────┐
│    外部分析层（Pattern Analyzer）                         │
│  • 时序模式挖掘                                          │
│  • 习惯识别                                              │
│  • 因果推断                                              │
└──────────────────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────────┐
│      查询工具层（Memory Tools for LLM）                   │
│  • 封装为tools_executor风格的工具                        │
│  • 支持LLM多跳推理                                       │
└──────────────────────────────────────────────────────────┘
```

---

## 二、Entity类型体系设计

基于你的真实数据，我设计了以下实体类型：

```python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entity类型定义
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ENTITY_TYPES = [
    # ━━━ 核心实体（直接来自scene JSON）━━━
    "Person",              # 人物：用户、教练、朋友等
    "Place",               # 地点：健身房、户外、家等
    "Activity",            # 活动：exercise, commute, social等
    "Topic",               # 话题/项目：三分化训练计划等
    "Device",              # 设备：泡沫轴、交通工具等
    
    # ━━━ 扩展实体（用于模式分析）━━━
    "TimeWindow",          # 时间窗口：早晨、晚上、工作日等
    "BodyPart",            # 身体部位：胸椎、肩背等（健身场景）
    "Environment",         # 环境：大风、室内、安静等
    "Routine",             # 习惯/例行事项（由分析层生成）
    "Pattern",             # 行为模式（由分析层生成）
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entity提取映射（从scene JSON到Graphiti）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_entities_from_scene(scene: dict, entity_canon: dict) -> list[dict]:
    """
    从scene中提取实体
    
    返回格式：
    [
        {
            "name": "用户",
            "type": "Person",
            "labels": ["Person", "User"],
            "metadata": {...原始entity_canon信息}
        },
        ...
    ]
    """
    entities = []
    
    # 1. 人物实体
    for person in entity_canon.get("people", []):
        entities.append({
            "name": person["canonical"],
            "type": "Person",
            "labels": ["Person"],
            "metadata": {
                "aliases": person.get("aliases", []),
                "confidence": person.get("confidence", 0.9),
                "voice_profile": person.get("voice_profile"),  # 保留声音特征
            }
        })
    
    # 2. 地点实体
    for place in entity_canon.get("places", []):
        entities.append({
            "name": place["canonical"],
            "type": "Place",
            "labels": ["Place"],
            "metadata": {
                "confidence": place.get("confidence", 0.9),
            }
        })
    
    # 3. 活动实体（核心！）
    activity_label = scene.get("activity", {}).get("label")
    if activity_label:
        entities.append({
            "name": activity_label,  # exercise, commute, social等
            "type": "Activity",
            "labels": ["Activity"],
            "metadata": {
                "probability": scene["activity"].get("p", 0.9),
                "alternatives": scene["activity"].get("alternatives", []),
            }
        })
    
    # 4. 话题/项目实体
    for topic in entity_canon.get("projects_or_topics", []):
        entities.append({
            "name": topic["canonical"],
            "type": "Topic",
            "labels": ["Topic"],
            "metadata": {
                "aliases": topic.get("aliases", []),
                "confidence": topic.get("confidence", 0.9),
            }
        })
    
    # 5. 设备实体
    for device in entity_canon.get("devices_or_tools", []):
        entities.append({
            "name": device["canonical"],
            "type": "Device",
            "labels": ["Device"],
            "metadata": {
                "confidence": device.get("confidence", 0.9),
            }
        })
    
    # 6. 时间窗口实体（从environment_index提取）
    time_of_day = scene.get("environment_index", {}).get("time_of_day")
    if time_of_day:
        entities.append({
            "name": time_of_day,  # morning, evening等
            "type": "TimeWindow",
            "labels": ["TimeWindow"],
            "metadata": {}
        })
    
    # 7. 环境实体（从weather、location_type提取）
    weather = scene.get("environment_index", {}).get("weather")
    if weather:
        entities.append({
            "name": f"天气_{weather}",
            "type": "Environment",
            "labels": ["Environment", "Weather"],
            "metadata": {"weather": weather}
        })
    
    return entities
```

---

## 三、Edge类型体系设计（核心！）

**关键设计原则**：Edge类型要能够表达**时序关系**和**因果关系**，为隐性模式发现预留结构。

```python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Edge类型定义
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EDGE_TYPES = [
    # ━━━ 基础关系（行为描述）━━━
    "PERFORMS",           # 用户-执行-活动
                         # 例：用户 PERFORMS 骑行
    
    "LOCATED_AT",        # 实体-位于-地点
                         # 例：用户 LOCATED_AT 健身房
    
    "USES",              # 实体-使用-设备
                         # 例：用户 USES 泡沫轴
    
    "INTERACTS_WITH",    # 人物-交互-人物
                         # 例：用户 INTERACTS_WITH 教练
    
    "DISCUSSES",         # 人物-讨论-话题
                         # 例：教练 DISCUSSES 三分化训练计划
    
    "TARGETS",           # 活动-针对-身体部位
                         # 例：胸椎放松活动 TARGETS 胸椎
    
    # ━━━ 时序关系（核心！用于模式发现）━━━
    "PRECEDES",          # A-发生在-B之前（松散时序）
                         # 例：骑行 PRECEDES 健身
                         # metadata: {"time_gap_minutes": 5}
    
    "IMMEDIATELY_FOLLOWED_BY",  # A-紧接着-B（紧密时序）
                               # 例：进入健身房 IMMEDIATELY_FOLLOWED_BY 与教练讨论
    
    "DURING",            # A-期间-B
                         # 例：用户 DURING 晚上 PERFORMS 健身
    
    # ━━━ 环境关系━━━
    "IN_ENVIRONMENT",    # 活动-在-环境中
                         # 例：骑行 IN_ENVIRONMENT 大风天
    
    "AT_TIME",           # 活动-在-时间窗口
                         # 例：健身 AT_TIME 晚上
    
    # ━━━ 模式关系（由分析层生成）━━━
    "PART_OF_ROUTINE",   # 活动-属于-习惯
                         # 例：健身 PART_OF_ROUTINE 晚间锻炼习惯
    
    "TRIGGERS",          # A-触发-B（因果关系）
                         # 例：见教练 TRIGGERS 制定训练计划
                         # 这个需要外部分析层推断
    
    "FREQUENTLY_PRECEDED_BY",  # A-经常发生在-B之后（统计模式）
                               # 例：健身 FREQUENTLY_PRECEDED_BY 骑行通勤
                               # metadata: {"frequency": 0.8, "sample_count": 20}
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Edge提取逻辑（从svo_bullets到Graphiti Edge）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_edges_from_scene(scene: dict, entities: list[dict]) -> list[str]:
    """
    从scene的svo_bullets中提取关系描述
    
    返回：适合传递给Graphiti的自然语言描述列表
    （Graphiti的LLM会从这些描述中提取结构化的Edge）
    """
    edge_facts = []
    
    # 1. 从svo_bullets提取行为关系
    for bullet in scene.get("svo_bullets", []):
        text = bullet["text"]
        bullet_type = bullet.get("type")
        confidence = bullet.get("confidence", 0.9)
        
        # 解析主谓宾结构：用户-骑行-在户外
        parts = text.split("-")
        if len(parts) >= 3:
            subject = parts[0].strip()
            verb = parts[1].strip()
            obj = "-".join(parts[2:]).strip()
            
            # 根据type生成不同的关系描述
            if bullet_type == "activity":
                edge_facts.append(
                    f"{subject}在{scene['start_time']}执行了活动：{verb}{obj}。"
                )
            elif bullet_type == "interaction":
                edge_facts.append(
                    f"{subject}与{obj}进行了互动：{verb}。时间：{scene['start_time']}"
                )
            elif bullet_type == "environment":
                edge_facts.append(
                    f"{subject}处于环境：{obj}。时间：{scene['start_time']}"
                )
    
    # 2. 提取时序关系（如果有merge_hint）
    merge_hint = scene.get("merge_hint", {})
    if merge_hint.get("can_merge_with_next"):
        edge_facts.append(
            f"场景{scene['id']}的活动在{scene['end_time']}尚未结束，"
            f"可能延续到下一个场景。"
        )
    
    # 3. 提取地点关系
    location = scene.get("location", {})
    place_id = location.get("place_id")
    if place_id:
        # 从entity_canon找到地点名称
        place_name = "未知地点"  # 需要从entity_canon反查
        activity = scene.get("activity", {}).get("label", "活动")
        edge_facts.append(
            f"用户在{scene['start_time']}位于{place_name}，"
            f"正在进行{activity}活动。"
        )
    
    # 4. 提取设备使用关系
    for bullet in scene.get("svo_bullets", []):
        if "使用" in bullet["text"] or "USES" in bullet.get("relation_hint", ""):
            edge_facts.append(
                f"{bullet['text']}。时间：{scene['start_time']}"
            )
    
    # 5. 提取key_quotes中的关键信息
    for quote in scene.get("key_quotes", []):
        speaker = quote.get("speaker", "")
        text = quote.get("text", "")
        # 从entity_canon映射speaker
        edge_facts.append(
            f"在{scene['start_time']}，说话者说：'{text}'"
        )
    
    return edge_facts
```

---

## 四、Episode构建策略

```python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Episode构建器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from dataclasses import dataclass
from datetime import datetime
from graphiti_core import RawEpisode, EpisodeType

@dataclass
class SceneEpisode:
    """Scene到Graphiti Episode的转换"""
    scene_id: str
    start_time: datetime
    end_time: datetime
    content: str           # 精简的内容（减少token）
    full_context: dict     # 完整的scene数据（存为metadata）
    
    def to_raw_episode(self, group_id: str) -> RawEpisode:
        """转换为Graphiti的RawEpisode"""
        return RawEpisode(
            name=f"scene_{self.scene_id}",
            content=self.content,
            source_description=f"用户记忆场景：{self.scene_id}",
            reference_time=self.start_time,
            source=EpisodeType.text,
            group_id=group_id,
            # metadata可以存储完整scene数据供后续分析
            # 但注意：Graphiti可能不直接支持metadata，需要通过其他方式存储
        )


def build_episode_content(scene: dict, entity_canon: dict) -> str:
    """
    构建Episode的content字段（精简版，减少token消耗）
    
    策略：
    1. 只包含核心信息：人物、地点、活动、关键行为
    2. 使用结构化格式，便于LLM提取
    3. 保留时间信息
    """
    lines = []
    
    # 1. 场景基本信息
    lines.append(f"场景ID: {scene['id']}")
    lines.append(f"时间: {scene['start_time']} 至 {scene['end_time']}")
    lines.append(f"时长: {scene['end_sec'] - scene['start_sec']:.0f}秒")
    
    # 2. 地点信息
    location = scene.get("location", {})
    if location.get("place_id"):
        # 从entity_canon查找地点名称
        place_name = _get_place_name(location["place_id"], entity_canon)
        lines.append(f"地点: {place_name}")
    
    # 3. 活动信息
    activity = scene.get("activity", {})
    if activity.get("label"):
        lines.append(f"活动类型: {activity['label']}")
    
    # 4. 参与者
    participants = scene.get("participants", [])
    if participants:
        participant_names = [
            _get_person_name(p_id, entity_canon) 
            for p_id in participants
        ]
        lines.append(f"参与者: {', '.join(participant_names)}")
    
    # 5. 核心行为（从svo_bullets提取）
    bullets = scene.get("svo_bullets", [])
    if bullets:
        lines.append("\n核心行为:")
        for bullet in bullets:
            lines.append(f"  - {bullet['text']}")
    
    # 6. 关键引用（如果有）
    key_quotes = scene.get("key_quotes", [])
    if key_quotes:
        lines.append("\n关键对话:")
        for quote in key_quotes[:2]:  # 最多2条，控制长度
            speaker = _map_speaker_to_person(quote.get("speaker"), scene, entity_canon)
            lines.append(f'  {speaker}: "{quote["text"]}"')
    
    # 7. 环境标签（精简）
    context_tags = scene.get("context_tags", [])[:5]  # 最多5个
    if context_tags:
        lines.append(f"\n环境: {', '.join(context_tags)}")
    
    # 8. 摘要（最重要！）
    summary = scene.get("summary", "")
    if summary:
        lines.append(f"\n摘要: {summary}")
    
    return "\n".join(lines)


def _get_place_name(place_id: str, entity_canon: dict) -> str:
    """从entity_canon查找地点名称"""
    for place in entity_canon.get("places", []):
        if place["id"] == place_id:
            return place["canonical"]
    return "未知地点"


def _get_person_name(person_id: str, entity_canon: dict) -> str:
    """从entity_canon查找人物名称"""
    for person in entity_canon.get("people", []):
        if person["id"] == person_id:
            return person["canonical"]
    return "未知人物"


def _map_speaker_to_person(speaker: str, scene: dict, entity_canon: dict) -> str:
    """将SPEAKER_XX映射到实际人物"""
    for spk_map in scene.get("speakers", []):
        if spk_map.get("spk") == speaker:
            person_id = spk_map.get("maps_to")
            return _get_person_name(person_id, entity_canon)
    return speaker
```

---

## 五、隐性模式发现的数据处理模式

虽然Graphiti不直接支持，但我们通过**外部分析层**实现：

```python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 隐性模式分析器（外部分析层）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from collections import defaultdict, Counter
from datetime import timedelta
from graphiti_core import Graphiti

class TemporalPatternAnalyzer:
    """
    时序模式分析器
    
    功能：
    1. 从Graphiti提取时序数据
    2. 挖掘重复出现的行为模式
    3. 识别隐性习惯
    4. 存回Graphiti作为高阶Edge和Entity
    """
    
    def __init__(self, graphiti: Graphiti):
        self.graphiti = graphiti
    
    async def detect_sequential_patterns(
        self,
        user_id: str,
        min_support: float = 0.6,  # 至少60%的情况下发生
        time_window: timedelta = timedelta(hours=24),
    ) -> list[dict]:
        """
        检测时序模式
        
        算法：
        1. 提取用户所有活动的时间线
        2. 使用滑动窗口查找频繁序列
        3. 计算支持度和置信度
        
        返回：
        [
            {
                "pattern_name": "晚间健身习惯",
                "sequence": ["commute", "exercise"],
                "support": 0.8,
                "avg_time_gap": timedelta(minutes=5),
                "instances": [...]
            },
            ...
        ]
        """
        # 步骤1: 从Graphiti获取所有活动边
        search_results = await self.graphiti.search(
            query=f"user {user_id} all activities",
            group_ids=[f"user_{user_id}"],
            # 使用时间排序
        )
        
        # 步骤2: 提取时序事件
        events = []
        for edge in search_results.edges:
            if edge.name in ["PERFORMS", "LOCATED_AT", "INTERACTS_WITH"]:
                events.append({
                    "activity": edge.fact,
                    "timestamp": edge.valid_at,
                    "source": edge.source_node_uuid,
                    "target": edge.target_node_uuid,
                    "edge_uuid": edge.uuid,
                })
        
        # 按时间排序
        events.sort(key=lambda e: e["timestamp"])
        
        # 步骤3: 滑动窗口模式挖掘
        patterns = defaultdict(list)
        
        for i, event_a in enumerate(events):
            for event_b in events[i+1:]:
                time_gap = event_b["timestamp"] - event_a["timestamp"]
                
                if time_gap > time_window:
                    break
                
                # 记录事件对
                pattern_key = (event_a["activity"], event_b["activity"])
                patterns[pattern_key].append({
                    "time_gap": time_gap,
                    "instance": (event_a, event_b)
                })
        
        # 步骤4: 计算支持度，过滤高频模式
        frequent_patterns = []
        total_events = len(events)
        
        for (activity_a, activity_b), instances in patterns.items():
            support = len(instances) / total_events
            
            if support >= min_support:
                avg_time_gap = sum(
                    [inst["time_gap"].total_seconds() for inst in instances]
                ) / len(instances)
                
                frequent_patterns.append({
                    "pattern_name": f"{activity_a} → {activity_b}",
                    "sequence": [activity_a, activity_b],
                    "support": support,
                    "count": len(instances),
                    "avg_time_gap_minutes": avg_time_gap / 60,
                    "instances": instances,
                })
        
        # 按支持度排序
        frequent_patterns.sort(key=lambda p: p["support"], reverse=True)
        
        return frequent_patterns
    
    async def identify_routines(
        self,
        user_id: str,
        min_occurrences: int = 3,
    ) -> list[dict]:
        """
        识别用户的例行习惯
        
        策略：
        1. 分析活动的时间规律（每天同一时段）
        2. 分析活动的序列规律（固定顺序）
        3. 生成Routine实体
        """
        # 实现类似逻辑...
        pass
    
    async def detect_triggers(
        self,
        user_id: str,
        llm_client: LLMClient,
    ) -> list[dict]:
        """
        检测触发关系（因果关系）
        
        策略：
        1. 提取时序共现的活动对
        2. 使用LLM判断是否存在因果关系
        3. 生成TRIGGERS边
        """
        patterns = await self.detect_sequential_patterns(user_id)
        
        triggers = []
        for pattern in patterns:
            if pattern["support"] > 0.7:  # 高频共现
                # 使用LLM判断因果关系
                prompt = f"""
                分析以下两个活动是否存在因果关系：
                
                活动A: {pattern["sequence"][0]}
                活动B: {pattern["sequence"][1]}
                
                统计信息：
                - 发生次数：{pattern["count"]}
                - 平均时间间隔：{pattern["avg_time_gap_minutes"]}分钟
                
                判断：活动A是否触发了活动B？
                回答：是/否，并简要说明理由。
                """
                
                response = await llm_client.generate(prompt)
                
                if "是" in response:
                    triggers.append({
                        "trigger": pattern["sequence"][0],
                        "consequence": pattern["sequence"][1],
                        "confidence": pattern["support"],
                        "reason": response,
                    })
        
        return triggers
    
    async def store_patterns_to_graphiti(
        self,
        user_id: str,
        patterns: list[dict],
    ):
        """
        将发现的模式存回Graphiti
        
        策略：
        1. 创建Routine实体（表示习惯）
        2. 创建PART_OF_ROUTINE边
        3. 创建TRIGGERS边（因果关系）
        """
        for pattern in patterns:
            # 创建Routine实体
            routine_episode = f"""
            发现用户的行为模式：{pattern["pattern_name"]}
            
            该模式包含以下活动序列：
            {" → ".join(pattern["sequence"])}
            
            统计信息：
            - 出现频率：{pattern["support"] * 100:.0f}%
            - 平均时间间隔：{pattern["avg_time_gap_minutes"]:.0f}分钟
            - 观察次数：{pattern["count"]}次
            
            这是一个稳定的行为习惯。
            """
            
            await self.graphiti.add_episode(
                routine_episode,
                group_ids=[f"user_{user_id}"],
                entity_types=ENTITY_TYPES,
                # Graphiti会自动提取"行为模式"实体和"PART_OF_ROUTINE"边
            )
```

---

## 六、查询工具封装（tools_executor风格）

```python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Graphiti查询工具（供LLM调用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from graphiti_core import Graphiti
from graphiti_core.search import SearchConfig, COMBINED_HYBRID_SEARCH_RRF
from datetime import datetime, timedelta

class GraphitiMemoryTools:
    """
    Graphiti记忆查询工具集
    封装为tools_executor风格，供LLM多跳推理使用
    """
    
    def __init__(self, graphiti: Graphiti):
        self.graphiti = graphiti
        self.pattern_analyzer = TemporalPatternAnalyzer(graphiti)
    
    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict,
        user_id: str | None = None,
    ) -> str:
        """
        统一的工具执行接口
        
        可用工具：
        1. semantic_memory_search: 语义检索记忆
        2. temporal_activity_query: 时序活动查询
        3. relationship_query: 关系查询
        4. habit_pattern_search: 习惯模式搜索
        5. trigger_analysis: 触发关系分析
        """
        try:
            if tool_name == "semantic_memory_search":
                return await self._semantic_search(arguments, user_id)
            
            elif tool_name == "temporal_activity_query":
                return await self._temporal_query(arguments, user_id)
            
            elif tool_name == "relationship_query":
                return await self._relationship_query(arguments, user_id)
            
            elif tool_name == "habit_pattern_search":
                return await self._habit_search(arguments, user_id)
            
            elif tool_name == "trigger_analysis":
                return await self._trigger_analysis(arguments, user_id)
            
            else:
                return f"未知工具：{tool_name}"
        
        except Exception as e:
            return f"工具执行失败 [{tool_name}]：{str(e)}"
    
    # ━━━ 工具实现 ━━━
    
    async def _semantic_search(self, arguments: dict, user_id: str) -> str:
        """
        工具1: 语义记忆搜索
        
        参数：
        - query: 查询文本
        - top_k: 返回数量
        - search_type: edges/nodes/episodes/all
        
        示例查询：
        - "用户最近的健身活动"
        - "用户和教练讨论了什么"
        - "用户使用过哪些设备"
        """
        query = arguments["query"]
        top_k = arguments.get("top_k", 10)
        search_type = arguments.get("search_type", "all")
        
        results = await self.graphiti.search(
            query=query,
            group_ids=[f"user_{user_id}"],
            num_results=top_k,
            config=COMBINED_HYBRID_SEARCH_RRF,
        )
        
        return self._format_search_results(results, search_type)
    
    async def _temporal_query(self, arguments: dict, user_id: str) -> str:
        """
        工具2: 时序活动查询
        
        参数：
        - start_time: 开始时间（ISO格式）
        - end_time: 结束时间
        - activity_type: 活动类型（可选）
        
        示例查询：
        - "用户在2025-09-30晚上做了什么"
        - "用户最近一周的健身记录"
        """
        start_time = datetime.fromisoformat(arguments["start_time"])
        end_time = datetime.fromisoformat(arguments["end_time"])
        activity_type = arguments.get("activity_type")
        
        # 构建时间范围查询
        query = f"user activities between {start_time} and {end_time}"
        if activity_type:
            query += f" activity_type:{activity_type}"
        
        results = await self.graphiti.search(
            query=query,
            group_ids=[f"user_{user_id}"],
            config=SearchConfig(
                # 配置时间过滤
                edge_config=EdgeSearchConfig(
                    search_methods=[EdgeSearchMethod.cosine_similarity],
                    # 可以在此添加时间过滤逻辑
                )
            ),
        )
        
        # 手动过滤时间范围
        filtered_edges = [
            edge for edge in results.edges
            if start_time <= edge.valid_at <= end_time
        ]
        
        return self._format_temporal_results(filtered_edges, start_time, end_time)
    
    async def _relationship_query(self, arguments: dict, user_id: str) -> str:
        """
        工具3: 关系查询
        
        参数：
        - entity_name: 实体名称
        - relation_type: 关系类型（可选）
        - max_hops: 最大跳数（默认1）
        
        示例查询：
        - "用户认识哪些人"
        - "用户去过哪些地点"
        - "用户的朋友的朋友"（多跳）
        """
        entity_name = arguments["entity_name"]
        relation_type = arguments.get("relation_type")
        max_hops = arguments.get("max_hops", 1)
        
        query = f"{entity_name} relationships"
        if relation_type:
            query += f" {relation_type}"
        
        results = await self.graphiti.search(
            query=query,
            group_ids=[f"user_{user_id}"],
            config=SearchConfig(
                edge_config=EdgeSearchConfig(
                    search_methods=[EdgeSearchMethod.bfs],
                    bfs_max_depth=max_hops,
                ),
            ),
        )
        
        return self._format_relationship_results(results, entity_name, relation_type)
    
    async def _habit_search(self, arguments: dict, user_id: str) -> str:
        """
        工具4: 习惯模式搜索
        
        参数：
        - pattern_type: 模式类型（routine/trigger/frequency）
        - time_range: 分析的时间范围（天数）
        
        示例查询：
        - "用户有什么运动习惯"
        - "用户的晚间例行活动"
        """
        pattern_type = arguments.get("pattern_type", "routine")
        time_range_days = arguments.get("time_range", 30)
        
        if pattern_type == "routine":
            # 调用外部分析器
            patterns = await self.pattern_analyzer.detect_sequential_patterns(
                user_id=user_id,
                min_support=0.5,
            )
            return self._format_pattern_results(patterns, "例行习惯")
        
        elif pattern_type == "trigger":
            triggers = await self.pattern_analyzer.detect_triggers(
                user_id=user_id,
                llm_client=self.graphiti.llm_client,
            )
            return self._format_trigger_results(triggers)
        
        else:
            return f"不支持的模式类型：{pattern_type}"
    
    async def _trigger_analysis(self, arguments: dict, user_id: str) -> str:
        """
        工具5: 触发关系分析
        
        参数：
        - activity_a: 活动A
        - activity_b: 活动B（可选）
        
        示例查询：
        - "见教练是否会触发其他活动"
        - "骑行通勤后用户通常做什么"
        """
        activity_a = arguments["activity_a"]
        activity_b = arguments.get("activity_b")
        
        # 查询activity_a相关的所有边
        query = f"{activity_a} followed by activities"
        results = await self.graphiti.search(
            query=query,
            group_ids=[f"user_{user_id}"],
        )
        
        # 分析时序关系
        # （这里需要结合外部分析器）
        
        return f"分析{activity_a}的触发关系..."
    
    # ━━━ 格式化函数 ━━━
    
    def _format_search_results(self, results, search_type: str) -> str:
        """格式化搜索结果"""
        lines = []
        
        if search_type in ["all", "nodes"]:
            lines.append("【相关实体】")
            for node in results.nodes[:5]:
                lines.append(f"  - {node.name} ({node.labels})")
                if node.summary:
                    lines.append(f"    {node.summary}")
        
        if search_type in ["all", "edges"]:
            lines.append("\n【相关关系】")
            for edge in results.edges[:10]:
                time_str = edge.valid_at.strftime("%Y-%m-%d %H:%M") if edge.valid_at else ""
                lines.append(f"  - {edge.fact} [{time_str}]")
        
        if search_type in ["all", "episodes"]:
            lines.append("\n【相关场景】")
            for episode in results.episodes[:3]:
                lines.append(f"  - {episode.name}: {episode.content[:100]}...")
        
        return "\n".join(lines) if lines else "未找到相关记忆。"
    
    def _format_temporal_results(self, edges: list, start: datetime, end: datetime) -> str:
        """格式化时序查询结果"""
        if not edges:
            return f"在{start}至{end}期间未找到活动记录。"
        
        lines = [f"时间范围：{start} 至 {end}"]
        lines.append(f"共找到 {len(edges)} 条活动记录：\n")
        
        # 按时间排序
        edges.sort(key=lambda e: e.valid_at)
        
        for idx, edge in enumerate(edges, 1):
            time_str = edge.valid_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"{idx}. [{time_str}] {edge.fact}")
        
        return "\n".join(lines)
    
    def _format_relationship_results(self, results, entity_name: str, relation_type: str) -> str:
        """格式化关系查询结果"""
        lines = [f"实体 '{entity_name}' 的关系：\n"]
        
        for edge in results.edges:
            if relation_type and edge.name != relation_type:
                continue
            
            lines.append(f"  - {edge.name}: {edge.fact}")
        
        return "\n".join(lines) if len(lines) > 1 else f"未找到 '{entity_name}' 的相关关系。"
    
    def _format_pattern_results(self, patterns: list, pattern_type: str) -> str:
        """格式化模式结果"""
        if not patterns:
            return f"未发现 {pattern_type} 模式。"
        
        lines = [f"发现 {len(patterns)} 个{pattern_type}模式：\n"]
        
        for idx, pattern in enumerate(patterns[:5], 1):
            lines.append(f"{idx}. {pattern['pattern_name']}")
            lines.append(f"   序列：{' → '.join(pattern['sequence'])}")
            lines.append(f"   出现频率：{pattern['support'] * 100:.0f}%")
            lines.append(f"   平均间隔：{pattern['avg_time_gap_minutes']:.0f}分钟\n")
        
        return "\n".join(lines)
    
    def _format_trigger_results(self, triggers: list) -> str:
        """格式化触发关系结果"""
        if not triggers:
            return "未发现明显的触发关系。"
        
        lines = ["发现以下触发关系：\n"]
        
        for idx, trigger in enumerate(triggers, 1):
            lines.append(f"{idx}. {trigger['trigger']} → {trigger['consequence']}")
            lines.append(f"   置信度：{trigger['confidence'] * 100:.0f}%")
            lines.append(f"   理由：{trigger['reason']}\n")
        
        return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tools Schema定义（供LLM识别）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GRAPHITI_TOOLS_SCHEMA = [
    {
        "name": "semantic_memory_search",
        "description": "语义搜索用户的记忆，支持自然语言查询。适用于查找相关的实体、关系或场景。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "查询文本，例如：'用户最近的健身活动'、'用户和教练讨论了什么'"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量，默认10",
                    "default": 10
                },
                "search_type": {
                    "type": "string",
                    "enum": ["all", "nodes", "edges", "episodes"],
                    "description": "搜索类型：all=全部，nodes=实体，edges=关系，episodes=场景",
                    "default": "all"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "temporal_activity_query",
        "description": "查询特定时间范围内的活动记录。适用于时间线查询、历史回顾。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "开始时间，ISO格式，例如：'2025-09-30T20:00:00+08:00'"
                },
                "end_time": {
                    "type": "string",
                    "description": "结束时间，ISO格式"
                },
                "activity_type": {
                    "type": "string",
                    "description": "活动类型过滤（可选），例如：'exercise'、'commute'"
                }
            },
            "required": ["start_time", "end_time"]
        }
    },
    {
        "name": "relationship_query",
        "description": "查询实体之间的关系，支持多跳查询。适用于关系发现、社交网络分析。",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_name": {
                    "type": "string",
                    "description": "实体名称，例如：'用户'、'教练'、'健身房'"
                },
                "relation_type": {
                    "type": "string",
                    "description": "关系类型（可选），例如：'INTERACTS_WITH'、'LOCATED_AT'"
                },
                "max_hops": {
                    "type": "integer",
                    "description": "最大跳数，默认1（直接关系）",
                    "default": 1
                }
            },
            "required": ["entity_name"]
        }
    },
    {
        "name": "habit_pattern_search",
        "description": "搜索用户的习惯模式，包括例行活动、重复行为等。适用于习惯分析、生活规律发现。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern_type": {
                    "type": "string",
                    "enum": ["routine", "trigger", "frequency"],
                    "description": "模式类型：routine=例行习惯，trigger=触发关系，frequency=频率分析",
                    "default": "routine"
                },
                "time_range": {
                    "type": "integer",
                    "description": "分析的时间范围（天数），默认30天",
                    "default": 30
                }
            }
        }
    },
    {
        "name": "trigger_analysis",
        "description": "分析活动之间的触发关系，发现因果模式。适用于行为预测、习惯理解。",
        "parameters": {
            "type": "object",
            "properties": {
                "activity_a": {
                    "type": "string",
                    "description": "活动A的名称，例如：'骑行通勤'"
                },
                "activity_b": {
                    "type": "string",
                    "description": "活动B的名称（可选），如果不指定则分析A触发的所有活动"
                }
            },
            "required": ["activity_a"]
        }
    },
]
```

---

## 七、完整的实施流程

```python
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 记忆处理进程（主流程）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import json
from pathlib import Path
from graphiti_core import Graphiti

class MemoryProcessor:
    """
    记忆处理进程
    负责从scene JSON到Graphiti的完整流程
    """
    
    def __init__(self, graphiti: Graphiti):
        self.graphiti = graphiti
        self.pattern_analyzer = TemporalPatternAnalyzer(graphiti)
    
    async def process_scene_file(self, scene_file_path: Path, user_id: str):
        """
        处理单个scene JSON文件
        
        流程：
        1. 解析JSON
        2. 提取关键字段
        3. 构建Episode
        4. 存储到Graphiti
        """
        # 步骤1: 读取JSON
        with open(scene_file_path, 'r', encoding='utf-8') as f:
            scene_data = json.load(f)
        
        entity_canon = scene_data.get("entity_canon", {})
        scenes = scene_data.get("scenes", [])
        
        # 步骤2: 逐个处理scene
        for scene in scenes:
            await self._process_single_scene(scene, entity_canon, user_id)
        
        print(f"✅ 处理完成：{len(scenes)}个场景已存储到Graphiti")
    
    async def _process_single_scene(
        self,
        scene: dict,
        entity_canon: dict,
        user_id: str,
    ):
        """处理单个scene"""
        # 步骤1: 构建Episode content
        content = build_episode_content(scene, entity_canon)
        
        # 步骤2: 创建RawEpisode
        scene_episode = SceneEpisode(
            scene_id=scene["id"],
            start_time=datetime.fromisoformat(scene["start_time"]),
            end_time=datetime.fromisoformat(scene["end_time"]),
            content=content,
            full_context=scene,
        )
        
        raw_episode = scene_episode.to_raw_episode(group_id=f"user_{user_id}")
        
        # 步骤3: 存储到Graphiti
        await self.graphiti.add_episode(
            raw_episode.content,
            source=raw_episode.source,
            source_description=raw_episode.source_description,
            reference_time=raw_episode.reference_time,
            group_ids=[raw_episode.group_id],
            entity_types=ENTITY_TYPES,
            # edge_types可以传递给LLM作为提示
        )
        
        print(f"  ✓ 存储场景: {scene['id']} ({scene['summary'][:50]}...)")
    
    async def analyze_patterns(self, user_id: str):
        """
        定期分析用户的行为模式
        建议：每天或每周运行一次
        """
        print(f"🔍 开始分析用户 {user_id} 的行为模式...")
        
        # 步骤1: 检测时序模式
        patterns = await self.pattern_analyzer.detect_sequential_patterns(
            user_id=user_id,
            min_support=0.5,
        )
        
        print(f"  发现 {len(patterns)} 个行为模式")
        
        # 步骤2: 识别触发关系
        triggers = await self.pattern_analyzer.detect_triggers(
            user_id=user_id,
            llm_client=self.graphiti.llm_client,
        )
        
        print(f"  发现 {len(triggers)} 个触发关系")
        
        # 步骤3: 存回Graphiti
        await self.pattern_analyzer.store_patterns_to_graphiti(
            user_id=user_id,
            patterns=patterns,
        )
        
        print(f"✅ 模式分析完成")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 使用示例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def main():
    # 初始化Graphiti
    graphiti = Graphiti(
        # ... 配置
    )
    
    # 创建记忆处理器
    processor = MemoryProcessor(graphiti)
    
    # 处理scene文件
    scene_file = Path("20250930-203000_60781c48.chunk.json")
    await processor.process_scene_file(scene_file, user_id="user_123")
    
    # （可选）立即分析模式
    await processor.analyze_patterns(user_id="user_123")
    
    # 创建查询工具
    memory_tools = GraphitiMemoryTools(graphiti)
    
    # 示例查询1: 语义搜索
    result1 = await memory_tools.execute_tool(
        tool_name="semantic_memory_search",
        arguments={"query": "用户的健身活动", "top_k": 5},
        user_id="user_123",
    )
    print(result1)
    
    # 示例查询2: 时序查询
    result2 = await memory_tools.execute_tool(
        tool_name="temporal_activity_query",
        arguments={
            "start_time": "2025-09-30T20:00:00+08:00",
            "end_time": "2025-09-30T21:00:00+08:00",
        },
        user_id="user_123",
    )
    print(result2)
    
    # 示例查询3: 习惯分析
    result3 = await memory_tools.execute_tool(
        tool_name="habit_pattern_search",
        arguments={"pattern_type": "routine"},
        user_id="user_123",
    )
    print(result3)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

---

## 八、关键设计说明

### 8.1 为什么这样设计Entity和Edge类型？

1. **Entity类型**：
   - 直接映射你的JSON结构（people, places, activities等）
   - 新增Activity实体：因为活动是分析的核心
   - 新增Routine/Pattern实体：为模式发现预留

2. **Edge类型**：
   - **基础关系**（PERFORMS, USES等）：表达直接行为
   - **时序关系**（PRECEDES, IMMEDIATELY_FOLLOWED_BY）：为模式发现提供时序信息
   - **模式关系**（TRIGGERS, PART_OF_ROUTINE）：存储分析结果

### 8.2 如何减少Token消耗？

1. **精简Episode content**：
   - 只保留核心信息（svo_bullets, summary, key_quotes）
   - 不传递完整的acoustic_details、uncertainties等

2. **使用`allowed_type_names`**：
   - 在调用`extract_edges`时传递`edge_types`作为提示
   - 限制LLM只生成预定义的关系类型

3. **批量处理**：
   - 如果一天有多个scene，考虑用`add_episode_bulk`

### 8.3 如何实现隐性模式发现？

**核心策略**：Graphiti存储+外部分析层

```
Graphiti（存储层）
    ↓ 存储时序数据
外部分析器（TemporalPatternAnalyzer）
    ↓ 挖掘模式
Graphiti（存储层）
    ↓ 存储模式实体和边
Tools（查询层）
    ↓ 暴露给LLM
```

### 8.4 Tools的设计理念

**模仿你的tools_executor.py**：
- 统一的`execute_tool()`接口
- 清晰的参数结构
- 格式化的返回结果
- 支持缓存（可选）

**增强功能**：
- 支持多跳查询（BFS）
- 支持模式检测
- 支持触发关系分析

---

## 九、实施建议

### 阶段1: 基础存储（第1周）

1. 实现`MemoryProcessor`
2. 测试scene JSON → Graphiti的转换
3. 验证Entity和Edge提取效果

### 阶段2: 查询工具（第2周）

1. 实现`GraphitiMemoryTools`
2. 测试5个工具的功能
3. 集成到你的推理系统

### 阶段3: 模式分析（第3-4周）

1. 实现`TemporalPatternAnalyzer`
2. 测试模式检测算法
3. 优化参数（min_support, time_window等）

### 阶段4: 优化迭代（第5周+）

1. 根据实际使用调整Entity/Edge类型
2. 优化Token消耗
3. 增强模式分析算法

---

## 十、预期效果

使用Graphiti后，LLM能够：

1. **回答复杂问题**：
   - "用户每次健身前都做什么准备？" → 发现"骑行通勤 → 健身"模式
   - "用户和教练的互动频率如何？" → 统计INTERACTS_WITH边

2. **发现隐性习惯**：
   - "用户晚上8点经常做什么？" → 发现晚间锻炼习惯
   - "哪些活动会触发用户去健身房？" → 发现触发关系

3. **多跳推理**：
   - "用户健身时讨论的话题涉及哪些设备？" → 三跳查询
   - "用户的朋友都去哪些地方？" → 社交网络分析

---

**这个方案综合了Graphiti的优势、你的数据特点、以及隐性模式发现的需求。它既实用又可扩展，是一个值得投入的长期架构。** 🎯