"""
社区操作模块 (Community Operations Module)

本模块负责图数据库中社区节点的检测、构建和维护。
核心功能包括：
1. 使用标签传播算法进行社区检测
2. 基于LLM的社区摘要生成
3. 社区节点的增量更新和维护
"""

import asyncio
import logging
from collections import defaultdict

from pydantic import BaseModel

from graphiti_core.driver.driver import GraphDriver, GraphProvider
from graphiti_core.edges import CommunityEdge
from graphiti_core.embedder import EmbedderClient
from graphiti_core.helpers import semaphore_gather
from graphiti_core.llm_client import LLMClient
from graphiti_core.models.nodes.node_db_queries import COMMUNITY_NODE_RETURN
from graphiti_core.nodes import CommunityNode, EntityNode, get_community_node_from_record
from graphiti_core.prompts import prompt_library
from graphiti_core.prompts.summarize_nodes import Summary, SummaryDescription
from graphiti_core.utils.datetime_utils import utc_now
from graphiti_core.utils.maintenance.edge_operations import build_community_edges

# 最大并发社区构建数量，用于控制LLM调用的并发量
MAX_COMMUNITY_BUILD_CONCURRENCY = 10

logger = logging.getLogger(__name__)


class Neighbor(BaseModel):
    """邻居节点模型
    
    用于标签传播算法中表示节点的邻居关系
    """
    node_uuid: str      # 邻居节点的UUID
    edge_count: int     # 与该邻居之间的边数量（边的权重）


async def get_community_clusters(
    driver: GraphDriver, group_ids: list[str] | None
) -> list[list[EntityNode]]:
    """获取社区集群
    
    使用标签传播算法对实体节点进行社区检测，返回检测到的社区集群列表。
    
    流程说明：
    1. 如果未指定group_ids，则查询所有存在group_id的实体节点
    2. 对每个group_id分别进行社区检测：
       a. 获取该group下的所有实体节点
       b. 为每个节点构建邻居投影图（包含邻居UUID和边权重）
       c. 使用标签传播算法进行社区检测
       d. 根据检测结果获取完整的实体节点对象
    
    Args:
        driver: 图数据库驱动
        group_ids: 要处理的group_id列表，None表示处理所有group
        
    Returns:
        社区集群列表，每个集群是一组属于同一社区的EntityNode
    """
    community_clusters: list[list[EntityNode]] = []

    # 步骤1: 如果未指定group_ids，从数据库查询所有group_id
    if group_ids is None:
        group_id_values, _, _ = await driver.execute_query(
            """
            MATCH (n:Entity)
            WHERE n.group_id IS NOT NULL
            RETURN
                collect(DISTINCT n.group_id) AS group_ids
            """
        )

        group_ids = group_id_values[0]['group_ids'] if group_id_values else []

    # 步骤2: 对每个group_id进行社区检测
    for group_id in group_ids:
        # 构建邻居投影图: key=节点UUID, value=该节点的邻居列表
        projection: dict[str, list[Neighbor]] = {}
        
        # 获取该group下的所有实体节点
        nodes = await EntityNode.get_by_group_ids(driver, [group_id])
        
        # 步骤2a: 为每个节点查询其邻居关系
        for node in nodes:
            # Neo4j查询：直接匹配RELATES_TO关系
            match_query = """
                MATCH (n:Entity {group_id: $group_id, uuid: $uuid})-[e:RELATES_TO]-(m: Entity {group_id: $group_id})
            """
            # Kuzu需要特殊处理：RELATES_TO是一个节点而非边
            if driver.provider == GraphProvider.KUZU:
                match_query = """
                MATCH (n:Entity {group_id: $group_id, uuid: $uuid})-[:RELATES_TO]-(e:RelatesToNode_)-[:RELATES_TO]-(m: Entity {group_id: $group_id})
                """
            
            # 查询邻居节点及其边的数量（权重）
            records, _, _ = await driver.execute_query(
                match_query
                + """
                WITH count(e) AS count, m.uuid AS uuid
                RETURN
                    uuid,
                    count
                """,
                uuid=node.uuid,
                group_id=group_id,
            )

            # 构建该节点的邻居列表
            projection[node.uuid] = [
                Neighbor(node_uuid=record['uuid'], edge_count=record['count']) for record in records
            ]

        # 步骤2b: 使用标签传播算法检测社区
        cluster_uuids = label_propagation(projection)

        # 步骤2c: 根据UUID列表并发获取完整的实体节点对象
        community_clusters.extend(
            list(
                await semaphore_gather(
                    *[EntityNode.get_by_uuids(driver, cluster) for cluster in cluster_uuids]
                )
            )
        )

    return community_clusters


