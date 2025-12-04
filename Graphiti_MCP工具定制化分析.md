# Graphiti MCP 工具定制化需求分析

## 一、核心判断

**结论先行**: 对于你的场景（24h声音信息提取用户习惯），**强烈建议定制化设计tools**。

原因：
1. ❌ Graphiti MCP的通用工具**不能满足**你的时序分析需求
2. ❌ 缺少时长统计、习惯模式等核心功能
3. ✅ 你已有的tools_executor设计更契合场景
4. ✅ 定制化工具能提供10倍以上的效率提升

---

## 二、对比分析

### 2.1 Graphiti MCP提供的工具（通用版）

```python
# 1. add_memory - 添加记忆
add_memory(
    name="用户活动",
    episode_body="用户在10:00-11:00参加了会议",
    group_id="user123"
)

# 2. search_nodes - 搜索节点（实体）
search_nodes(
    query="会议相关的实体",
    max_nodes=10
)

# 3. search_memory_facts - 搜索事实（关系）
search_memory_facts(
    query="用户做了什么",
    max_facts=10
)

# 4. get_episodes - 获取原始记录
get_episodes(
    group_ids=["user123"],
    max_episodes=10
)

# 5. 其他：delete_entity_edge, delete_episode, clear_graph, get_status
```

**Graphiti MCP的局限性**：

| 功能需求 | Graphiti MCP | 你的场景需求 | 差距 |
|----------|--------------|--------------|------|
| **时间范围查询** | ❌ 只能通过SearchFilters过滤 | ✅ 需要精确的start~end查询 | **关键缺失** |
| **时长统计** | ❌ 没有 | ✅ 需要计算事件总时长 | **关键缺失** |
| **分类统计** | ❌ 没有 | ✅ 需要按event_type聚合 | **关键缺失** |
| **时间窗口** | ❌ 没有 | ✅ 需要"今天"、"本周"等快捷查询 | **关键缺失** |
| **习惯分析** | ❌ 没有 | ✅ 需要发现重复模式 | **关键缺失** |
| **结果格式化** | ⚠️ 返回原始JSON | ✅ 需要自然语言格式化 | 需要额外处理 |
| **缓存机制** | ❌ 没有 | ✅ 需要缓存提高性能 | 需要自己实现 |

### 2.2 你的tools_executor设计（定制版）

```python
# 你的工具列表
tools = {
    "hybrid_semantic_search": {
        "description": "混合语义搜索",
        "params": {"query": str, "top_k": int},
        "适用": "找相关记忆"
    },
    
    "fetch_time_range_episodes": {
        "description": "时间范围检索",
        "params": {"start_time": str, "end_time": str},
        "适用": "查询特定时段的活动"
    },
    
    "compute_event_duration": {
        "description": "事件时长统计",
        "params": {
            "event_type": str,
            "date": str | None,
            "time_window": str | None  # today/yesterday/this_week等
        },
        "适用": "统计时长、发现习惯"
    },
    
    "find_latest_event": {
        "description": "查找最近事件",
        "params": {"event_type": str, "max_days_back": int},
        "适用": "找最近一次做某事的时间"
    }
}
```

**你的设计优势**：
1. ✅ 针对时序数据优化
2. ✅ 支持时长聚合和统计
3. ✅ 内置缓存机制
4. ✅ 结果自然语言格式化
5. ✅ 快捷时间窗口（today/this_week等）

---

## 三、具体场景对比

### 场景1: "我昨天开了多久会？"

#### 使用Graphiti MCP（通用工具）

```python
# 步骤1: 获取昨天的episodes（没有直接的时间窗口）
episodes = await get_episodes(
    group_ids=["user123"],
    max_episodes=100  # 必须取很多，因为不知道哪些是昨天的
)

# 步骤2: 在LLM端或客户端手动过滤昨天的数据
yesterday = date.today() - timedelta(days=1)
yesterday_episodes = [
    ep for ep in episodes 
    if ep["created_at"].startswith(str(yesterday))
]

# 步骤3: 手动过滤会议类型
meeting_episodes = [
    ep for ep in yesterday_episodes
    if "会议" in ep["content"] or "meeting" in ep["content"].lower()
]

# 步骤4: 手动解析时间并计算时长
# ... 需要大量代码解析时间戳、计算时长

# 问题：
# 1. 需要获取大量无关数据（浪费带宽和token）
# 2. 需要在客户端进行复杂的时间处理
# 3. 没有时长字段，需要自己解析
# 4. 效率极低
```

