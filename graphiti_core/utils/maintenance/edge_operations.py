"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import logging
from datetime import datetime
from time import time

from pydantic import BaseModel
from typing_extensions import LiteralString

from graphiti_core.driver.driver import GraphDriver, GraphProvider
from graphiti_core.edges import (
    CommunityEdge,
    EntityEdge,
    EpisodicEdge,
    create_entity_edge_embeddings,
)
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.helpers import MAX_REFLEXION_ITERATIONS, semaphore_gather
from graphiti_core.llm_client import LLMClient
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodicNode
from graphiti_core.prompts import prompt_library
from graphiti_core.prompts.dedupe_edges import EdgeDuplicate
from graphiti_core.prompts.extract_edges import ExtractedEdges, MissingFacts
from graphiti_core.search.search import search
from graphiti_core.search.search_config import SearchResults
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import SearchFilters
from graphiti_core.utils.datetime_utils import ensure_utc, utc_now
from graphiti_core.utils.maintenance.dedup_helpers import _normalize_string_exact

DEFAULT_EDGE_NAME = 'RELATES_TO'

logger = logging.getLogger(__name__)


def build_episodic_edges(
    entity_nodes: list[EntityNode],
    episode_uuid: str,
    created_at: datetime,
) -> list[EpisodicEdge]:
    episodic_edges: list[EpisodicEdge] = [
        EpisodicEdge(
            source_node_uuid=episode_uuid,
            target_node_uuid=node.uuid,
            created_at=created_at,
            group_id=node.group_id,
        )
        for node in entity_nodes
    ]

    logger.debug(f'Built episodic edges: {episodic_edges}')

    return episodic_edges


def build_community_edges(
    entity_nodes: list[EntityNode],
    community_node: CommunityNode,
    created_at: datetime,
) -> list[CommunityEdge]:
    edges: list[CommunityEdge] = [
        CommunityEdge(
            source_node_uuid=community_node.uuid,
            target_node_uuid=node.uuid,
            created_at=created_at,
            group_id=community_node.group_id,
        )
        for node in entity_nodes
    ]

    return edges


