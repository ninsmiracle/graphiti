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
from collections import defaultdict
from time import time

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EMBEDDING_DIM
from graphiti_core.errors import SearchRerankerError
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.helpers import semaphore_gather
from graphiti_core.nodes import CommunityNode, EntityNode, EpisodicNode
from graphiti_core.search.search_config import (
    DEFAULT_SEARCH_LIMIT,
    CommunityReranker,
    CommunitySearchConfig,
    CommunitySearchMethod,
    EdgeReranker,
    EdgeSearchConfig,
    EdgeSearchMethod,
    EpisodeReranker,
    EpisodeSearchConfig,
    NodeReranker,
    NodeSearchConfig,
    NodeSearchMethod,
    SearchConfig,
    SearchResults,
)
from graphiti_core.search.search_filters import SearchFilters
from graphiti_core.search.search_utils import (
    community_fulltext_search,
    community_similarity_search,
    edge_bfs_search,
    edge_fulltext_search,
    edge_similarity_search,
    episode_fulltext_search,
    episode_mentions_reranker,
    get_embeddings_for_communities,
    get_embeddings_for_edges,
    get_embeddings_for_nodes,
    maximal_marginal_relevance,
    node_bfs_search,
    node_distance_reranker,
    node_fulltext_search,
    node_similarity_search,
    rrf,
)

logger = logging.getLogger(__name__)


async def search(
    clients: GraphitiClients,
    query: str,
    group_ids: list[str] | None,
    config: SearchConfig,
    search_filter: SearchFilters,
    center_node_uuid: str | None = None,
    bfs_origin_node_uuids: list[str] | None = None,
    query_vector: list[float] | None = None,
    driver: GraphDriver | None = None,
) -> SearchResults:
    """
    Graphiti 核心搜索函数 - Recall（读流程）的主入口
    
    【整体流程概述】
    1. 准备阶段：生成查询向量（embedding）
    2. 并行搜索：同时搜索 edges、nodes、episodes、communities 四种图元素
    3. 重排序：对每种元素的搜索结果进行reranking
    4. 返回结果：整合所有搜索结果
    
    【参数说明】
    - clients: 包含driver、embedder、cross_encoder的客户端集合
    - query: 用户的查询文本
    - config: 搜索配置，决定使用哪些搜索方法和重排序策略
    - search_filter: 搜索过滤器，用于时间范围、标签等过滤
    - center_node_uuid: 中心节点UUID，用于node_distance重排序
    - bfs_origin_node_uuids: BFS起始节点，用于图遍历搜索
    
    【LLM Token消耗】
    在标准的COMBINED_HYBRID_SEARCH_RRF配置下：
    - 如果需要向量搜索：调用1次embedding API（约50-100 tokens，取决于query长度）
    - 不调用LLM生成模型（GPT-4/Claude等），因为RRF重排序是纯算法
    - Cross-encoder reranker会调用额外的ranking API（但RRF不使用）
    """
    start = time()

    # 步骤1: 初始化客户端
    driver = driver or clients.driver
    embedder = clients.embedder
    cross_encoder = clients.cross_encoder

    # 步骤2: 空查询直接返回
    if query.strip() == '':
        return SearchResults()

    # 步骤3: 判断是否需要生成查询向量
    # 如果配置中包含余弦相似度搜索或MMR重排序，需要生成embedding
    # 【Token消耗点1】：此处调用embedder.create()会消耗token
    # 典型消耗：对于"What is the weather like?"这样的查询，约消耗10-20 tokens
    if (
        config.edge_config
        and EdgeSearchMethod.cosine_similarity in config.edge_config.search_methods
        or config.edge_config
        and EdgeReranker.mmr == config.edge_config.reranker
        or config.node_config
        and NodeSearchMethod.cosine_similarity in config.node_config.search_methods
        or config.node_config
        and NodeReranker.mmr == config.node_config.reranker
        or (
            config.community_config
            and CommunitySearchMethod.cosine_similarity in config.community_config.search_methods
        )
        or (config.community_config and CommunityReranker.mmr == config.community_config.reranker)
    ):
        search_vector = (
            query_vector
            if query_vector is not None
            else await embedder.create(input_data=[query.replace('\n', ' ')])
        )
    else:
        # 如果不需要向量搜索，使用零向量占位
        search_vector = [0.0] * EMBEDDING_DIM

    # 步骤4: 处理group_ids
    # if group_ids is empty, set it to None
    group_ids = group_ids if group_ids and group_ids != [''] else None
    
    # 步骤5: 并行执行四种搜索（Graphiti的核心优化）
    # 使用semaphore_gather确保并发控制，避免过载数据库
    # 每个搜索函数内部会根据config执行不同的搜索策略
    (
        (edges, edge_reranker_scores),
        (nodes, node_reranker_scores),
        (episodes, episode_reranker_scores),
        (communities, community_reranker_scores),
    ) = await semaphore_gather(
        # 5.1 边（关系）搜索
        edge_search(
            driver,
            cross_encoder,
            query,
            search_vector,
            group_ids,
            config.edge_config,
            search_filter,
            center_node_uuid,
            bfs_origin_node_uuids,
            config.limit,
            config.reranker_min_score,
        ),
        # 5.2 节点（实体）搜索
        node_search(
            driver,
            cross_encoder,
            query,
            search_vector,
            group_ids,
            config.node_config,
            search_filter,
            center_node_uuid,
            bfs_origin_node_uuids,
            config.limit,
            config.reranker_min_score,
        ),
        # 5.3 情节（原始内容片段）搜索
        episode_search(
            driver,
            cross_encoder,
            query,
            search_vector,
            group_ids,
            config.episode_config,
            search_filter,
            config.limit,
            config.reranker_min_score,
        ),
        # 5.4 社区（实体聚类）搜索
        community_search(
            driver,
            cross_encoder,
            query,
            search_vector,
            group_ids,
            config.community_config,
            config.limit,
            config.reranker_min_score,
        ),
    )

    # 步骤6: 构建最终结果
    results = SearchResults(
        edges=edges,
        edge_reranker_scores=edge_reranker_scores,
        nodes=nodes,
        node_reranker_scores=node_reranker_scores,
        episodes=episodes,
        episode_reranker_scores=episode_reranker_scores,
        communities=communities,
        community_reranker_scores=community_reranker_scores,
    )

    latency = (time() - start) * 1000

    logger.debug(f'search returned context for query {query} in {latency} ms')

    return results