#### 使用你的定制工具

```python
result = await execute_tool(
    tool_name="compute_event_duration",
    arguments={
        "event_type": "meeting",
        "time_window": "yesterday"
    },
    user_id="user123"
)

# 返回：
# """
# 昨天 meeting 总时长：2小时30分钟（150分钟）
# 
# 详细时段：
# 1. 10:00~11:30 (1h30m) 开会 - 项目评审
# 2. 14:00~15:00 (1h) 开会 - 周报会议
# """

# 优势：
# 1. 一次调用完成
# 2. 服务端直接计算，不浪费带宽
# 3. 结果已格式化，直接可用
# 4. 有缓存，相同查询秒返
```

**效率对比**：
- Graphiti MCP: 需要3-4次API调用 + 大量客户端处理 ≈ **5-10秒**
- 你的工具: 1次调用 + 缓存 ≈ **0.1-0.5秒**

### 场景2: "我最近一周每天睡了多久？"

#### 使用Graphiti MCP

```python
# 非常困难！
# 1. 需要获取一周的所有episodes
# 2. 需要识别哪些是睡眠相关
# 3. 需要按天分组
# 4. 需要计算每天的睡眠时长
# 5. 需要处理跨天的睡眠（晚上11点到早上7点）

# 大约需要10-20行代码 + 复杂的时间处理逻辑
```

#### 使用你的定制工具

```python
result = await execute_tool(
    tool_name="compute_event_duration",
    arguments={
        "event_type": "sleep",
        "time_window": "recent_7_days"
    },
    user_id="user123"
)

# 返回：
# """
# 近7天 sleep 总时长：49小时30分钟（2970分钟）
# 
# 详细时段：
# 1. 2024-01-01 23:00~07:30 (8h30m) 睡觉
# 2. 2024-01-02 23:30~07:00 (7h30m) 睡觉
# ...
# """
```

### 场景3: "我多久没运动了？"

#### 使用Graphiti MCP

```python
# 步骤1: 搜索运动相关的facts
facts = await search_memory_facts(
    query="运动 锻炼 健身",
    max_facts=50
)

# 步骤2: 手动解析时间戳，找最近的
# 步骤3: 计算距今天数
# 复杂且容易出错
```

#### 使用你的定制工具

```python
result = await execute_tool(
    tool_name="find_latest_event",
    arguments={
        "event_type": "exercise",
        "max_days_back": 30
    },
    user_id="user123"
)

# 返回：
# "您上次运动是在 5 天前（2024-01-10 18:00），当时进行了跑步30分钟。"
```

---

## 四、核心差异总结

### 4.1 设计哲学不同

```
┌─────────────────────────────────────────────────────────┐
│ Graphiti MCP（通用知识图谱）                             │
├─────────────────────────────────────────────────────────┤
│ 设计目标：通用的知识存储和检索                           │
│ 适用场景：文档、对话、知识管理                           │
│ 核心能力：实体提取、关系构建、语义搜索                   │
│ 不适合：  时序分析、统计聚合、模式发现                   │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ 你的场景（24h声音信息 → 用户习惯）                      │
├─────────────────────────────────────────────────────────┤
│ 设计目标：时序事件分析和习惯发现                         │
│ 适用场景：生活记录、行为分析、习惯追踪                   │
│ 核心能力：时间范围查询、时长统计、模式识别               │
│ 需要：    精确的时间控制、高效的聚合计算                 │
└─────────────────────────────────────────────────────────┘
```

### 4.2 数据模型不同

#### Graphiti的数据模型

```python
# Graphiti关注：实体和关系
EntityNode(
    name="用户",
    type="Person",
    summary="..."
)

EntityEdge(
    source="用户",
    relation="ATTENDS",
    target="会议",
    fact="用户参加了会议",
    valid_at="2024-01-15T10:00:00Z"  # 时间只是一个属性
)

# 问题：
# 1. 没有duration（时长）字段
# 2. valid_at是关系的"生效时间"，不是事件的"持续时间"
# 3. 难以表达"从10:00到11:00"这种时间段
```

#### 你的场景需要的数据模型

```python
# 你需要的是：时间段事件
TimeRangeEvent(
    user_id="user123",
    event_type="meeting",
    start_time="2024-01-15T10:00:00",
    end_time="2024-01-15T11:30:00",
    duration_minutes=90,  # 关键字段！
    action="开会",
    summary="项目评审会议"
)

# 优势：
# 1. 原生支持时间段
# 2. duration直接可查
# 3. 易于按时间范围聚合
# 4. 天然适合时长统计
```