async def extract_edges(
    clients: GraphitiClients,
    episode: EpisodicNode,
    nodes: list[EntityNode],
    previous_episodes: list[EpisodicNode],
    edge_type_map: dict[tuple[str, str], list[str]],
    group_id: str = '',
    edge_types: dict[str, type[BaseModel]] | None = None,
) -> list[EntityEdge]:
    """
    从 episode 中提取实体之间的关系边
    
    主要功能:
    1. 使用 LLM 识别文本中实体之间的关系（如 "张三认识李四"）
    2. 使用 Reflexion 技术进行自我反思，确保没有遗漏重要关系
    3. 解析时间信息（关系的有效期和失效期）
    4. 创建 EntityEdge 对象
    
    参数:
    - nodes: 已提取的实体节点列表
    - edge_type_map: 边类型映射，定义哪些实体类型之间可以建立哪些关系
    - edge_types: 自定义边类型的 Pydantic 模型定义
    """
    start = time()

    # ========================================
    # 阶段 0: 初始化配置
    # ========================================
    extract_edges_max_tokens = 16384  # LLM 响应的最大 token 数
    llm_client = clients.llm_client

    # ========================================
    # 阶段 1: 构建边类型签名映射
    # ========================================
    # 目的: 将边类型名称映射到它连接的实体类型对
    # 示例: {'WorksFor': ('Person', 'Company'), 'LocatedIn': ('Company', 'City')}
    # 这告诉 LLM 什么类型的实体之间可以建立什么类型的关系
    edge_type_signature_map: dict[str, tuple[str, str]] = {
        edge_type: signature
        for signature, edge_types in edge_type_map.items()
        for edge_type in edge_types
    }

    # ========================================
    # 阶段 2: 构建边类型上下文
    # ========================================
    # 为 LLM 准备边类型的描述信息
    # 包括: 边类型名称、适用的实体类型对、边的描述
    edge_types_context = (
        [
            {
                'fact_type_name': type_name,  # 边类型名称 (如 'WorksFor')
                'fact_type_signature': edge_type_signature_map.get(type_name, ('Entity', 'Entity')),  # 实体类型对
                'fact_type_description': type_model.__doc__,  # 边类型描述
            }
            for type_name, type_model in edge_types.items()
        ]
        if edge_types is not None
        else []
    )

    # ========================================
    # 阶段 3: 准备 LLM 上下文
    # ========================================
    # 构建提示词上下文，包含所有必要信息供 LLM 提取关系
    context = {
        'episode_content': episode.content,  # 当前 episode 的文本内容
        'nodes': [  # 已识别的实体节点列表（带索引）
            {'id': idx, 'name': node.name, 'entity_types': node.labels}
            for idx, node in enumerate(nodes)
        ],
        'previous_episodes': [ep.content for ep in previous_episodes],  # 历史上下文
        'reference_time': episode.valid_at,  # 参考时间点
        'edge_types': edge_types_context,  # 可用的边类型定义
        'custom_prompt': '',  # 自定义提示（用于 Reflexion）
    }

    # ========================================
    # 阶段 4: Reflexion 循环 - 使用自我反思提高提取质量
    # ========================================
    # Reflexion 技术: LLM 先提取关系，然后反思是否有遗漏，再进行补充提取
    # 这个循环最多执行 MAX_REFLEXION_ITERATIONS 次
    facts_missed = True  # 标记是否还有遗漏的关系
    reflexion_iterations = 0
    
    while facts_missed and reflexion_iterations <= MAX_REFLEXION_ITERATIONS:
        # 步骤 4.1: 使用 LLM 提取关系边
        # LLM 会分析文本，识别实体之间的关系，返回结构化数据
        llm_response = await llm_client.generate_response(
            prompt_library.extract_edges.edge(context),
            response_model=ExtractedEdges,
            max_tokens=extract_edges_max_tokens,
            group_id=group_id,
            prompt_name='extract_edges.edge',
        )
        edges_data = ExtractedEdges(**llm_response).edges

        # 记录已提取的关系，用于下一轮 Reflexion
        context['extracted_facts'] = [edge_data.fact for edge_data in edges_data]

        reflexion_iterations += 1
        
        # 步骤 4.2: Reflexion - 反思是否有遗漏
        if reflexion_iterations < MAX_REFLEXION_ITERATIONS:
            # 让 LLM 审查已提取的关系，判断是否遗漏了重要信息
            reflexion_response = await llm_client.generate_response(
                prompt_library.extract_edges.reflexion(context),
                response_model=MissingFacts,
                max_tokens=extract_edges_max_tokens,
                group_id=group_id,
                prompt_name='extract_edges.reflexion',
            )

            missing_facts = reflexion_response.get('missing_facts', [])

            # 如果发现遗漏，将其加入自定义提示，下一轮会重点关注
            custom_prompt = 'The following facts were missed in a previous extraction: '
            for fact in missing_facts:
                custom_prompt += f'\n{fact},'

            context['custom_prompt'] = custom_prompt

            # 如果没有遗漏了，退出循环
            facts_missed = len(missing_facts) != 0

    end = time()
    logger.debug(f'Extracted new edges: {edges_data} in {(end - start) * 1000} ms')

    if len(edges_data) == 0:
        return []

    # ========================================
    # 阶段 5: 转换为 EntityEdge 对象
    # ========================================
    # 将 LLM 返回的结构化数据转换为系统使用的 EntityEdge 对象
    edges = []
    for edge_data in edges_data:
        # 步骤 5.1: 初始化时间信息变量
        valid_at = edge_data.valid_at  # 关系的生效时间（如 "2023 年开始工作于..."）
        invalid_at = edge_data.invalid_at  # 关系的失效时间（如 "2024 年离职"）
        valid_at_datetime = None
        invalid_at_datetime = None

        # 步骤 5.2: 过滤空关系
        if not edge_data.fact.strip():
            continue

        # 步骤 5.3: 获取源节点和目标节点的索引
        # LLM 返回的是节点在列表中的索引（如 source_entity_id=0, target_entity_id=1）
        source_node_idx = edge_data.source_entity_id
        target_node_idx = edge_data.target_entity_id

        # 步骤 5.4: 验证节点索引的有效性
        if len(nodes) == 0:
            logger.warning('No entities provided for edge extraction')
            continue

        # 检查索引是否在有效范围内
        if not (0 <= source_node_idx < len(nodes) and 0 <= target_node_idx < len(nodes)):
            logger.warning(
                f'Invalid entity IDs in edge extraction for {edge_data.relation_type}. '
                f'source_entity_id: {source_node_idx}, target_entity_id: {target_node_idx}, '
                f'but only {len(nodes)} entities available (valid range: 0-{len(nodes) - 1})'
            )
            continue
        
        # 步骤 5.5: 通过索引获取节点的 UUID
        source_node_uuid = nodes[source_node_idx].uuid
        target_node_uuid = nodes[target_node_idx].uuid

        # 步骤 5.6: 解析时间信息
        # 将 ISO 格式的时间字符串转换为 datetime 对象
        if valid_at:
            try:
                valid_at_datetime = ensure_utc(
                    datetime.fromisoformat(valid_at.replace('Z', '+00:00'))
                )
            except ValueError as e:
                logger.warning(f'WARNING: Error parsing valid_at date: {e}. Input: {valid_at}')

        if invalid_at:
            try:
                invalid_at_datetime = ensure_utc(
                    datetime.fromisoformat(invalid_at.replace('Z', '+00:00'))
                )
            except ValueError as e:
                logger.warning(f'WARNING: Error parsing invalid_at date: {e}. Input: {invalid_at}')
        
        # 步骤 5.7: 创建 EntityEdge 对象
        # 这个边表示两个实体之间的关系
        edge = EntityEdge(
            source_node_uuid=source_node_uuid,  # 起始节点
            target_node_uuid=target_node_uuid,  # 目标节点
            name=edge_data.relation_type,  # 关系类型 (如 'WorksFor', 'Knows')
            group_id=group_id,  # 图分区 ID
            fact=edge_data.fact,  # 关系的自然语言描述 (如 "张三在阿里巴巴工作")
            episodes=[episode.uuid],  # 记录该关系首次出现的 episode
            created_at=utc_now(),  # 创建时间
            valid_at=valid_at_datetime,  # 关系生效时间
            invalid_at=invalid_at_datetime,  # 关系失效时间
        )
        edges.append(edge)
        logger.debug(
            f'Created new edge: {edge.name} from (UUID: {edge.source_node_uuid}) to (UUID: {edge.target_node_uuid})'
        )

    logger.debug(f'Extracted edges: {[(e.name, e.uuid) for e in edges]}')

    return edges