async def edge_search(
    driver: GraphDriver,
    cross_encoder: CrossEncoderClient,
    query: str,
    query_vector: list[float],
    group_ids: list[str] | None,
    config: EdgeSearchConfig | None,
    search_filter: SearchFilters,
    center_node_uuid: str | None = None,
    bfs_origin_node_uuids: list[str] | None = None,
    limit=DEFAULT_SEARCH_LIMIT,
    reranker_min_score: float = 0,
) -> tuple[list[EntityEdge], list[float]]:
    """
    边（关系）搜索函数 - COMBINED_HYBRID_SEARCH_RRF的边搜索实现
    
    【混合搜索策略】
    在COMBINED_HYBRID_SEARCH_RRF配置下，该函数执行：
    1. BM25全文搜索（基于关系的fact文本）
    2. 余弦相似度向量搜索（基于fact的embedding）
    3. RRF重排序融合两种搜索结果
    
    【为什么使用混合搜索？】
    - BM25擅长：精确关键词匹配、稀有词检索
    - 向量搜索擅长：语义理解、同义词检索、跨语言
    - 结合两者可以覆盖更全面的检索场景
    
    【搜索流程】
    阶段1: 并行执行多种搜索方法
    阶段2: 融合并去重结果
    阶段3: 使用reranker重新排序
    阶段4: 返回top-k结果
    """
    if config is None:
        return [], []

    # 阶段1: 构建搜索任务列表
    # 注意：每个方法返回2*limit个结果，为后续重排序提供更多候选
    search_tasks = []
    
    # 1.1 BM25全文搜索
    # 在Neo4j中使用FULLTEXT INDEX，基于Lucene的BM25算法
    # 优点：快速、精确匹配、支持复杂查询语法
    if EdgeSearchMethod.bm25 in config.search_methods:
        search_tasks.append(
            edge_fulltext_search(driver, query, search_filter, group_ids, 2 * limit)
        )
    
    # 1.2 余弦相似度向量搜索
    # 使用预先计算的fact_embedding与query_vector计算相似度
    # 优点：语义理解、模糊匹配、跨语言支持
    if EdgeSearchMethod.cosine_similarity in config.search_methods:
        search_tasks.append(
            edge_similarity_search(
                driver,
                query_vector,
                None,
                None,
                search_filter,
                group_ids,
                2 * limit,
                config.sim_min_score,
            )
        )
    
    # 1.3 广度优先搜索（BFS）
    # 从指定节点开始，遍历图结构找到相关边
    # 优点：发现隐式关联、探索邻近关系
    if EdgeSearchMethod.bfs in config.search_methods:
        search_tasks.append(
            edge_bfs_search(
                driver,
                bfs_origin_node_uuids,
                config.bfs_max_depth,
                search_filter,
                group_ids,
                2 * limit,
            )
        )

    # 阶段2: 并行执行所有搜索任务
    # 使用semaphore_gather控制并发，避免数据库过载
    search_results: list[list[EntityEdge]] = []
    if search_tasks:
        search_results = list(await semaphore_gather(*search_tasks))

    # 2.1 如果启用BFS但未指定起始节点，使用初步搜索结果的节点作为起点
    # 这是一种"查询扩展"策略，可以发现更多相关边
    if EdgeSearchMethod.bfs in config.search_methods and bfs_origin_node_uuids is None:
        source_node_uuids = [edge.source_node_uuid for result in search_results for edge in result]
        search_results.append(
            await edge_bfs_search(
                driver,
                source_node_uuids,
                config.bfs_max_depth,
                search_filter,
                group_ids,
                2 * limit,
            )
        )

    # 2.2 构建UUID到Edge对象的映射，用于去重和后续检索
    edge_uuid_map = {edge.uuid: edge for result in search_results for edge in result}

    # 阶段3: 重排序（Reranking）
    # 这是COMBINED_HYBRID_SEARCH_RRF的核心步骤
    reranked_uuids: list[str] = []
    edge_scores: list[float] = []
    
    # 3.1 RRF重排序（COMBINED_HYBRID_SEARCH_RRF的默认选择）
    # RRF = Reciprocal Rank Fusion，倒数排名融合
    if config.reranker == EdgeReranker.rrf or config.reranker == EdgeReranker.episode_mentions:
        search_result_uuids = [[edge.uuid for edge in result] for result in search_results]
        # 调用rrf函数进行排序融合（见下方rrf函数的详细注释）
        reranked_uuids, edge_scores = rrf(search_result_uuids, min_score=reranker_min_score)
    
    # 3.2 MMR重排序（Maximal Marginal Relevance）
    # 平衡相关性和多样性，避免返回过于相似的结果
    elif config.reranker == EdgeReranker.mmr:
        search_result_uuids_and_vectors = await get_embeddings_for_edges(
            driver, list(edge_uuid_map.values())
        )
        reranked_uuids, edge_scores = maximal_marginal_relevance(
            query_vector,
            search_result_uuids_and_vectors,
            config.mmr_lambda,
            reranker_min_score,
        )
    
    # 3.3 Cross-Encoder重排序
    # 使用深度学习模型对query-fact对进行精确评分
    # 优点：最准确，缺点：最慢、消耗token
    elif config.reranker == EdgeReranker.cross_encoder:
        fact_to_uuid_map = {edge.fact: edge.uuid for edge in list(edge_uuid_map.values())[:limit]}
        reranked_facts = await cross_encoder.rank(query, list(fact_to_uuid_map.keys()))
        reranked_uuids = [
            fact_to_uuid_map[fact] for fact, score in reranked_facts if score >= reranker_min_score
        ]
        edge_scores = [score for _, score in reranked_facts if score >= reranker_min_score]
    
    # 3.4 节点距离重排序
    # 基于图结构，优先返回离中心节点更近的边
    elif config.reranker == EdgeReranker.node_distance:
        if center_node_uuid is None:
            raise SearchRerankerError('No center node provided for Node Distance reranker')

        # use rrf as a preliminary sort
        sorted_result_uuids, node_scores = rrf(
            [[edge.uuid for edge in result] for result in search_results],
            min_score=reranker_min_score,
        )
        sorted_results = [edge_uuid_map[uuid] for uuid in sorted_result_uuids]

        # node distance reranking
        source_to_edge_uuid_map = defaultdict(list)
        for edge in sorted_results:
            source_to_edge_uuid_map[edge.source_node_uuid].append(edge.uuid)

        source_uuids = [source_node_uuid for source_node_uuid in source_to_edge_uuid_map]

        reranked_node_uuids, edge_scores = await node_distance_reranker(
            driver, source_uuids, center_node_uuid, min_score=reranker_min_score
        )

        for node_uuid in reranked_node_uuids:
            reranked_uuids.extend(source_to_edge_uuid_map[node_uuid])

    # 阶段4: 构建最终结果
    reranked_edges = [edge_uuid_map[uuid] for uuid in reranked_uuids]

    # 4.1 如果使用episode_mentions重排序，额外按提及次数排序
    if config.reranker == EdgeReranker.episode_mentions:
        reranked_edges.sort(reverse=True, key=lambda edge: len(edge.episodes))

    # 4.2 返回top-k结果和对应的分数
    return reranked_edges[:limit], edge_scores[:limit]


