# Graphiti 写流程分析与 LLM 调用统计

## 一、你的理解评估 ✅

你的理解**基本正确**！我对照了代码，你的12个步骤覆盖了核心流程。以下是一些细节补充：

### 你的理解（原文）vs 实际实现

| 步骤 | 你的理解 | 实际实现 | 准确度 |
|-----|---------|---------|-------|
| 1 | scene原文作为episode存储 | ✅ 创建EpisodicNode对象 | ✅ 正确 |
| 2 | LLM抽取entity列表 | ✅ extract_nodes() 调用LLM | ✅ 正确 |
| 3 | 基于name进行向量搜索+规则+LLM匹配 | ✅ resolve_extracted_nodes() | ✅ 正确 |
| 4 | LLM抽取edge信息 | ✅ extract_edges() 调用LLM | ✅ 正确 |
| 5 | 更新edge的uuid_map | ✅ 使用uuid_map更新边的节点引用 | ✅ 正确 |
| 6 | 遍历edge找相关edge | ✅ get_related_edges_bulk() | ✅ 正确 |
| 7 | 语义搜索相似边（去重） | ✅ search_cross_encoder_edges() | ✅ 正确 |
| 8 | 语义搜索冲突边 | ✅ 同一个search调用，用于invalidation | ✅ 正确 |
| 9.1 | 处理重复边 | ✅ LLM判断duplicate_facts | ✅ 正确 |
| 9.2 | 处理冲突边 | ✅ LLM判断contradicted_facts | ✅ 正确 |
| 9.3 | 标记矛盾边失效 | ✅ resolve_edge_contradictions() | ✅ 正确 |
| 10 | LLM提取episode属性 | ✅ extract_attributes_from_nodes() | ✅ 正确 |
| 11 | episode和entity落盘 | ✅ add_nodes_and_edges_bulk() | ✅ 正确 |
| 12.1 | 找entity所属社区 | ✅ determine_entity_community() | ✅ 正确 |
| 12.2 | 合并entity和community摘要 | ✅ summarize_pair() LLM调用 | ✅ 正确 |
| 12.3 | 生成community描述 | ✅ generate_summary_description() | ✅ 正确 |
| 12.4 | 构建has_member边 | ✅ build_community_edges() | ✅ 正确 |

### 补充细节

你遗漏的几个小点（不影响整体理解）：

1. **Reflexion技术**：在步骤2和4中，LLM会进行自我反思，确保没有遗漏重要信息
   - 提取entity后，LLM会反思是否遗漏了实体
   - 提取edge后，LLM会反思是否遗漏了关系
   
2. **Embedding生成**：在多个阶段会生成向量
   - 步骤3：entity的name_embedding（用于去重）
   - 步骤7：edge的fact_embedding（用于相似度搜索）
   - 步骤10：entity的summary_embedding（用于语义搜索）
   - 步骤12：community的name_embedding

3. **历史上下文**：每个LLM调用都会带上previous_episodes作为上下文

## 二、完整流程图（带LLM调用标注）