async def resolve_extracted_edges(
    clients: GraphitiClients,
    extracted_edges: list[EntityEdge],
    episode: EpisodicNode,
    entities: list[EntityNode],
    edge_types: dict[str, type[BaseModel]],
    edge_type_map: dict[tuple[str, str], list[str]],
) -> tuple[list[EntityEdge], list[EntityEdge]]:
    """
    解析提取的边，处理去重和冲突
    
    主要功能:
    1. 去重：合并相同的边
    2. 冲突检测：识别矛盾的边（如时间冲突）
    3. 边失效：将过时的边标记为 invalid
    
    示例场景:
    - 原有边: "用户8点去健身" (valid_at=8:00)
    - 新边: "用户10点去健身" (valid_at=10:00)
    - 结果: 
      * 8点的边被标记失效 (invalid_at=10:00, expired_at=now)
      * 10点的边作为新的有效事实
    """
    # ========================================
    # 阶段 1: 快速去重 - 在本批次内去除完全相同的边
    # ========================================
    # 目的: 避免将相同的边重复发送给 LLM 处理，提高效率
    seen: dict[tuple[str, str, str], EntityEdge] = {}
    deduplicated_edges: list[EntityEdge] = []

    for edge in extracted_edges:
        # 构建唯一键: (源节点, 目标节点, 归一化后的事实描述)
        key = (
            edge.source_node_uuid,
            edge.target_node_uuid,
            _normalize_string_exact(edge.fact),
        )
        if key not in seen:
            seen[key] = edge
            deduplicated_edges.append(edge)

    extracted_edges = deduplicated_edges

    driver = clients.driver
    llm_client = clients.llm_client
    embedder = clients.embedder
    
    # 为所有边生成 embedding 向量（用于语义搜索）
    await create_entity_edge_embeddings(embedder, extracted_edges)

    # ========================================
    # 阶段 2: 查询相关边 - 为每条新边找到可能重复的候选边
    # ========================================
    # 步骤 2.1: 获取相同节点对之间的所有边
    # 例如: 查询 (用户, 健身房) 之间的所有已存在的边
    # 注意valid_edges_list是一个二维数组

    # extracted_edges = [新边1, 新边2, 新边3]  # 长度 = 3
    # # 为每条新边执行一次查询，返回 3 个结果
    # valid_edges_list = [
    #     [已有边...],  # 新边1 的查询结果（可能是空列表）
    #     [已有边...],  # 新边2 的查询结果（可能是空列表）
    #     [已有边...],  # 新边3 的查询结果（可能是空列表）
    # ]  # 外层长度 = 3，保证与 extracted_edges 相等
    valid_edges_list: list[list[EntityEdge]] = await semaphore_gather(
        *[
            EntityEdge.get_between_nodes(driver, edge.source_node_uuid, edge.target_node_uuid)
            for edge in extracted_edges
        ]
    )

    # 步骤 2.2: 使用语义搜索找到相似的边（用于去重）
    # 只在相同节点对的边中搜索
    # 例如: "用户8点去健身" 和 "用户在早上去健身房" 可能被识别为相似
    # 【pythonic写法】semaphore_gather并发执行搜索任务，返回搜索结果
    related_edges_results: list[SearchResults] = await semaphore_gather(
        *[
            # 搜索相似的边，返回搜索结果
            search(
                clients,
                extracted_edge.fact,
                group_ids=[extracted_edge.group_id],
                config=EDGE_HYBRID_SEARCH_RRF,
                search_filter=SearchFilters(edge_uuids=[edge.uuid for edge in valid_edges]),
            )
            # 遍历提取的边和相同节点对的边，改变search的入参
            # zip是同时遍历两个容器的意思，strict=True表示两个容器长度必须相同
            for extracted_edge, valid_edges in zip(extracted_edges, valid_edges_list, strict=True)
        ]
    )

    related_edges_lists: list[list[EntityEdge]] = [result.edges for result in related_edges_results]

    # ========================================
    # 阶段 3: 查询冲突候选边 - 找到可能矛盾的边
    # ========================================
    # 在整个图中搜索语义相似的边（不限制节点对）
    # 用于检测冲突，例如:
    # - "用户8点去健身" vs "用户10点去健身" (时间冲突)
    # - "张三在阿里工作" vs "张三在腾讯工作" (地点冲突)
    edge_invalidation_candidate_results: list[SearchResults] = await semaphore_gather(
        *[
            search(
                clients,
                extracted_edge.fact,
                group_ids=[extracted_edge.group_id],
                config=EDGE_HYBRID_SEARCH_RRF,
                search_filter=SearchFilters(),  # 不限制范围
            )
            for extracted_edge in extracted_edges
        ]
    )

    edge_invalidation_candidates: list[list[EntityEdge]] = [
        result.edges for result in edge_invalidation_candidate_results
    ]

    logger.debug(
        f'Related edges lists: {[(e.name, e.uuid) for edges_lst in related_edges_lists for e in edges_lst]}'
    )

    # ========================================
    # 阶段 4: 构建实体映射表
    # ========================================
    uuid_entity_map: dict[str, EntityNode] = {entity.uuid: entity for entity in entities}

    # ========================================
    # 阶段 5: 确定每条边允许的边类型
    # ========================================
    # 目的: 验证边的类型是否与节点类型匹配
    # 例如: "WorksFor" 边只能在 (Person, Company) 之间建立
    #       如果 LLM 错误地为 (Person, Person) 返回 "WorksFor"，则需要修正
    edge_types_lst: list[dict[str, type[BaseModel]]] = []
    custom_type_names = set(edge_types or {})
    
    for extracted_edge in extracted_edges:
        # 步骤 5.1: 获取源节点和目标节点的类型标签
        source_node = uuid_entity_map.get(extracted_edge.source_node_uuid)
        target_node = uuid_entity_map.get(extracted_edge.target_node_uuid)
        source_node_labels = (
            source_node.labels + ['Entity'] if source_node is not None else ['Entity']
        )
        target_node_labels = (
            target_node.labels + ['Entity'] if target_node is not None else ['Entity']
        )
        
        # 步骤 5.2: 构建所有可能的标签对组合
        # 例如: 如果源节点标签是 ['Entity', 'Person']，目标节点是 ['Entity', 'Company']
        # 则标签对包括: (Entity, Entity), (Entity, Company), (Person, Entity), (Person, Company)
        label_tuples = [
            (source_label, target_label)
            for source_label in source_node_labels
            for target_label in target_node_labels
        ]

        # 步骤 5.3: 查找允许的边类型
        extracted_edge_types = {}
        for label_tuple in label_tuples:
            type_names = edge_type_map.get(label_tuple, [])
            for type_name in type_names:
                type_model = edge_types.get(type_name)
                if type_model is None:
                    continue

                extracted_edge_types[type_name] = type_model

        edge_types_lst.append(extracted_edge_types)

    # 步骤 5.4: 验证和修正边类型
    for extracted_edge, extracted_edge_types in zip(extracted_edges, edge_types_lst, strict=True):
        allowed_type_names = set(extracted_edge_types)
        is_custom_name = extracted_edge.name in custom_type_names
        
        if not allowed_type_names:
            # 情况 A: 没有允许的自定义类型
            # 如果边使用了不允许的自定义类型，回退到默认类型
            if is_custom_name and extracted_edge.name != DEFAULT_EDGE_NAME:
                extracted_edge.name = DEFAULT_EDGE_NAME
            continue
        
        if is_custom_name and extracted_edge.name not in allowed_type_names:
            # 情况 B: 边使用了自定义类型，但不适用于当前节点对
            # 例如: 'WorksFor' 用于 (Person, Person) - 不允许
            # 回退到默认边类型
            extracted_edge.name = DEFAULT_EDGE_NAME

    # ========================================
    # 阶段 6: 并行解析所有边 - 去重和冲突处理
    # ========================================
    # 对每条新边执行:
    # 1. 与 related_edges 比较，识别重复
    # 2. 与 existing_edges 比较，识别冲突
    # 3. 返回: (解析后的边, 失效的边列表, 重复边列表)
    results: list[tuple[EntityEdge, list[EntityEdge], list[EntityEdge]]] = list(
        await semaphore_gather(
            *[
                resolve_extracted_edge(
                    llm_client,
                    extracted_edge,
                    related_edges,
                    existing_edges,
                    episode,
                    extracted_edge_types,
                    custom_type_names,
                )
                for extracted_edge, related_edges, existing_edges, extracted_edge_types in zip(
                    extracted_edges,
                    related_edges_lists,
                    edge_invalidation_candidates,
                    edge_types_lst,
                    strict=True,
                )
            ]
        )
    )

    # ========================================
    # 阶段 7: 收集结果
    # ========================================
    resolved_edges: list[EntityEdge] = []
    invalidated_edges: list[EntityEdge] = []
    for result in results:
        resolved_edge = result[0]  # 解析后的有效边
        invalidated_edge_chunk = result[1]  # 被标记为失效的边

        resolved_edges.append(resolved_edge)
        invalidated_edges.extend(invalidated_edge_chunk)

    logger.debug(f'Resolved edges: {[(e.name, e.uuid) for e in resolved_edges]}')

    # ========================================
    # 阶段 8: 为解析后的边和失效边生成 embedding
    # ========================================
    await semaphore_gather(
        create_entity_edge_embeddings(embedder, resolved_edges),
        create_entity_edge_embeddings(embedder, invalidated_edges),
    )

    return resolved_edges, invalidated_edges