def label_propagation(projection: dict[str, list[Neighbor]]) -> list[list[str]]:
    """标签传播社区检测算法 (Label Propagation Algorithm)
    
    这是一个经典的图社区检测算法，通过迭代传播的方式将相似的节点聚类到同一社区。
    
    算法原理：
    1. 初始化：每个节点被分配一个唯一的社区标签（使用序号作为初始标签）
    2. 迭代传播：
       - 每个节点统计其邻居所属社区的权重（边数量）
       - 节点选择邻居中权重最大的社区作为自己的新社区
       - 如果有多个社区权重相同，选择标签值较大的社区（打破平衡）
       - 如果候选社区权重<=1，保持当前社区或选择较大标签
    3. 收敛：当一轮迭代中所有节点的社区都不再变化时，算法结束
    4. 输出：返回检测到的社区集群列表
    
    Args:
        projection: 节点邻居投影图，格式为 {node_uuid: [Neighbor列表]}
        
    Returns:
        社区集群列表，每个集群是一组节点UUID的列表
    """
    # 步骤1: 初始化社区标签，每个节点用其序号作为初始社区ID
    community_map = {uuid: i for i, uuid in enumerate(projection.keys())}

    # 步骤2: 迭代传播直到收敛
    while True:
        no_change = True  # 用于检测是否收敛
        new_community_map: dict[str, int] = {}

        # 遍历每个节点，计算其新的社区归属
        for uuid, neighbors in projection.items():
            curr_community = community_map[uuid]

            # 统计邻居所属各个社区的权重（边数量之和）
            community_candidates: dict[int, int] = defaultdict(int)
            for neighbor in neighbors:
                community_candidates[community_map[neighbor.node_uuid]] += neighbor.edge_count
            
            # 将社区候选按权重排序（降序）
            community_lst = [
                (count, community) for community, count in community_candidates.items()
            ]
            community_lst.sort(reverse=True)
            
            # 选择权重最高的社区
            candidate_rank, community_candidate = community_lst[0] if community_lst else (0, -1)
            
            # 决策逻辑：
            # - 如果候选社区有效且权重>1，则采用该社区
            # - 否则，在候选社区和当前社区中选择标签较大的（打破平衡）
            if community_candidate != -1 and candidate_rank > 1:
                new_community = community_candidate
            else:
                new_community = max(community_candidate, curr_community)

            new_community_map[uuid] = new_community

            # 检测是否有变化
            if new_community != curr_community:
                no_change = False

        # 如果本轮没有任何节点改变社区，则算法收敛
        if no_change:
            break

        # 更新社区映射，进入下一轮迭代
        community_map = new_community_map

    # 步骤3: 将社区映射转换为集群列表
    # 相同社区ID的节点被分组到同一个集群
    community_cluster_map = defaultdict(list)
    for uuid, community in community_map.items():
        community_cluster_map[community].append(uuid)

    clusters = [cluster for cluster in community_cluster_map.values()]
    return clusters


async def summarize_pair(llm_client: LLMClient, summary_pair: tuple[str, str]) -> str:
    """汇总一对节点摘要
    
    使用LLM将两个节点的摘要合并为一个综合摘要。
    这是构建社区摘要的基础操作，通过两两合并的方式最终得到整个社区的摘要。
    
    Args:
        llm_client: LLM客户端，用于生成摘要
        summary_pair: 包含两个摘要文本的元组
        
    Returns:
        合并后的摘要文本
    """
    # 准备LLM上下文：将两个摘要转换为列表格式
    context = {
        'node_summaries': [{'summary': summary} for summary in summary_pair],
    }

    # 调用LLM生成合并后的摘要
    llm_response = await llm_client.generate_response(
        prompt_library.summarize_nodes.summarize_pair(context),
        response_model=Summary,
        prompt_name='summarize_nodes.summarize_pair',
    )

    pair_summary = llm_response.get('summary', '')

    return pair_summary