```
┌──────────────────────────────────────────────────────────────┐
│ 阶段0: 初始化                                                 │
│ - 验证参数                                                    │
│ - 获取previous_episodes (检索最近的N个episodes作为上下文)     │
│ - 创建EpisodicNode对象                                        │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 阶段1: 节点提取（extract_nodes）                              │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 📞 LLM调用 #1: extract_nodes.extract_message/text/json   │ │
│ │   输入: episode_content + previous_episodes              │ │
│ │   输出: ExtractedEntities (实体列表)                     │ │
│ │   Token: 中等 (~2K-8K input + ~1K output)               │ │
│ └──────────────────────────────────────────────────────────┘ │
│                         │                                     │
│                         ▼                                     │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 📞 LLM调用 #2: extract_nodes.reflexion (可选，最多1次)   │ │
│ │   输入: episode + 已提取的实体                           │ │
│ │   输出: MissedEntities (遗漏的实体)                      │ │
│ │   Token: 小 (~1K-3K input + ~500 output)                │ │
│ └──────────────────────────────────────────────────────────┘ │
│                         │                                     │
│                         ▼                                     │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 如果有遗漏实体，重新调用 LLM #1 (最多1次)                │ │
│ └──────────────────────────────────────────────────────────┘ │
│ 结果: extracted_nodes (5-20个实体，典型值10个)               │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 阶段2: 节点去重（resolve_extracted_nodes）                    │
│ - 向量搜索找相似实体                                          │
│ - 规则匹配（字符串相似度）                                     │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 📞 LLM调用 #3-N: dedupe_nodes (针对每个候选重复)          │ │
│ │   输入: 新entity + 候选重复entity                         │ │
│ │   输出: NodeResolution (是否重复)                         │ │
│ │   并发: 对每个实体的去重候选并发调用                       │ │
│ │   Token: 小 (~500-1K input + ~100 output) × 去重次数     │ │
│ │   次数: 典型 0-10次 (取决于有多少候选重复)                │ │
│ └──────────────────────────────────────────────────────────┘ │
│ 结果: nodes (去重后5-15个), uuid_map (映射关系)              │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 阶段3: 边提取（extract_edges）                                │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 📞 LLM调用 #N+1: extract_edges.edge                       │ │
│ │   输入: episode + nodes + previous_episodes               │ │
│ │   输出: ExtractedEdges (关系列表)                         │ │
│ │   Token: 大 (~3K-10K input + ~2K output)                 │ │
│ └──────────────────────────────────────────────────────────┘ │
│                         │                                     │
│                         ▼                                     │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 📞 LLM调用 #N+2: extract_edges.reflexion (最多1次)        │ │
│ │   输入: episode + 已提取的关系                            │ │
│ │   输出: MissingFacts (遗漏的关系)                         │ │
│ │   Token: 小 (~1K-3K input + ~500 output)                 │ │
│ └──────────────────────────────────────────────────────────┘ │
│                         │                                     │
│                         ▼                                     │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 如果有遗漏关系，重新调用 LLM #N+1 (最多1次)              │ │
│ └──────────────────────────────────────────────────────────┘ │
│ 结果: extracted_edges (3-30条边，典型值10-15条)              │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 阶段4: 边去重和冲突处理（resolve_extracted_edges）            │
│ - 使用uuid_map更新边的节点引用                                │
│ - 查询每条边的相关边（同节点对）                               │
│ - 向量搜索语义相似边（用于冲突检测）                           │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 📞 LLM调用 #M-P: dedupe_edges.resolve_edge (每条边)      │ │
│ │   输入: 新edge + related_edges + invalidation_candidates │ │
│ │   输出: EdgeDuplicate (duplicate_facts, contradicted)    │ │
│ │   并发: 对每条新边并发调用                                │ │
│ │   Token: 中 (~1K-3K input + ~200 output) × 边数量        │ │
│ │   次数: 10-15次 (等于extracted_edges数量)                │ │
│ └──────────────────────────────────────────────────────────┘ │
│ - 处理重复边（复用已有边，更新episodes）                      │
│ - 处理冲突边（标记旧边为失效）                                 │
│ 结果: resolved_edges, invalidated_edges                      │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 阶段5: 节点属性提取（extract_attributes_from_nodes）          │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 📞 LLM调用 #P+1-Q: extract_attributes (每个node)         │ │
│ │   输入: node + episode + previous_episodes                │ │
│ │   输出: 实体的详细属性（如果有entity_type定义）            │ │
│ │   并发: 对每个节点并发调用                                │ │
│ │   Token: 小 (~500-1K input + ~200 output) × 节点数       │ │
│ │   次数: 5-15次 (等于nodes数量，如果定义了entity_type)     │ │
│ │   注意: 如果未定义entity_type，此步骤跳过！               │ │
│ └──────────────────────────────────────────────────────────┘ │
│                         │                                     │
│                         ▼                                     │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 📞 LLM调用 #Q+1-R: extract_summary (每个node，可选)      │ │
│ │   输入: node + episode + previous_episodes                │ │
│ │   输出: EntitySummary (节点摘要)                          │ │
│ │   并发: 对每个节点并发调用                                │ │
│ │   Token: 中 (~1K-2K input + ~300 output) × 节点数        │ │
│ │   次数: 5-15次 (等于nodes数量)                           │ │
│ └──────────────────────────────────────────────────────────┘ │
│ - 生成name_embedding和summary_embedding                      │
│ 结果: hydrated_nodes (填充完整属性的节点)                     │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 阶段6: 数据持久化                                             │
│ - 创建episodic_edges (episode到entity的MENTIONS边)           │
│ - 批量保存nodes, edges, episode到数据库                       │
│ 结果: 数据已写入图数据库                                       │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 阶段7: 社区更新（update_communities）【可选，默认关闭】       │
│ if update_communities == True:                               │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ 对每个node调用 update_community():                        │ │
│ │                                                           │ │
│ │ 📞 LLM调用 #R+1-S: summarize_pair (每个node)             │ │
│ │   输入: entity.summary + community.summary               │ │
│ │   输出: 合并后的新摘要                                    │ │
│ │   Token: 中 (~1K-3K input + ~500 output) × 节点数        │ │
│ │   次数: 5-15次 (等于nodes数量)                           │ │
│ │                                                           │ │
│ │ 📞 LLM调用 #S+1-T: generate_summary_description          │ │
│ │   输入: 新的community摘要                                │ │
│ │   输出: 简短的community名称                              │ │
│ │   Token: 小 (~500-1K input + ~50 output) × 节点数        │ │
│ │   次数: 5-15次 (等于nodes数量)                           │ │
│ └──────────────────────────────────────────────────────────┘ │
│ - 生成community的name_embedding                              │
│ - 保存更新的community和HAS_MEMBER边                          │
│ 结果: communities, community_edges                           │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         ▼
                     完成 ✅
```