def resolve_edge_contradictions(
    resolved_edge: EntityEdge, invalidation_candidates: list[EntityEdge]
) -> list[EntityEdge]:
    """
    处理边的时间冲突，标记过时的边为失效
    
    核心逻辑: 时间线管理
    - 每条边有 valid_at (生效时间) 和 invalid_at (失效时间)
    - 如果新边与旧边冲突，旧边需要被标记为失效
    
    示例场景:
    
    场景 1: 习惯改变
    - 旧边: "用户8点去健身" (valid_at=2023-01-01 08:00, invalid_at=null)
    - 新边: "用户10点去健身" (valid_at=2023-06-01 10:00, invalid_at=null)
    - 结果: 旧边被标记失效
      * 旧边.invalid_at = 2023-06-01 10:00 (在新边生效时失效)
      * 旧边.expired_at = now (记录失效的处理时间)
    
    场景 2: 临时改变
    - 旧边: "用户8点去健身" (valid_at=2023-01-01, invalid_at=null)
    - 新边: "用户10点去健身" (valid_at=2023-06-01, invalid_at=2023-06-30)
    - 结果: 旧边被标记临时失效
      * 旧边.invalid_at = 2023-06-01
      * 6月30日后，10点的边失效，但8点的边已经被标记为失效
    
    场景 3: 无冲突（时间不重叠）
    - 边A: (valid_at=2023-01-01, invalid_at=2023-05-31)
    - 边B: (valid_at=2023-06-01, invalid_at=null)
    - 结果: 不冲突，边A在边B生效前已失效
    """
    if len(invalidation_candidates) == 0:
        return []

    # ========================================
    # 核心冲突检测逻辑
    # ========================================
    invalidated_edges: list[EntityEdge] = []
    for edge in invalidation_candidates:
        # 步骤 1: 标准化时间为 UTC
        edge_invalid_at_utc = ensure_utc(edge.invalid_at)
        resolved_edge_valid_at_utc = ensure_utc(resolved_edge.valid_at)
        edge_valid_at_utc = ensure_utc(edge.valid_at)
        resolved_edge_invalid_at_utc = ensure_utc(resolved_edge.invalid_at)

        # ========================================
        # 情况 A: 无冲突 - 时间不重叠
        # ========================================
        # 条件 1: 旧边在新边生效前已失效
        # 例如: 旧边(valid=1月, invalid=5月) vs 新边(valid=6月)
        #       → 5月 <= 6月，旧边已失效，无冲突
        #
        # 条件 2: 新边在旧边生效前已失效
        # 例如: 新边(valid=1月, invalid=5月) vs 旧边(valid=6月)
        #       → 新边5月失效 <= 旧边6月生效，无冲突
        if (
            edge_invalid_at_utc is not None
            and resolved_edge_valid_at_utc is not None
            and edge_invalid_at_utc <= resolved_edge_valid_at_utc
        ) or (
            edge_valid_at_utc is not None
            and resolved_edge_invalid_at_utc is not None
            and resolved_edge_invalid_at_utc <= edge_valid_at_utc
        ):
            continue
        
        # ========================================
        # 情况 B: 有冲突 - 新边使旧边失效
        # ========================================
        # 条件: 旧边生效时间 < 新边生效时间
        # 例如: "用户8点去健身" (valid=2023-01-01 08:00)
        #       "用户10点去健身" (valid=2023-06-01 10:00)
        #       → 8:00 < 10:00，旧边需要失效
        #
        # 处理方式:
        # 1. 将旧边的 invalid_at 设置为新边的 valid_at
        #    意思是: 旧习惯在新习惯开始时结束
        # 2. 设置 expired_at 为当前时间
        #    记录什么时候发现这个冲突并处理的
        elif (
            edge_valid_at_utc is not None
            and resolved_edge_valid_at_utc is not None
            and edge_valid_at_utc < resolved_edge_valid_at_utc
        ):
            # 标记旧边失效
            edge.invalid_at = resolved_edge.valid_at  # 失效时间 = 新边生效时间
            edge.expired_at = edge.expired_at if edge.expired_at is not None else utc_now()  # 记录处理时间
            invalidated_edges.append(edge)

    return invalidated_edges