async def generate_summary_description(llm_client: LLMClient, summary: str) -> str:
    """生成摘要的简短描述（社区名称）
    
    使用LLM从详细摘要中提取一个简短的描述性名称。
    这个名称将作为社区节点的name属性。
    
    Args:
        llm_client: LLM客户端
        summary: 详细的社区摘要文本
        
    Returns:
        简短的描述性名称
    """
    context = {
        'summary': summary,
    }

    # 调用LLM生成简短描述
    llm_response = await llm_client.generate_response(
        prompt_library.summarize_nodes.summary_description(context),
        response_model=SummaryDescription,
        prompt_name='summarize_nodes.summary_description',
    )

    description = llm_response.get('description', '')

    return description


async def build_community(
    llm_client: LLMClient, community_cluster: list[EntityNode]
) -> tuple[CommunityNode, list[CommunityEdge]]:
    """构建单个社区节点
    
    从一个实体节点集群构建对应的社区节点。使用分治法（二叉树归并）的方式
    将所有实体的摘要合并为一个社区摘要。
    
    算法流程：
    1. 提取所有实体节点的摘要
    2. 使用二叉树归并方式合并摘要：
       - 如果摘要数量为奇数，暂存一个
       - 将摘要两两配对，并发调用LLM合并
       - 将暂存的摘要加回
       - 重复此过程直到只剩一个摘要
    3. 基于最终摘要生成社区名称
    4. 创建社区节点和社区边
    
    时间复杂度: O(log n)轮，每轮并发处理n/2对
    
    Args:
        llm_client: LLM客户端
        community_cluster: 属于该社区的实体节点列表
        
    Returns:
        (社区节点, 社区边列表)的元组
    """
    # 步骤1: 提取所有实体节点的摘要
    summaries = [entity.summary for entity in community_cluster]
    length = len(summaries)
    
    # 步骤2: 使用二叉树归并方式合并摘要
    while length > 1:
        odd_one_out: str | None = None
        
        # 如果摘要数量为奇数，暂存最后一个
        if length % 2 == 1:
            odd_one_out = summaries.pop()
            length -= 1
        
        # 将摘要分成左右两半，两两配对并发合并
        # 例如: [s1,s2,s3,s4] -> [(s1,s3), (s2,s4)] -> 并发合并
        new_summaries: list[str] = list(
            await semaphore_gather(
                *[
                    summarize_pair(llm_client, (str(left_summary), str(right_summary)))
                    for left_summary, right_summary in zip(
                        summaries[: int(length / 2)],  # 左半部分
                        summaries[int(length / 2) :],  # 右半部分
                        strict=False
                    )
                ]
            )
        )
        
        # 将暂存的摘要加回到新摘要列表
        if odd_one_out is not None:
            new_summaries.append(odd_one_out)
        
        # 更新摘要列表和长度，进入下一轮合并
        summaries = new_summaries
        length = len(summaries)

    # 步骤3: 最终只剩一个摘要，这就是社区摘要
    summary = summaries[0]
    
    # 步骤4: 基于摘要生成社区名称
    name = await generate_summary_description(llm_client, summary)
    
    # 步骤5: 创建社区节点对象
    now = utc_now()
    community_node = CommunityNode(
        name=name,
        group_id=community_cluster[0].group_id,
        labels=['Community'],
        created_at=now,
        summary=summary,
    )
    
    # 步骤6: 创建从社区到各个实体的HAS_MEMBER边
    community_edges = build_community_edges(community_cluster, community_node, now)

    logger.debug((community_node, community_edges))

    return community_node, community_edges