## 三、LLM调用次数统计

### 场景设定

**假设一个典型的scene输入**：
- episode长度: 500-1000字
- 提取出: 10个entity
- 生成: 15条edge
- 去重候选: 平均每个entity有1个候选重复 (共10次去重判断)
- 边相关: 每条edge平均有2条related_edges和3条invalidation_candidates

### 不开启 update_communities (默认)

| 阶段 | LLM调用次数 | 备注 |
|-----|-----------|------|
| **节点提取** |  |  |
| extract_nodes | 1-2次 | 主提取1次 + reflexion最多1次 |
| **节点去重** |  |  |
| dedupe_nodes | 0-10次 | 取决于找到多少候选重复，并发 |
| **边提取** |  |  |
| extract_edges | 1-2次 | 主提取1次 + reflexion最多1次 |
| **边去重/冲突** |  |  |
| resolve_edge | 15次 | 每条edge一次，并发 |
| **节点属性** |  |  |
| extract_attributes | 0-10次 | 仅当定义entity_type时，并发 |
| extract_summary | 10次 | 每个node一次，并发 |
| **总计** | **27-49次** | 典型值: ~35次 |

### 开启 update_communities = True

| 阶段 | LLM调用次数 | 备注 |
|-----|-----------|------|
| 前面所有步骤 | 27-49次 | 同上 |
| **社区更新** |  |  |
| summarize_pair | 10次 | 每个node一次，并发 |
| generate_summary_description | 10次 | 每个node一次，并发 |
| **总计** | **47-69次** | 典型值: ~55次 |

### 增幅分析

```
不开启社区: ~35次 LLM调用
开启社区:  ~55次 LLM调用

增幅: +20次 (+57%)
```

## 四、Token消耗估算

### Token消耗表（单次调用）