async def node_search(
    driver: GraphDriver,
    cross_encoder: CrossEncoderClient,
    query: str,
    query_vector: list[float],
    group_ids: list[str] | None,
    config: NodeSearchConfig | None,
    search_filter: SearchFilters,
    center_node_uuid: str | None = None,
    bfs_origin_node_uuids: list[str] | None = None,
    limit=DEFAULT_SEARCH_LIMIT,
    reranker_min_score: float = 0,
) -> tuple[list[EntityNode], list[float]]:
    """
    节点（实体）搜索函数 - 与edge_search类似的混合搜索策略
    
    【节点搜索的特点】
    - 节点代表图中的实体（人、地点、事物等）
    - 搜索基于节点的name和summary属性
    - 使用name_embedding进行向量搜索
    
    【COMBINED_HYBRID_SEARCH_RRF中的节点搜索】
    1. BM25全文搜索：基于node.name和node.summary
    2. 余弦相似度搜索：基于node.name_embedding
    3. RRF融合结果
    
    【与edge_search的区别】
    - edge搜索关注"关系"（Alice knows Bob）
    - node搜索关注"实体"（Alice是谁）
    - 两者互补，共同构建完整的知识图谱检索
    """
    if config is None:
        return [], []

    # 阶段1: 构建节点搜索任务
    search_tasks = []
    
    # 1.1 BM25全文搜索：在节点名称和摘要中查找关键词
    if NodeSearchMethod.bm25 in config.search_methods:
        search_tasks.append(
            node_fulltext_search(driver, query, search_filter, group_ids, 2 * limit)
        )
    
    # 1.2 余弦相似度搜索：基于name_embedding的语义匹配
    if NodeSearchMethod.cosine_similarity in config.search_methods:
        search_tasks.append(
            node_similarity_search(
                driver,
                query_vector,
                search_filter,
                group_ids,
                2 * limit,
                config.sim_min_score,
            )
        )
    
    # 1.3 BFS图遍历：探索相邻节点
    if NodeSearchMethod.bfs in config.search_methods:
        search_tasks.append(
            node_bfs_search(
                driver,
                bfs_origin_node_uuids,
                search_filter,
                config.bfs_max_depth,
                group_ids,
                2 * limit,
            )
        )

    # 阶段2: 并行执行搜索
    search_results: list[list[EntityNode]] = []
    if search_tasks:
        search_results = list(await semaphore_gather(*search_tasks))

    # 2.1 查询扩展：使用初步结果作为BFS起点
    if NodeSearchMethod.bfs in config.search_methods and bfs_origin_node_uuids is None:
        origin_node_uuids = [node.uuid for result in search_results for node in result]
        search_results.append(
            await node_bfs_search(
                driver,
                origin_node_uuids,
                search_filter,
                config.bfs_max_depth,
                group_ids,
                2 * limit,
            )
        )

    # 2.2 构建UUID映射
    search_result_uuids = [[node.uuid for node in result] for result in search_results]
    node_uuid_map = {node.uuid: node for result in search_results for node in result}

    # 阶段3: 重排序
    reranked_uuids: list[str] = []
    node_scores: list[float] = []
    
    # 3.1 RRF重排序（COMBINED_HYBRID_SEARCH_RRF的默认选择）
    if config.reranker == NodeReranker.rrf:
        reranked_uuids, node_scores = rrf(search_result_uuids, min_score=reranker_min_score)
    
    # 3.2 MMR重排序：平衡相关性和多样性
    elif config.reranker == NodeReranker.mmr:
        search_result_uuids_and_vectors = await get_embeddings_for_nodes(
            driver, list(node_uuid_map.values())
        )

        reranked_uuids, node_scores = maximal_marginal_relevance(
            query_vector,
            search_result_uuids_and_vectors,
            config.mmr_lambda,
            reranker_min_score,
        )
    
    # 3.3 Cross-Encoder重排序：使用深度模型精确评分
    elif config.reranker == NodeReranker.cross_encoder:
        name_to_uuid_map = {node.name: node.uuid for node in list(node_uuid_map.values())}

        reranked_node_names = await cross_encoder.rank(query, list(name_to_uuid_map.keys()))
        reranked_uuids = [
            name_to_uuid_map[name]
            for name, score in reranked_node_names
            if score >= reranker_min_score
        ]
        node_scores = [score for _, score in reranked_node_names if score >= reranker_min_score]
    
    # 3.4 Episode提及次数重排序：优先返回被多次提及的节点
    elif config.reranker == NodeReranker.episode_mentions:
        reranked_uuids, node_scores = await episode_mentions_reranker(
            driver, search_result_uuids, min_score=reranker_min_score
        )
    
    # 3.5 节点距离重排序：优先返回离中心节点近的节点
    elif config.reranker == NodeReranker.node_distance:
        if center_node_uuid is None:
            raise SearchRerankerError('No center node provided for Node Distance reranker')
        reranked_uuids, node_scores = await node_distance_reranker(
            driver,
            rrf(search_result_uuids, min_score=reranker_min_score)[0],
            center_node_uuid,
            min_score=reranker_min_score,
        )

    # 阶段4: 构建并返回最终结果
    reranked_nodes = [node_uuid_map[uuid] for uuid in reranked_uuids]

    return reranked_nodes[:limit], node_scores[:limit]