async def build_communities(
    driver: GraphDriver,
    llm_client: LLMClient,
    group_ids: list[str] | None,
) -> tuple[list[CommunityNode], list[CommunityEdge]]:
    """批量构建社区节点（全量重建）
    
    这是社区构建的主入口函数，用于从头开始构建所有社区。
    
    流程说明：
    1. 使用标签传播算法检测社区集群
    2. 为每个集群并发构建社区节点（受信号量限制并发数）
    3. 收集所有构建的社区节点和边
    
    注意：这是一个全量重建操作，通常在初始化或大规模重构时使用。
    日常维护应使用 update_community() 进行增量更新。
    
    Args:
        driver: 图数据库驱动
        llm_client: LLM客户端
        group_ids: 要处理的group_id列表，None表示处理所有group
        
    Returns:
        (社区节点列表, 社区边列表)的元组
    """
    # 步骤1: 检测社区集群
    community_clusters = await get_community_clusters(driver, group_ids)

    # 步骤2: 创建信号量限制并发构建数量，避免LLM调用过载
    semaphore = asyncio.Semaphore(MAX_COMMUNITY_BUILD_CONCURRENCY)

    async def limited_build_community(cluster):
        """带并发限制的社区构建"""
        async with semaphore:
            return await build_community(llm_client, cluster)

    # 步骤3: 并发构建所有社区
    communities: list[tuple[CommunityNode, list[CommunityEdge]]] = list(
        await semaphore_gather(
            *[limited_build_community(cluster) for cluster in community_clusters]
        )
    )

    # 步骤4: 分离节点和边到两个独立列表
    community_nodes: list[CommunityNode] = []
    community_edges: list[CommunityEdge] = []
    for community in communities:
        community_nodes.append(community[0])
        community_edges.extend(community[1])

    return community_nodes, community_edges


async def remove_communities(driver: GraphDriver):
    """移除所有社区节点
    
    从图数据库中删除所有Community类型的节点及其关联的边。
    这通常在重建社区之前调用，以清除旧的社区数据。
    
    Args:
        driver: 图数据库驱动
    """
    await driver.execute_query(
        """
        MATCH (c:Community)
        DETACH DELETE c
        """
    )


async def determine_entity_community(
    driver: GraphDriver, entity: EntityNode
) -> tuple[CommunityNode | None, bool]:
    """确定实体节点所属的社区
    
    这个函数用于确定一个实体节点应该属于哪个社区。它使用两个策略：
    1. 如果实体已经属于某个社区，直接返回该社区
    2. 如果实体不属于任何社区，通过邻居投票选择最常见的社区
    
    流程说明：
    第一步：检查实体是否已经有社区
    - 查询是否存在 (Community)-[:HAS_MEMBER]->(Entity) 关系
    - 如果存在，返回该社区和False（表示不是新成员）
    
    第二步：如果没有社区，通过邻居投票决定
    - 查询所有邻居实体所属的社区
    - 统计每个社区的出现次数（投票）
    - 选择票数最多的社区
    - 返回该社区和True（表示是新成员）
    
    Args:
        driver: 图数据库驱动
        entity: 要确定社区归属的实体节点
        
    Returns:
        (社区节点或None, 是否是新成员)的元组
        - 如果返回(community, False)：实体已经属于该社区
        - 如果返回(community, True)：实体应加入该社区（新成员）
        - 如果返回(None, False)：实体没有合适的社区
    """
    # 第一步：检查实体是否已经属于某个社区
    records, _, _ = await driver.execute_query(
        """
        MATCH (c:Community)-[:HAS_MEMBER]->(n:Entity {uuid: $entity_uuid})
        RETURN
        """
        + COMMUNITY_NODE_RETURN,
        entity_uuid=entity.uuid,
    )

    # 如果已经有社区，直接返回（不是新成员）
    if len(records) > 0:
        return get_community_node_from_record(records[0]), False

    # 第二步：如果没有社区，通过邻居投票选择社区
    # 查询：找到所有与该实体相连的其他实体及其所属社区
    match_query = """
        MATCH (c:Community)-[:HAS_MEMBER]->(m:Entity)-[:RELATES_TO]-(n:Entity {uuid: $entity_uuid})
    """
    # Kuzu数据库需要特殊处理：RELATES_TO是节点而非边
    if driver.provider == GraphProvider.KUZU:
        match_query = """
            MATCH (c:Community)-[:HAS_MEMBER]->(m:Entity)-[:RELATES_TO]-(e:RelatesToNode_)-[:RELATES_TO]-(n:Entity {uuid: $entity_uuid})
        """
    records, _, _ = await driver.execute_query(
        match_query
        + """
        RETURN
        """
        + COMMUNITY_NODE_RETURN,
        entity_uuid=entity.uuid,
    )

    # 从查询结果构建社区节点列表（可能包含重复）
    communities: list[CommunityNode] = [
        get_community_node_from_record(record) for record in records
    ]

    # 统计每个社区的出现次数（投票）
    community_map: dict[str, int] = defaultdict(int)
    for community in communities:
        community_map[community.uuid] += 1

    # 找出票数最多的社区
    community_uuid = None
    max_count = 0
    for uuid, count in community_map.items():
        if count > max_count:
            community_uuid = uuid
            max_count = count

    # 如果没有任何邻居有社区，返回None
    if max_count == 0:
        return None, False

    # 返回票数最多的社区对象（是新成员）
    for community in communities:
        if community.uuid == community_uuid:
            return community, True

    return None, False