| LLM调用类型 | Input Tokens | Output Tokens | Total | 成本系数 |
|-----------|-------------|---------------|-------|---------|
| extract_nodes | 2,000-8,000 | 500-1,500 | ~4,000 | 1.0x |
| reflexion | 1,000-3,000 | 200-500 | ~1,500 | 0.4x |
| dedupe_nodes | 500-1,000 | 50-150 | ~600 | 0.15x |
| extract_edges | 3,000-10,000 | 1,000-3,000 | ~6,000 | 1.5x |
| resolve_edge | 1,000-3,000 | 100-300 | ~1,500 | 0.4x |
| extract_attributes | 500-1,000 | 100-300 | ~600 | 0.15x |
| extract_summary | 1,000-2,000 | 200-500 | ~1,200 | 0.3x |
| summarize_pair | 1,000-3,000 | 300-800 | ~1,800 | 0.45x |
| generate_summary_desc | 500-1,000 | 20-100 | ~600 | 0.15x |

### Token总消耗（不含社区更新）

```
节点提取:
  - extract_nodes (1次): 4,000 tokens
  - reflexion (1次): 1,500 tokens
  小计: 5,500 tokens (1.0x基准)

节点去重:
  - dedupe_nodes (10次): 600 × 10 = 6,000 tokens
  小计: 6,000 tokens (1.1x)

边提取:
  - extract_edges (1次): 6,000 tokens
  - reflexion (1次): 1,500 tokens
  小计: 7,500 tokens (1.4x)

边去重/冲突:
  - resolve_edge (15次): 1,500 × 15 = 22,500 tokens
  小计: 22,500 tokens (4.1x)

节点属性:
  - extract_attributes (10次): 600 × 10 = 6,000 tokens
  - extract_summary (10次): 1,200 × 10 = 12,000 tokens
  小计: 18,000 tokens (3.3x)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
总计（不含社区）: ~59,500 tokens
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Token总消耗（含社区更新）

```
前面所有步骤: 59,500 tokens

社区更新:
  - summarize_pair (10次): 1,800 × 10 = 18,000 tokens
  - generate_summary_desc (10次): 600 × 10 = 6,000 tokens
  小计: 24,000 tokens (4.4x)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
总计（含社区）: ~83,500 tokens
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Token增幅分析

```
不开启社区: ~59,500 tokens
开启社区:   ~83,500 tokens

增幅: +24,000 tokens (+40%)
```

## 五、成本估算（以OpenAI GPT-4为例）

### GPT-4 Pricing (2024)
- Input: $0.03 / 1K tokens
- Output: $0.06 / 1K tokens
- 平均（假设input:output = 2:1）: ~$0.04 / 1K tokens

### 单次scene写入成本

#### 不开启社区
```
Token消耗: 59,500 tokens
成本: 59.5 × $0.04 = $2.38
```

#### 开启社区
```
Token消耗: 83,500 tokens
成本: 83.5 × $0.04 = $3.34

增加成本: $0.96 (+40%)
```

### 规模化成本估算

| 每日scene数 | 不含社区 | 含社区 | 增加成本 |
|-----------|---------|-------|---------|
| 100 | $238 | $334 | +$96 |
| 1,000 | $2,380 | $3,340 | +$960 |
| 10,000 | $23,800 | $33,400 | +$9,600 |
| 100,000 | $238,000 | $334,000 | +$96,000 |

## 六、性能影响分析

### 耗时分析

假设平均每次LLM调用耗时（并发情况下）：
- 小模型调用: 0.5-1秒
- 中等模型调用: 1-2秒
- 大模型调用: 2-4秒

由于使用了并发（semaphore_gather），实际耗时取决于最长的那批调用。

#### 不含社区更新

```
串行阶段（无法并发）:
  - extract_nodes: 2秒
  - reflexion: 1秒
  - extract_edges: 3秒
  - reflexion: 1秒
  小计: 7秒

并发阶段（取最长）:
  - dedupe_nodes (10个并发): 1秒
  - resolve_edge (15个并发): 2秒
  - extract_summary (10个并发): 2秒
  小计: 5秒

总耗时: ~12秒
```