async def episode_search(
    driver: GraphDriver,
    cross_encoder: CrossEncoderClient,
    query: str,
    _query_vector: list[float],
    group_ids: list[str] | None,
    config: EpisodeSearchConfig | None,
    search_filter: SearchFilters,
    limit=DEFAULT_SEARCH_LIMIT,
    reranker_min_score: float = 0,
) -> tuple[list[EpisodicNode], list[float]]:
    """
    情节（Episode）搜索函数 - 搜索原始文本内容
    
    【Episode是什么？】
    - Episode代表原始输入的文本片段（如对话、文档段落）
    - 是知识图谱的"源数据"，nodes和edges都是从episodes中提取的
    - 包含完整的上下文信息
    
    【搜索策略】
    COMBINED_HYBRID_SEARCH_RRF中的episode搜索：
    1. 仅使用BM25全文搜索（不使用向量搜索）
    2. 原因：episode.content通常较长，全文搜索更有效
    3. RRF重排序（虽然只有一个搜索方法，但保持一致性）
    
    【应用场景】
    当需要查看原始上下文时使用episode搜索：
    - "这句话是在什么场景下说的？"
    - "找出包含某个关键词的对话"
    - "提供完整的原文引用"
    """
    if config is None:
        return [], []
    
    # 阶段1: 执行BM25全文搜索
    # 注意：episode搜索通常只使用BM25，因为：
    # 1. episode.content较长，embedding效果不如短文本
    # 2. 用户通常需要精确的关键词匹配来定位原文
    search_results: list[list[EpisodicNode]] = list(
        await semaphore_gather(
            *[
                episode_fulltext_search(driver, query, search_filter, group_ids, 2 * limit),
            ]
        )
    )

    # 阶段2: 构建UUID映射
    search_result_uuids = [[episode.uuid for episode in result] for result in search_results]
    episode_uuid_map = {episode.uuid: episode for result in search_results for episode in result}

    # 阶段3: 重排序
    reranked_uuids: list[str] = []
    episode_scores: list[float] = []
    
    # 3.1 RRF重排序（默认）
    if config.reranker == EpisodeReranker.rrf:
        reranked_uuids, episode_scores = rrf(search_result_uuids, min_score=reranker_min_score)

    # 3.2 Cross-Encoder重排序
    # 先用RRF初步排序，再用cross-encoder精排前k个结果
    # 这种两阶段策略平衡了准确性和效率
    elif config.reranker == EpisodeReranker.cross_encoder:
        # use rrf as a preliminary reranker
        rrf_result_uuids, episode_scores = rrf(search_result_uuids, min_score=reranker_min_score)
        rrf_results = [episode_uuid_map[uuid] for uuid in rrf_result_uuids][:limit]

        content_to_uuid_map = {episode.content: episode.uuid for episode in rrf_results}

        reranked_contents = await cross_encoder.rank(query, list(content_to_uuid_map.keys()))
        reranked_uuids = [
            content_to_uuid_map[content]
            for content, score in reranked_contents
            if score >= reranker_min_score
        ]
        episode_scores = [score for _, score in reranked_contents if score >= reranker_min_score]

    # 阶段4: 构建并返回最终结果
    reranked_episodes = [episode_uuid_map[uuid] for uuid in reranked_uuids]

    return reranked_episodes[:limit], episode_scores[:limit]