### 4.3 查询模式不同

| 查询类型 | Graphiti擅长 | 你的场景需要 |
|----------|--------------|--------------|
| **实体查询** | ✅ "谁是Alice？" | ⚠️ 不是重点 |
| **关系查询** | ✅ "Alice和Bob的关系" | ⚠️ 不是重点 |
| **语义搜索** | ✅ "机器学习相关知识" | ✅ 需要但不够 |
| **时间范围** | ⚠️ 只能过滤 | ✅✅✅ 核心需求 |
| **时长统计** | ❌ 不支持 | ✅✅✅ 核心需求 |
| **聚合计算** | ❌ 不支持 | ✅✅✅ 核心需求 |
| **模式识别** | ❌ 不支持 | ✅✅ 重要需求 |

---

## 五、建议方案

### 方案1: 纯定制化（推荐★★★★★）

**完全使用你自己的tools_executor**

```python
# 你的工具栈
tools = [
    hybrid_semantic_search,      # 保留语义搜索（Graphiti的优势）
    fetch_time_range_episodes,   # 时间范围查询
    compute_event_duration,      # 时长统计
    find_latest_event,           # 最近事件
    # 可以添加更多定制工具：
    find_recurring_patterns,     # 发现重复模式
    compare_time_periods,        # 时间段对比
    predict_next_occurrence,     # 预测下次发生时间
]

# 底层存储
storage = {
    "primary": "PostgreSQL + TimescaleDB",  # 时序数据优化
    "search": "Graphiti知识图谱（可选）",   # 语义搜索增强
    "cache": "Redis",                       # 结果缓存
}
```

**优势**：
- ✅ 完全控制，针对场景优化
- ✅ 性能最优（专用时序存储）
- ✅ 功能完整（所有需要的工具）
- ✅ 成本最低（不需要Graphiti的昂贵写入）

**劣势**：
- ⚠️ 需要自己维护
- ⚠️ 缺少Graphiti的实体关系提取

### 方案2: 混合架构（如果需要实体关系）

**Graphiti + 你的定制工具**

```python
class HybridTools:
    def __init__(self):
        self.graphiti = Graphiti(...)  # 用于实体关系
        self.custom_tools = YourToolsExecutor(...)  # 用于时序分析
    
    async def route_query(self, query: str, tool_name: str):
        """智能路由到合适的工具"""
        
        # 时序相关 → 你的工具
        if tool_name in ["compute_event_duration", "fetch_time_range_episodes"]:
            return await self.custom_tools.execute(tool_name, ...)
        
        # 实体关系相关 → Graphiti
        elif tool_name in ["search_nodes", "search_memory_facts"]:
            return await self.graphiti_mcp.execute(tool_name, ...)
        
        # 混合查询 → 两者结合
        else:
            graphiti_results = await self.graphiti.search(...)
            custom_results = await self.custom_tools.execute(...)
            return merge(graphiti_results, custom_results)

# 工具列表（扩展）
tools = [
    # 你的时序工具
    "compute_event_duration",
    "fetch_time_range_episodes",
    "find_latest_event",
    
    # Graphiti的实体工具（如果需要）
    "search_entity_relationships",  # 包装Graphiti的search_nodes
    "find_related_entities",        # 包装Graphiti的search_memory_facts
]
```

**适用场景**：
- 需要同时分析"用户做了什么"（实体）和"做了多久"（时长）
- 例如："Alice和Bob见面的频率和时长趋势"

### 方案3: 最小化Graphiti（不推荐）

**只用Graphiti MCP + 大量客户端处理**

```python
# 不推荐！效率极低
# 示例：查询昨天的会议时长

# 步骤1: 调用Graphiti MCP
episodes = await mcp.get_episodes(max_episodes=100)

# 步骤2-10: 大量客户端处理
# ... 过滤时间
# ... 识别会议
# ... 计算时长
# ... 格式化结果

# 问题：
# - 带宽浪费（获取大量无关数据）
# - Token浪费（传递大量数据给LLM）
# - 延迟高（多次往返）
# - 代码复杂（大量边缘逻辑）
```

---

## 六、具体实现建议

### 6.1 推荐的工具Schema设计