async def resolve_extracted_edge(
    llm_client: LLMClient,
    extracted_edge: EntityEdge,
    related_edges: list[EntityEdge],
    existing_edges: list[EntityEdge],
    episode: EpisodicNode,
    edge_type_candidates: dict[str, type[BaseModel]] | None = None,
    custom_edge_type_names: set[str] | None = None,
) -> tuple[EntityEdge, list[EntityEdge], list[EntityEdge]]:
    """
    解析单条提取的边，处理去重和冲突
    
    主要流程:
    1. 快速精确匹配 - 如果完全相同的边已存在，直接复用
    2. LLM 去重判断 - 判断新边是否与已有边重复
    3. LLM 冲突检测 - 识别矛盾的边
    4. 时间冲突处理 - 标记过时的边为失效
    
    参数说明:
    - extracted_edge: 新提取的边
    - related_edges: 相同节点对之间的已有边（用于去重）
    - existing_edges: 语义相似的边（用于冲突检测）
    
    返回:
    - resolved_edge: 解析后的边（可能是新边或复用的已有边）
    - invalidated_edges: 被标记为失效的边列表
    - duplicate_edges: 识别出的重复边列表
    
    示例场景:
    
    场景 1: 完全重复
    - 新边: "用户在健身房锻炼"
    - 已有边: "用户在健身房锻炼"
    - 结果: 复用已有边，将当前 episode 添加到 episodes 列表
    
    场景 2: 语义重复
    - 新边: "张三在阿里巴巴工作"
    - 已有边: "张三就职于阿里巴巴"
    - 结果: LLM 判断为重复，复用已有边
    
    场景 3: 时间冲突
    - 新边: "用户10点去健身" (valid_at=2023-06-01)
    - 已有边: "用户8点去健身" (valid_at=2023-01-01)
    - 结果: 
      * 复用新边
      * 旧边被标记失效 (invalid_at=2023-06-01, expired_at=now)
    """
    # ========================================
    # 阶段 1: 快速路径 - 精确匹配检查
    # ========================================
    # 如果没有相关边和已有边，直接返回新边
    if len(related_edges) == 0 and len(existing_edges) == 0:
        return extracted_edge, [], []

    # 快速精确匹配: 如果事实描述和节点对完全相同，直接复用已有边
    # 优点: 避免不必要的 LLM 调用，提高性能
    normalized_fact = _normalize_string_exact(extracted_edge.fact)
    for edge in related_edges:
        if (
            edge.source_node_uuid == extracted_edge.source_node_uuid
            and edge.target_node_uuid == extracted_edge.target_node_uuid
            and _normalize_string_exact(edge.fact) == normalized_fact
        ):
            resolved = edge
            # 将当前 episode 添加到边的 episodes 列表
            # 意思是: 这个事实在这个 episode 中被再次提及
            if episode is not None and episode.uuid not in resolved.episodes:
                resolved.episodes.append(episode.uuid)
            return resolved, [], []

    start = time()

    # ========================================
    # 阶段 2: 准备 LLM 上下文
    # ========================================
    # 将边信息转换为 LLM 可理解的格式
    # 为每条边分配索引，方便 LLM 返回引用
    related_edges_context = [{'idx': i, 'fact': edge.fact} for i, edge in enumerate(related_edges)]

    invalidation_edge_candidates_context = [
        {'idx': i, 'fact': existing_edge.fact} for i, existing_edge in enumerate(existing_edges)
    ]

    edge_types_context = (
        [
            {
                'fact_type_name': type_name,
                'fact_type_description': type_model.__doc__,
            }
            for type_name, type_model in edge_type_candidates.items()
        ]
        if edge_type_candidates is not None
        else []
    )

    context = {
        'existing_edges': related_edges_context,  # 用于去重的已有边
        'new_edge': extracted_edge.fact,  # 新提取的边
        'edge_invalidation_candidates': invalidation_edge_candidates_context,  # 可能冲突的边
        'edge_types': edge_types_context,  # 边类型定义
    }

    if related_edges or existing_edges:
        logger.debug(
            'Resolving edge: sent %d EXISTING FACTS%s and %d INVALIDATION CANDIDATES%s',
            len(related_edges),
            f' (idx 0-{len(related_edges) - 1})' if related_edges else '',
            len(existing_edges),
            f' (idx 0-{len(existing_edges) - 1})' if existing_edges else '',
        )

    # ========================================
    # 阶段 3: LLM 去重和冲突判断
    # ========================================
    # 让 LLM 分析新边与已有边的关系:
    # 1. duplicate_facts: 哪些已有边与新边重复? (返回索引列表)
    # 2. contradicted_facts: 哪些已有边与新边矛盾? (返回索引列表)
    # 3. fact_type: 新边应该使用什么类型?
    llm_response = await llm_client.generate_response(
        prompt_library.dedupe_edges.resolve_edge(context),
        response_model=EdgeDuplicate,
        model_size=ModelSize.small,
        prompt_name='dedupe_edges.resolve_edge',
    )
    response_object = EdgeDuplicate(**llm_response)
    duplicate_facts = response_object.duplicate_facts

    # ========================================
    # 阶段 4: 处理重复边
    # ========================================
    # 步骤 4.1: 验证 LLM 返回的索引是否有效
    invalid_duplicates = [i for i in duplicate_facts if i < 0 or i >= len(related_edges)]
    if invalid_duplicates:
        logger.warning(
            'LLM returned invalid duplicate_facts idx values %s (valid range: 0-%d for EXISTING FACTS)',
            invalid_duplicates,
            len(related_edges) - 1,
        )
    
    #【NINS】：核心去重逻辑。使用查到的已有相似边，不创建新边。最相关的相似边的episodic会被替换。
    # 步骤 4.2: 过滤出有效的重复边索引
    duplicate_fact_ids: list[int] = [i for i in duplicate_facts if 0 <= i < len(related_edges)]

    # 步骤 4.3: 如果找到重复，使用已有边替代新边
    resolved_edge = extracted_edge
    for duplicate_fact_id in duplicate_fact_ids:
        resolved_edge = related_edges[duplicate_fact_id]
        break  # 只取第一个重复边

    # 步骤 4.4: 将当前 episode 添加到边的历史记录中
    if duplicate_fact_ids and episode is not None:
        resolved_edge.episodes.append(episode.uuid)

    # ========================================
    # 阶段 5: 处理矛盾边（冲突检测）
    # ========================================
    contradicted_facts: list[int] = response_object.contradicted_facts

    # 步骤 5.1: 验证 LLM 返回的矛盾索引
    invalid_contradictions = [i for i in contradicted_facts if i < 0 or i >= len(existing_edges)]
    if invalid_contradictions:
        logger.warning(
            'LLM returned invalid contradicted_facts idx values %s (valid range: 0-%d for INVALIDATION CANDIDATES)',
            invalid_contradictions,
            len(existing_edges) - 1,
        )

    # 步骤 5.2: 收集需要失效的候选边
    # 这些边与新边存在语义冲突
    # 例如: "用户8点去健身" vs "用户10点去健身"
    invalidation_candidates: list[EntityEdge] = [
        existing_edges[i] for i in contradicted_facts if 0 <= i < len(existing_edges)
    ]

    # ========================================
    # 阶段 6: 处理边类型和属性
    # ========================================
    fact_type: str = response_object.fact_type
    candidate_type_names = set(edge_type_candidates or {})
    custom_type_names = custom_edge_type_names or set()

    is_default_type = fact_type.upper() == 'DEFAULT'
    is_custom_type = fact_type in custom_type_names
    is_allowed_custom_type = fact_type in candidate_type_names

    if is_allowed_custom_type:
        # 情况 A: LLM 选择了允许的自定义边类型
        # 例如: 'WorksFor' 用于 (Person, Company) - 允许
        # 采用自定义类型，并提取结构化属性
        resolved_edge.name = fact_type

        edge_attributes_context = {
            'episode_content': episode.content,
            'reference_time': episode.valid_at,
            'fact': resolved_edge.fact,
        }

        # 如果边类型有自定义属性（如 WorksFor 可能有 position, salary 等）
        # 使用 LLM 从文本中提取这些属性
        edge_model = edge_type_candidates.get(fact_type) if edge_type_candidates else None
        if edge_model is not None and len(edge_model.model_fields) != 0:
            edge_attributes_response = await llm_client.generate_response(
                prompt_library.extract_edges.extract_attributes(edge_attributes_context),
                response_model=edge_model,  # type: ignore
                model_size=ModelSize.small,
                prompt_name='extract_edges.extract_attributes',
            )

            resolved_edge.attributes = edge_attributes_response
    elif not is_default_type and is_custom_type:
        # 情况 B: LLM 选择了自定义类型，但不适用于当前节点对
        # 例如: 'WorksFor' 用于 (Person, Person) - 不允许
        # 回退到默认边标签，清除结构化属性
        resolved_edge.name = DEFAULT_EDGE_NAME
        resolved_edge.attributes = {}
    elif not is_default_type:
        # 情况 C: LLM 生成的非自定义标签
        # 只要不是 DEFAULT 标记，允许通过
        # 例如: LLM 可能生成 'FRIENDS_WITH', 'MENTORS' 等动态标签
        resolved_edge.name = fact_type
        resolved_edge.attributes = {}

    end = time()
    logger.debug(
        f'Resolved Edge: {extracted_edge.name} is {resolved_edge.name}, in {(end - start) * 1000} ms'
    )

    now = utc_now()

    # ========================================
    # 阶段 7: 时间冲突处理
    # ========================================
    # 步骤 7.1: 如果边已经有 invalid_at 但没有 expired_at，设置 expired_at
    if resolved_edge.invalid_at and not resolved_edge.expired_at:
        resolved_edge.expired_at = now

    # 步骤 7.2: 检查新边是否被更新的信息失效
    # 核心场景: 如果冲突候选中有更新的边，新边可能已经过时
    # 例如:
    # - 新边: "用户8点去健身" (valid_at=2023-01-01)
    # - 候选: "用户10点去健身" (valid_at=2023-06-01)
    # - 结果: 新边应该在2023-06-01失效
    if resolved_edge.expired_at is None:
        # 按时间排序候选边（没有时间的排在前面）
        invalidation_candidates.sort(key=lambda c: (c.valid_at is None, ensure_utc(c.valid_at)))
        
        for candidate in invalidation_candidates:
            candidate_valid_at_utc = ensure_utc(candidate.valid_at)
            resolved_edge_valid_at_utc = ensure_utc(resolved_edge.valid_at)
            
            # 如果候选边的生效时间 > 新边的生效时间
            # 说明候选边提供了更新的信息，新边应该失效
            if (
                candidate_valid_at_utc is not None
                and resolved_edge_valid_at_utc is not None
                and candidate_valid_at_utc > resolved_edge_valid_at_utc
            ):
                # 标记新边失效
                resolved_edge.invalid_at = candidate.valid_at  # 在候选边生效时失效
                resolved_edge.expired_at = now  # 记录失效处理时间
                break

    # ========================================
    # 阶段 8: 标记矛盾的旧边为失效
    # ========================================
    # 调用 resolve_edge_contradictions 处理时间冲突
    # 将所有与新边冲突的旧边标记为失效
    # 详细逻辑见 resolve_edge_contradictions 函数的注释
    invalidated_edges: list[EntityEdge] = resolve_edge_contradictions(
        resolved_edge, invalidation_candidates
    )
    
    # 收集重复边列表（用于追踪）
    duplicate_edges: list[EntityEdge] = [related_edges[idx] for idx in duplicate_fact_ids]

    return resolved_edge, invalidated_edges, duplicate_edges