async def community_search(
    driver: GraphDriver,
    cross_encoder: CrossEncoderClient,
    query: str,
    query_vector: list[float],
    group_ids: list[str] | None,
    config: CommunitySearchConfig | None,
    limit=DEFAULT_SEARCH_LIMIT,
    reranker_min_score: float = 0,
) -> tuple[list[CommunityNode], list[float]]:
    """
    社区（Community）搜索函数 - 搜索实体聚类
    
    【Community是什么？】
    - Community是图中紧密关联的节点（实体）的聚类
    - 使用Leiden算法或类似的社区发现算法生成
    - 每个community有一个summary描述整个社区的主题
    - 类似于"主题聚类"或"知识模块"
    
    【为什么需要Community搜索？】
    1. 提供高层次的概览：快速了解图中有哪些主题
    2. 减少检索范围：先找到相关社区，再细查节点和边
    3. 发现隐含关系：社区内的节点通常有共同特征
    
    【COMBINED_HYBRID_SEARCH_RRF中的社区搜索】
    1. BM25全文搜索：基于community.name和community.summary
    2. 余弦相似度搜索：基于community.name_embedding
    3. RRF融合结果
    
    【应用场景】
    - "这个知识图谱包含哪些主要话题？"
    - "与机器学习相关的实体群组"
    - "找出所有与金融相关的知识模块"
    """
    if config is None:
        return [], []

    # 阶段1: 并行执行BM25和向量搜索
    # Community搜索总是同时使用两种方法，因为：
    # 1. community数量通常较少（相比nodes/edges），两种搜索成本可控
    # 2. community.summary包含丰富语义，向量搜索效果好
    # 3. community.name可能包含关键词，BM25也有效
    search_results: list[list[CommunityNode]] = list(
        await semaphore_gather(
            *[
                # 1.1 BM25全文搜索
                community_fulltext_search(driver, query, group_ids, 2 * limit),
                # 1.2 余弦相似度向量搜索
                community_similarity_search(
                    driver, query_vector, group_ids, 2 * limit, config.sim_min_score
                ),
            ]
        )
    )

    # 阶段2: 构建UUID映射
    search_result_uuids = [[community.uuid for community in result] for result in search_results]
    community_uuid_map = {
        community.uuid: community for result in search_results for community in result
    }

    # 阶段3: 重排序
    reranked_uuids: list[str] = []
    community_scores: list[float] = []
    
    # 3.1 RRF重排序（COMBINED_HYBRID_SEARCH_RRF的默认选择）
    if config.reranker == CommunityReranker.rrf:
        reranked_uuids, community_scores = rrf(search_result_uuids, min_score=reranker_min_score)
    
    # 3.2 MMR重排序：确保返回不同主题的社区
    elif config.reranker == CommunityReranker.mmr:
        search_result_uuids_and_vectors = await get_embeddings_for_communities(
            driver, list(community_uuid_map.values())
        )

        reranked_uuids, community_scores = maximal_marginal_relevance(
            query_vector, search_result_uuids_and_vectors, config.mmr_lambda, reranker_min_score
        )
    
    # 3.3 Cross-Encoder重排序：精确评估query与community的相关性
    elif config.reranker == CommunityReranker.cross_encoder:
        name_to_uuid_map = {node.name: node.uuid for result in search_results for node in result}
        reranked_nodes = await cross_encoder.rank(query, list(name_to_uuid_map.keys()))
        reranked_uuids = [
            name_to_uuid_map[name] for name, score in reranked_nodes if score >= reranker_min_score
        ]
        community_scores = [score for _, score in reranked_nodes if score >= reranker_min_score]

    # 阶段4: 构建并返回最终结果
    reranked_communities = [community_uuid_map[uuid] for uuid in reranked_uuids]

    return reranked_communities[:limit], community_scores[:limit]