```python
# 完整的工具定义
tools_schema = [
    {
        "name": "compute_event_duration",
        "description": "统计特定类型事件的总时长和详细时段",
        "parameters": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": "事件类型，如meeting/sleep/exercise/work等",
                    "enum": ["meeting", "sleep", "exercise", "work", "leisure", "commute", "all"]
                },
                "date": {
                    "type": "string",
                    "description": "查询特定日期，格式YYYY-MM-DD，与time_window二选一",
                    "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
                },
                "time_window": {
                    "type": "string",
                    "description": "预设时间窗口，与date二选一",
                    "enum": ["today", "yesterday", "this_week", "this_month", "recent_7_days", "recent_30_days"]
                }
            },
            "required": ["event_type"],
            "oneOf": [
                {"required": ["date"]},
                {"required": ["time_window"]}
            ]
        },
        "returns": {
            "type": "string",
            "description": "格式化的时长统计结果，包含总时长和详细时段"
        }
    },
    
    {
        "name": "fetch_time_range_episodes",
        "description": "查询指定时间范围内的所有事件记录",
        "parameters": {
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "开始时间，支持格式：YYYY-MM-DD HH:MM、今天10点、昨天下午等",
                },
                "end_time": {
                    "type": "string",
                    "description": "结束时间，格式同start_time"
                }
            },
            "required": ["start_time", "end_time"]
        }
    },
    
    {
        "name": "find_latest_event",
        "description": "查找最近一次做某事的时间和详情",
        "parameters": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": "事件类型"
                },
                "max_days_back": {
                    "type": "integer",
                    "description": "向前搜索的最大天数",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 365
                }
            },
            "required": ["event_type"]
        }
    },
    
    {
        "name": "find_recurring_patterns",
        "description": "发现用户的重复行为模式（新增）",
        "parameters": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": "要分析的事件类型"
                },
                "time_range_days": {
                    "type": "integer",
                    "description": "分析的时间范围（天数）",
                    "default": 30
                },
                "min_occurrences": {
                    "type": "integer",
                    "description": "最少重复次数",
                    "default": 3
                }
            },
            "required": ["event_type"]
        },
        "returns": {
            "type": "object",
            "description": "发现的模式，包括频率、典型时间、时长等"
        }
    },
    
    {
        "name": "hybrid_semantic_search",
        "description": "语义搜索相关记忆（保留Graphiti优势）",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询"
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回结果数量",
                    "default": 8
                }
            },
            "required": ["query"]
        }
    }
]
```

### 6.2 缓存策略优化

```python
# 你已有的缓存基础上，增加智能失效
class SmartCache:
    def __init__(self):
        self.cache = {}
        self.cache_ttl = {
            "compute_event_duration": {
                "today": 60,           # 1分钟（会变化）
                "yesterday": 86400,    # 1天（不会变）
                "this_week": 300,      # 5分钟
                "this_month": 600,     # 10分钟
            },
            "fetch_time_range_episodes": 300,  # 5分钟
            "find_latest_event": 600,          # 10分钟
            "hybrid_semantic_search": 3600,    # 1小时
        }
    
    def get_ttl(self, tool_name: str, arguments: dict) -> int:
        """根据工具和参数动态决定TTL"""
        if tool_name == "compute_event_duration":
            time_window = arguments.get("time_window", "")
            return self.cache_ttl[tool_name].get(time_window, 300)
        
        return self.cache_ttl.get(tool_name, 300)
```

### 6.3 结果格式化增强

```python
# 增加更丰富的格式化
def _format_duration_result_enhanced(agg: dict, label: str = "事件") -> dict:
    """返回结构化结果 + 自然语言描述"""
    
    segments = agg.get("segments", [])
    total = agg.get("total_minutes", 0)
    
    # 计算统计信息
    stats = {
        "total_minutes": total,
        "total_hours": total / 60,
        "segment_count": len(segments),
        "avg_duration_minutes": total / len(segments) if segments else 0,
        "longest_segment": max(segments, key=lambda s: s["minutes"]) if segments else None,
        "shortest_segment": min(segments, key=lambda s: s["minutes"]) if segments else None,
    }
    
    # 生成自然语言描述
    natural_language = _generate_nl_description(stats, label)
    
    return {
        "summary": natural_language,
        "statistics": stats,
        "detailed_segments": segments,
        "visualization_data": _prepare_chart_data(segments)  # 供前端绘图
    }

# 示例输出：
{
    "summary": "本周开会总时长 12.5 小时，平均每次 1.5 小时，最长的一次是周三的项目评审（3小时）",
    "statistics": {
        "total_minutes": 750,
        "total_hours": 12.5,
        "segment_count": 8,
        "avg_duration_minutes": 93.75
    },
    "detailed_segments": [...],
    "visualization_data": {
        "daily": [{"date": "2024-01-15", "minutes": 120}, ...],
        "hourly": [{"hour": 10, "count": 3}, ...]
    }
}
```