async def update_community(
    driver: GraphDriver,
    llm_client: LLMClient,
    embedder: EmbedderClient,
    entity: EntityNode,
) -> tuple[list[CommunityNode], list[CommunityEdge]]:
    """更新社区（增量维护）
    
    这是社区维护的核心函数，用于在添加或更新实体节点时增量更新其所属社区。
    相比全量重建，增量更新大大提高了效率，适合实时场景。
    
    核心思想：
    当一个新的或更新的实体节点加入图时，我们需要：
    1. 确定它应该属于哪个社区（已有社区或邻居社区）
    2. 将该实体的信息融入社区摘要
    3. 如果是新成员，创建HAS_MEMBER边
    4. 更新社区的embedding和元数据
    
    详细流程：
    
    步骤1：确定实体所属社区
    - 调用 determine_entity_community() 查找社区
    - 如果实体已有社区，直接使用（is_new=False）
    - 如果没有，通过邻居投票找到最合适的社区（is_new=True）
    - 如果完全没有可用社区，返回空结果
    
    步骤2：更新社区摘要
    - 将实体的摘要与社区的现有摘要合并
    - 使用LLM生成新的综合摘要
    
    步骤3：更新社区名称
    - 基于新摘要生成新的简短描述作为社区名称
    
    步骤4：处理成员关系边
    - 如果是新成员（is_new=True），创建并保存HAS_MEMBER边
    - 如果已经是成员，不需要创建新边
    
    步骤5：更新embedding
    - 基于新名称重新生成社区的name_embedding
    - 这用于后续的语义搜索
    
    步骤6：保存社区节点
    - 将更新后的社区节点保存到数据库
    
    使用场景：
    - 添加新实体节点后调用，将其纳入社区
    - 更新实体节点后调用，刷新社区摘要
    - 周期性维护，保持社区信息最新
    
    Args:
        driver: 图数据库驱动
        llm_client: LLM客户端，用于生成摘要和名称
        embedder: 嵌入模型客户端，用于生成embedding
        entity: 要加入或更新社区的实体节点
        
    Returns:
        (更新的社区节点列表, 新创建的社区边列表)的元组
        - 如果没有找到合适的社区，返回空列表
        - 正常情况返回包含一个社区节点的列表
    """
    # 步骤1: 确定实体所属的社区
    community, is_new = await determine_entity_community(driver, entity)

    # 如果没有找到合适的社区，直接返回
    # 这种情况通常发生在实体完全孤立，没有邻居有社区
    if community is None:
        return [], []

    # 步骤2: 将实体摘要与社区摘要合并，生成新的综合摘要
    new_summary = await summarize_pair(llm_client, (entity.summary, community.summary))
    
    # 步骤3: 基于新摘要生成新的社区名称
    new_name = await generate_summary_description(llm_client, new_summary)

    # 更新社区对象的属性
    community.summary = new_summary
    community.name = new_name

    # 步骤4: 如果是新成员，创建HAS_MEMBER边
    community_edges = []
    if is_new:
        # 创建从社区到实体的HAS_MEMBER边
        community_edge = (build_community_edges([entity], community, utc_now()))[0]
        # 立即保存边到数据库
        await community_edge.save(driver)
        community_edges.append(community_edge)

    # 步骤5: 重新生成社区名称的embedding（用于语义搜索）
    await community.generate_name_embedding(embedder)

    # 步骤6: 保存更新后的社区节点到数据库
    await community.save(driver)

    return [community], community_edges