async def filter_existing_duplicate_of_edges(
    driver: GraphDriver, duplicates_node_tuples: list[tuple[EntityNode, EntityNode]]
) -> list[tuple[EntityNode, EntityNode]]:
    if not duplicates_node_tuples:
        return []

    duplicate_nodes_map = {
        (source.uuid, target.uuid): (source, target) for source, target in duplicates_node_tuples
    }

    if driver.provider == GraphProvider.NEPTUNE:
        query: LiteralString = """
            UNWIND $duplicate_node_uuids AS duplicate_tuple
            MATCH (n:Entity {uuid: duplicate_tuple.source})-[r:RELATES_TO {name: 'IS_DUPLICATE_OF'}]->(m:Entity {uuid: duplicate_tuple.target})
            RETURN DISTINCT
                n.uuid AS source_uuid,
                m.uuid AS target_uuid
        """

        duplicate_nodes = [
            {'source': source.uuid, 'target': target.uuid}
            for source, target in duplicates_node_tuples
        ]

        records, _, _ = await driver.execute_query(
            query,
            duplicate_node_uuids=duplicate_nodes,
            routing_='r',
        )
    else:
        if driver.provider == GraphProvider.KUZU:
            query = """
                UNWIND $duplicate_node_uuids AS duplicate
                MATCH (n:Entity {uuid: duplicate.src})-[:RELATES_TO]->(e:RelatesToNode_ {name: 'IS_DUPLICATE_OF'})-[:RELATES_TO]->(m:Entity {uuid: duplicate.dst})
                RETURN DISTINCT
                    n.uuid AS source_uuid,
                    m.uuid AS target_uuid
            """
            duplicate_node_uuids = [{'src': src, 'dst': dst} for src, dst in duplicate_nodes_map]
        else:
            query: LiteralString = """
                UNWIND $duplicate_node_uuids AS duplicate_tuple
                MATCH (n:Entity {uuid: duplicate_tuple[0]})-[r:RELATES_TO {name: 'IS_DUPLICATE_OF'}]->(m:Entity {uuid: duplicate_tuple[1]})
                RETURN DISTINCT
                    n.uuid AS source_uuid,
                    m.uuid AS target_uuid
            """
            duplicate_node_uuids = list(duplicate_nodes_map.keys())

        records, _, _ = await driver.execute_query(
            query,
            duplicate_node_uuids=duplicate_node_uuids,
            routing_='r',
        )

    # Remove duplicates that already have the IS_DUPLICATE_OF edge
    for record in records:
        duplicate_tuple = (record.get('source_uuid'), record.get('target_uuid'))
        if duplicate_nodes_map.get(duplicate_tuple):
            duplicate_nodes_map.pop(duplicate_tuple)

    return list[tuple[EntityNode, EntityNode]](duplicate_nodes_map.values())