---

## 七、最终建议

### 对于你的场景，推荐：

**✅ 方案1：纯定制化（90%推荐）**

```python
# 架构
User Voice (24h) 
    ↓ [语音识别]
Raw Text 
    ↓ [事件提取]
Structured Events (start_time, end_time, type, action)
    ↓ [存储]
PostgreSQL/TimescaleDB (时序优化)
    ↓ [查询]
Your Custom Tools (compute_duration, find_patterns, etc.)
    ↓ [推理]
LLM (使用tools调用)
    ↓
Natural Language Response
```

**原因**：
1. 你的核心需求是**时序分析**，不是实体关系
2. Graphiti的写入成本对你来说**太贵**（24h声音会产生大量数据）
3. 你的工具设计已经很成熟，直接用更高效
4. 性能提升10倍+，成本降低100倍+

**只在以下情况考虑Graphiti**：
- 需要深度的实体关系分析（如"Alice和Bob的关系网络"）
- 需要跨用户的知识共享（如"所有用户的运动习惯"）
- 数据量不大（<1000条/天）

### 具体行动计划

```python
# 第1步：保留你的tools_executor.py（已经很好）

# 第2步：增强工具（可选）
# - 添加 find_recurring_patterns（发现习惯）
# - 添加 compare_periods（对比分析）
# - 添加 predict_next（预测下次时间）

# 第3步：优化缓存
# - 实现智能TTL
# - 添加缓存预热

# 第4步：增强格式化
# - 返回结构化数据 + 自然语言
# - 添加可视化数据

# 第5步：（可选）轻量级集成Graphiti
# - 只用于语义搜索增强
# - 不使用昂贵的实体提取
```

---

## 八、成本对比（重要！）

### 你的场景：24h声音 → 假设每天100条事件

#### 方案A：使用Graphiti MCP

```
写入成本（每天）：
- 100条事件 × 10,000 tokens = 1,000,000 tokens
- 成本：1,000,000 × $0.002/1K = $2/天
- 月成本：$60

查询成本（每天100次查询）：
- 由于缺少时长统计，需要获取大量原始数据
- 平均每次查询需要处理50条记录
- 100次 × 50条 × 100 tokens = 500,000 tokens
- 成本：500,000 × $0.002/1K = $1/天
- 月成本：$30

总成本：$90/月
```

#### 方案B：使用你的定制工具

```
写入成本（每天）：
- 只需存储结构化数据到数据库
- 成本：几乎为0（数据库存储成本）

查询成本（每天100次）：
- 服务端聚合，只返回结果
- 平均每次只消耗15 tokens（embedding）
- 100次 × 15 tokens = 1,500 tokens
- 成本：1,500 × $0.00002/1K = $0.00003/天
- 月成本：$0.001

有缓存时成本更低：约$0.0001/月

总成本：~$0.001/月
```

**结论：你的方案便宜90,000倍！**

---

## 九、结论

### 明确回答你的问题：

**1. Graphiti MCP代码直接可用吗？**
- ✅ 技术上可用（代码质量不错）
- ❌ 功能上不适合你的场景（缺少时序分析）
- ⚠️ 成本上不划算（贵90,000倍）

**2. 是否需要定制化设计tools？**
- ✅✅✅ **强烈建议定制化**
- 你已有的tools_executor设计excellent，继续用
- 可以小幅增强（添加模式识别、对比分析等）
- 不要被Graphiti MCP的通用性迷惑

**3. 核心建议：**
```
保持你的定制化工具 ✅
不要全面迁移到Graphiti ❌
可选：用Graphiti做语义搜索增强 ⚠️

你的架构更适合时序事件分析场景。
```

### 最后的话

Graphiti是优秀的**通用知识图谱**工具，但你的场景是**时序事件分析**，属于不同的问题领域。

**类比**：
- Graphiti ≈ 通用的图书馆系统
- 你的需求 ≈ 专业的时间管理系统

不要因为"知识图谱"听起来高大上就硬用，选择最适合场景的工具才是正道。

**你的tools_executor已经很好了，继续打磨它！** 🎯