#### 含社区更新

```
前面步骤: 12秒

并发阶段（社区）:
  - summarize_pair (10个并发): 2秒
  - generate_summary_desc (10个并发): 1秒
  小计: 3秒

总耗时: ~15秒

增加时间: +3秒 (+25%)
```

## 七、优化建议

### 1. 降低社区更新成本

**策略A: 延迟更新**
```python
# 不在每次add_episode时更新社区
# 而是定期批量更新
await graphiti.add_episode(..., update_communities=False)

# 每小时或每100个episode后批量更新
await build_communities(driver, llm_client, group_ids=None)
```
- 优点: 大幅降低成本（节省40%）
- 缺点: 社区信息有延迟

**策略B: 选择性更新**
```python
# 只对重要节点更新社区
def should_update_community(node):
    return node.importance_score > 0.7

# 自定义更新逻辑
if should_update_community(node):
    await update_community(...)
```
- 优点: 平衡成本和实时性
- 缺点: 需要定义重要性标准

**策略C: 采样更新**
```python
import random

# 只更新50%的节点的社区
if random.random() < 0.5:
    await update_community(...)
```
- 优点: 简单粗暴降低成本
- 缺点: 社区信息不完整

### 2. 使用更便宜的模型

```python
# 对不重要的LLM调用使用小模型
llm_config = {
    'extract_nodes': 'gpt-4',  # 关键任务用大模型
    'extract_edges': 'gpt-4',
    'dedupe_nodes': 'gpt-3.5-turbo',  # 简单任务用小模型
    'resolve_edge': 'gpt-3.5-turbo',
    'summarize_pair': 'gpt-3.5-turbo',  # 社区更新用小模型
}
```
- GPT-3.5成本约为GPT-4的1/10
- 社区更新用GPT-3.5可节省~90%社区相关成本

### 3. 缓存和去重

```python
# 缓存相似的LLM请求
# 如果10分钟内有相同的去重判断，直接复用结果
cache = LRUCache(maxsize=1000)
```

### 4. 调整并发数

```python
# 增加并发数以提高吞吐量（注意API限流）
graphiti = Graphiti(
    ...,
    max_coroutines=20  # 默认10，可适当增加
)
```

## 八、总结

### 核心数据

| 指标 | 不含社区 | 含社区 | 增幅 |
|-----|---------|-------|------|
| LLM调用次数 | ~35次 | ~55次 | +57% |
| Token消耗 | ~59.5K | ~83.5K | +40% |
| 成本（GPT-4） | $2.38 | $3.34 | +40% |
| 耗时 | ~12秒 | ~15秒 | +25% |

### 建议

1. **如果你的场景需要社区功能**（如层次化检索、聚类分析）：
   - 开启 `update_communities=True`
   - 接受40%的成本增加
   - 考虑使用更便宜的模型做社区更新

2. **如果你主要关注实体和关系**：
   - 保持 `update_communities=False`（默认）
   - 定期（如每天）批量重建社区
   - 节省40%成本

3. **混合策略**（推荐）：
   - 日常写入: `update_communities=False`
   - 重要节点: 单独调用 `update_community()`
   - 定期全量: 每周运行一次 `build_communities()`

### Token消耗占比（含社区）

```
边去重/冲突: 22,500 tokens (27%) ████████████
社区更新:    24,000 tokens (29%) ██████████████
节点属性:    18,000 tokens (22%) ███████████
边提取:       7,500 tokens (9%)  ████
节点去重:     6,000 tokens (7%)  ███
节点提取:     5,500 tokens (7%)  ███

总计: 83,500 tokens
```

**最大成本贡献**：社区更新(29%) 和 边去重(27%)

**优化方向**：
1. 优化社区更新策略 → 最多节省29%成本
2. 减少边去重调用（提高related_edges查询精度）→ 节省部分边去重成本
3. 使用更便宜的模型 → 整体降低10-90%成本

希望这份详细的分析对你有帮助！🎉

