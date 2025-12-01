import asyncio, os
import logging
from dotenv import load_dotenv
from graphiti_core import Graphiti
from graphiti_core.search.search_config_recipes import (
    NODE_HYBRID_SEARCH_RRF,
    COMBINED_HYBRID_SEARCH_RRF,
)
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.search.search_filters import SearchFilters

logger = logging.getLogger(__name__)

try:
    from .graphiti_factory import create_graphiti
except Exception:
    from graphiti_factory import create_graphiti

import rich, textwrap, re, json
from rich import print as rprint
from datetime import datetime, date, timedelta, timezone

# ─────────────────────────────────────────────────────────────────────────────
# Timezones (exported for tools_executor)
CHINA_TZ = timezone(timedelta(hours=8))
# ─────────────────────────────────────────────────────────────────────────────
from neo4j import AsyncGraphDatabase
import yaml
import openai

# Safe import for both package and direct script execution
try:
    from .load_config import load_graphiti_config, create_intent_client
except Exception:
    from load_config import load_graphiti_config, create_intent_client

load_dotenv()
os.environ["OPENAI_API_KEY"] = "DUMMY_KEY"

# Centralized config
CFG = load_graphiti_config()
try:
    from .load_config import normalize_group_id as _norm_gid  # type: ignore
except Exception:
    from load_config import normalize_group_id as _norm_gid  # type: ignore

# Intent classification client
_intent_client = create_intent_client(CFG)


# Derive user uid per-call (Flask context not directly available here if used as a module)
def _current_user_uid() -> str:
    try:
        # Lazy import to avoid hard dependency when used as script
        from auth import get_current_user_id  # type: ignore
    except Exception:
        try:
            from backend.auth import get_current_user_id  # type: ignore
        except Exception:
            get_current_user_id = None  # type: ignore
    if callable(locals().get("get_current_user_id")):  # type: ignore
        try:
            uid = get_current_user_id()  # type: ignore
            if uid:
                return uid
        except Exception:
            pass
    return CFG.user_uid


def is_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


# Prefer readable truncation for CJK (no whitespace word boundaries)
# Falls back to textwrap.shorten for non-CJK text.
def _shorten_display(text: str | None, width: int = 60) -> str:
    if not text:
        return ""
    s = str(text).strip().replace("\n", " ")
    try:
        if is_chinese(s):
            return s if len(s) <= width else (s[:width] + " …")
        return textwrap.shorten(s, width=width, placeholder=" …")
    except Exception:
        return s if len(s) <= width else (s[:width] + " …")


async def hybrid_search(query: str, top_k=5, debug=True, group_id: str | None = None):
    g = create_graphiti(verbose=False)

    # 使用 COMBINED_HYBRID_SEARCH_RRF 同时召回节点与社区，召回更稳
    cfg = COMBINED_HYBRID_SEARCH_RRF.model_copy()
    cfg.limit = top_k

    # 查询规范化：中文做轻量扩展；非中文做小写标准化以避免大小写差异
    expanded_query = query
    if is_chinese(query):
        extras = ["谁", "什么", "什么时候", "在哪里", "怎么", "如何", "相关", "关于"]
        expanded_query = " ".join({query, *extras})
    else:
        expanded_query = query.lower()

    res = await g.search_(
        expanded_query,
        cfg,
        search_filter=SearchFilters(),
        group_ids=[_norm_gid(group_id or _current_user_uid())],
    )

    if debug:
        rich.print(f"[bold cyan]\n🔎 query:[/]{query}  -> expanded:[{expanded_query}]")
        rich.print(f"[bold]Returned {len(res.nodes)} nodes, {len(res.edges)} edges[/]")
        for n in res.nodes:
            txt = n.summary or n.name
            short_txt = _shorten_display(txt, width=60)
            rich.print(" •", short_txt)

    await g.close()
    # 返回节点与边，便于外部拼接包含时间的上下文
    return res.nodes, res.edges


# ───────────── 时间解析工具（与 ingest 对齐） ─────────────


def _parse_dt(value) -> datetime | None:
    try:
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            s = str(value).strip()
            if not s:
                return None
            s = s.replace("T", " ")
            if s.endswith("Z") or s.endswith("z"):
                s = s[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                # 兼容旧格式：YYYYMMDD-HHMMSS
                try:
                    dt = datetime.strptime(s, "%Y%m%d-%H%M%S")
                except Exception:
                    return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CHINA_TZ)
        return dt.astimezone(CHINA_TZ)
    except Exception:
        return None


# 优先将 content 中的时间视为上海本地时间（即使字符串里带有其它偏移，如+09:00）
# 这是为了与业务存储语义对齐：content 的时间语义=本地CST
_def_content_dt_pat = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)")


def _parse_content_dt(value) -> datetime | None:
    try:
        if value is None:
            return None
        if isinstance(value, datetime):
            dt_local = value
            if dt_local.tzinfo is None:
                dt_local = dt_local.replace(tzinfo=CHINA_TZ)
            return dt_local.astimezone(CHINA_TZ)
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("T", " ")
        m = _def_content_dt_pat.match(s)
        if not m:
            # 回退到通用解析
            return _parse_dt(s)
        naive_str = f"{m.group(1)} {m.group(2)}"
        # 解析为本地CST
        try:
            dt_local = datetime.fromisoformat(naive_str)
        except ValueError:
            try:
                dt_local = datetime.strptime(naive_str, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    dt_local = datetime.strptime(naive_str, "%Y-%m-%d %H:%M")
                except Exception:
                    return None
        if dt_local.tzinfo is None:
            dt_local = dt_local.replace(tzinfo=CHINA_TZ)
        return dt_local.astimezone(CHINA_TZ)
    except Exception:
        return None


# ───────────── 时间区间/事件时长辅助方法 ─────────────
DUR_KEYWORDS = [
    "多久",
    "持续",
    "时长",
    "多长时间",
    "how long",
    "duration",
    "用时",
    "耗时",
    "花了多久",
    "花费多长时间",
]

DATE_PATTERNS = [
    # 2025-07-08
    re.compile(r"(\d{4})-(\d{2})-(\d{2})"),
    # 20250708
    re.compile(r"(\d{4})(\d{2})(\d{2})"),
    # 2025年7月8日（允许前导零与无零）
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
]


def is_duration_intent(q: str) -> bool:
    ql = q.lower()
    patterns = [
        r"多长\s*时间",
        r"多长\s*时",
        r"多久",
        r"持续",
        r"时长",
        r"用\s*时",
        r"耗\s*时",
        r"花(了)?\s*多\s*久",
        r"花费\s*多长\s*时间",
        r"how\s*long",
        r"duration",
    ]
    try:
        return any(re.search(pat, q) or re.search(pat, ql) for pat in patterns)
    except Exception:
        return any(k in q or k in ql for k in DUR_KEYWORDS)


def extract_date(q: str) -> date | None:
    for pat in DATE_PATTERNS:
        m = pat.search(q)
        if not m:
            continue
        y, mth, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mth, d)
        except ValueError:
            continue
    return None


def extract_event_type_intent(query: str) -> str | None:
    """
    使用LLM分析用户查询，提取想要查询的事件类型。
    返回事件类型关键词，如："通勤"、"会议"、"健身"、"睡觉"等
    """
    # 添加调试日志
    print(f"DEBUG: 正在分析查询: {query}")

    try:
        prompt = f"""
请分析以下用户查询，提取用户想要查询的具体事件类型。只返回核心的事件类型关键词，不要其他解释。

用户查询：{query}

常见事件类型包括但不限于：
- 通勤（交通、上班、下班、开车、骑车、地铁等）
- 会议（开会、讨论、会议、meeting等）
- 健身（运动、锻炼、跑步、健身房、瑜伽等）
- 睡觉（休息、睡眠、午睡等）
- 工作（编程、开发、写代码、办公等）
- 学习（看书、阅读、上课、培训等）
- 娱乐（看电影、游戏、听音乐等）
- 用餐（吃饭、早餐、午餐、晚餐等）

如果查询是关于某个具体事件类型的时长，请只返回该事件类型的关键词（如：通勤、会议、健身等）。
如果查询不是关于特定事件类型，返回"all"。
"""

        print(f"DEBUG: 发送给LLM的prompt: {prompt[:200]}...")

        response = _intent_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50,
        )

        event_type = response.choices[0].message.content.strip().lower()
        print(f"DEBUG: LLM原始响应: {event_type}")
        print(f"DEBUG: 处理后的事件类型: {event_type}")

        # 清理可能的额外文本
        if event_type and len(event_type) < 20:  # 合理的关键词长度
            return event_type
        return None

    except Exception as e:
        print(f"DEBUG: LLM调用异常: {str(e)}")
        print("DEBUG: 使用fallback关键词匹配")

        # Fallback：使用简单的关键词匹配
        query_lower = query.lower()

        # 关键词映射表
        type_keywords = {
            "通勤": [
                "通勤",
                "上班",
                "下班",
                "地铁",
                "公交",
                "开车",
                "骑车",
                "电动车",
                "commute",
                "交通",
            ],
            "会议": ["会议", "开会", "讨论", "meeting", "discussion", "会"],
            "健身": [
                "健身",
                "运动",
                "锻炼",
                "跑步",
                "瑜伽",
                "gym",
                "fitness",
                "exercise",
                "训练",
            ],
            "睡觉": ["睡觉", "休息", "睡眠", "午睡", "sleep", "rest", "睡"],
            "工作": ["工作", "编程", "开发", "办公", "写代码", "work", "coding"],
            "学习": ["学习", "看书", "阅读", "上课", "培训", "study", "learning"],
            "娱乐": [
                "娱乐",
                "看电影",
                "游戏",
                "听音乐",
                "entertainment",
                "movie",
                "game",
            ],
            "用餐": ["吃饭", "早餐", "午餐", "晚餐", "用餐", "meal", "eating"],
        }

        # 查找匹配的事件类型
        for event_type, keywords in type_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                print(f"DEBUG: Fallback识别到事件类型: {event_type}")
                return event_type

        print("DEBUG: Fallback未识别到特定事件类型")
        return "all"


def classify_event_with_llm(event_content: dict, target_type: str) -> bool:
    """
    使用LLM判断给定事件是否属于目标类型。
    event_content: episodic事件的content字段（JSON解析后的dict）
    target_type: 目标事件类型关键词
    返回: True如果属于目标类型，False否则
    """
    if target_type == "all":
        return True

    try:
        # 提取事件的关键信息
        action = event_content.get("action", "")
        location = event_content.get("location", "")
        events = event_content.get("events", [])
        participants = event_content.get("participants", [])

        event_summary = f"动作：{action}"
        if location:
            event_summary += f"，地点：{location}"
        if events:
            event_summary += f"，事件：{events}"
        if participants:
            event_summary += f"，参与者：{participants}"

        prompt = f"""
请判断以下事件是否属于"{target_type}"类型。只回答"是"或"否"，不要其他解释。

事件信息：{event_summary}

目标类型：{target_type}

判断标准：
- 如果事件的主要活动、动作或目的与目标类型相关，则回答"是"
- 如果事件与目标类型无关或只是间接相关，则回答"否"
"""

        response = _intent_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
        )

        result = response.choices[0].message.content.strip().lower()
        return "是" in result or "yes" in result

    except Exception as e:
        print(f"DEBUG: LLM分类异常: {str(e)}")

        # Fallback：使用简单的关键词匹配
        action = str(event_content.get("action", "")).lower()
        location = str(event_content.get("location", "")).lower()
        content_text = f"{action} {location}".lower()

        # 基础关键词匹配规则
        type_keywords = {
            "通勤": [
                "通勤",
                "上班",
                "下班",
                "地铁",
                "公交",
                "开车",
                "骑车",
                "电动车",
                "commute",
            ],
            "会议": [
                "会议",
                "开会",
                "讨论",
                "meeting",
                "discussion",
                "项目",
                "启动会",
                "沟通",
            ],
            "健身": [
                "健身",
                "运动",
                "锻炼",
                "跑步",
                "瑜伽",
                "gym",
                "fitness",
                "exercise",
                "训练",
                "教练",
            ],
            "睡觉": ["睡觉", "休息", "睡眠", "午睡", "sleep", "rest"],
            "工作": ["工作", "编程", "开发", "办公", "写代码", "work", "coding"],
            "学习": ["学习", "看书", "阅读", "上课", "培训", "study", "learning"],
            "娱乐": [
                "娱乐",
                "看电影",
                "游戏",
                "听音乐",
                "entertainment",
                "movie",
                "game",
            ],
            "用餐": ["吃饭", "早餐", "午餐", "晚餐", "用餐", "meal", "eating"],
        }

        keywords = type_keywords.get(target_type, [target_type])
        is_match = any(kw in content_text for kw in keywords)
        print(f"DEBUG: Fallback分类 {action} -> {target_type}: {is_match}")
        return is_match


async def fetch_day_episodes(day: date, group_id: str | None = None) -> list[dict]:
    """Query Episodic nodes by group and day.
    Primary path: (e:Episodic) with date(e.valid_at)=day, returning e.content.
    Fallback: fetch all (e:Episodic) by group and filter client-side by content.start_time.
    """
    driver = AsyncGraphDatabase.driver(
        CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password)
    )
    rows: list[dict] = []
    async with driver.session() as session:
        # 优先使用 valid_at 进行日过滤与排序
        cypher = (
            "MATCH (e:Episodic) \n"
            "WHERE e.group_id = $gid AND e.valid_at IS NOT NULL AND date(e.valid_at) = date($day) \n"
            "RETURN e.content AS body, e.valid_at AS ref ORDER BY ref ASC"
        )
        day_str = day.isoformat()
        try:
            res = await session.run(
                cypher, gid=_norm_gid(group_id or _current_user_uid()), day=day_str
            )
            async for rec in res:
                rows.append(
                    {
                        "body": rec.get("body"),
                        "ref": rec.get("ref"),
                    }
                )
        except Exception:
            rows = []

        # 如果按 valid_at 为空或无结果，退回到分组拉取+客户端过滤（content.start_time）
        if not rows:
            cypher_all = (
                "MATCH (e:Episodic) \n"
                "WHERE e.group_id = $gid \n"
                "RETURN e.content AS body, e.valid_at AS ref"
            )
            res2 = await session.run(cypher_all, gid=_norm_gid(group_id or _current_user_uid()))
            async for rec in res2:
                rows.append(
                    {
                        "body": rec.get("body"),
                        "ref": rec.get("ref"),
                    }
                )
    await driver.close()

    if not rows:
        return []

    # client-side 日期过滤：解析 start_time 的日期与目标 day 比较
    filtered: list[dict] = []
    for item in rows:
        body_str = item.get("body")
        if not body_str:
            continue
        try:
            body = json.loads(body_str)
        except Exception:
            continue
        st = body.get("start_time")
        dt = _parse_dt(st)
        if dt and dt.date() == day:
            filtered.append(
                {
                    "body": body_str,
                    "ref": item.get("ref"),
                }
            )
    return filtered


def _parse_start_end(ep_body: dict) -> tuple[datetime | None, datetime | None]:
    s = ep_body.get("start_time")
    e = ep_body.get("end_time")
    start_dt = _parse_dt(s)
    end_dt = _parse_dt(e)
    return start_dt, end_dt


# 针对 content 语义（CST优先）的起止时间解析


def _parse_content_start_end(ep_body: dict) -> tuple[datetime | None, datetime | None]:
    s = ep_body.get("start_time")
    e = ep_body.get("end_time")
    start_dt = _parse_content_dt(s)
    end_dt = _parse_content_dt(e)
    return start_dt, end_dt


def _is_meeting(ep_body: dict) -> bool:
    text_fields: list[str] = []
    action = (ep_body.get("action") or "").lower()
    text_fields.append(action)
    # 事件摘要里也可能包含会议词
    events = ep_body.get("events") or []
    for ev in events:
        if isinstance(ev, dict):
            text_fields.append((ev.get("summary") or "").lower())
            text_fields.append((ev.get("event_type") or "").lower())
    blob = "\n".join(text_fields)
    return ("meeting" in blob) or ("会议" in blob)


def _classify_category(ep_body: dict) -> str:
    text_fields: list[str] = []
    action = str(ep_body.get("action") or "").lower()
    text_fields.append(action)
    events = ep_body.get("events") or []
    for ev in events:
        if isinstance(ev, dict):
            text_fields.append(str(ev.get("summary") or "").lower())
            text_fields.append(str(ev.get("event_type") or "").lower())
    blob = "\n".join(text_fields)
    if ("meeting" in blob) or ("会议" in blob):
        return "meeting"
    if ("commute" in blob) or ("通勤" in blob) or ("上班" in blob) or ("下班" in blob):
        return "commute"
    if ("讨论" in blob) or ("dialogue" in blob):
        return "dialogue"
    return "other"


async def compute_event_durations_for_day(
    day: date, group_id: str | None = None, event_type: str | None = None
):
    """
    统计指定日期当天所有 Episodic 事件的时长。

    参数：
    - day: 查询日期
    - group_id: 用户组ID
    - event_type: 可选的事件类型过滤，如"通勤"、"会议"、"健身"等。如果为None则返回所有事件

    返回：{"segments": [...], "total_minutes": int, "filtered_type": str | None}
    segment 字段：scene_id, action, start_time, end_time, minutes, topic?, is_meeting, category
    """
    raw_eps = await fetch_day_episodes(day, group_id or _current_user_uid())
    segments = []

    def _topic_from_events(body: dict) -> str | None:
        events = body.get("events") or []
        for ev in events:
            if isinstance(ev, dict) and ev.get("summary"):
                s = str(ev.get("summary"))
                first = s.strip().splitlines()[0].strip()
                return first if first else None
        return None

    for item in raw_eps:
        body_str = item.get("body")
        if not body_str:
            continue
        try:
            body = json.loads(body_str)
        except Exception:
            continue

        # 如果指定了事件类型，使用LLM进行智能过滤
        if event_type and event_type != "all":
            try:
                is_target_type = classify_event_with_llm(body, event_type)
                if not is_target_type:
                    continue  # 跳过不匹配的事件
            except Exception:
                # LLM分类失败时，退回到基础的关键词匹配
                action = str(body.get("action", "")).lower()
                location = str(body.get("location", "")).lower()
                content_text = f"{action} {location}".lower()

                # 基础关键词匹配规则
                type_keywords = {
                    "通勤": [
                        "通勤",
                        "上班",
                        "下班",
                        "地铁",
                        "公交",
                        "开车",
                        "骑车",
                        "电动车",
                        "commute",
                    ],
                    "会议": ["会议", "开会", "讨论", "meeting", "discussion"],
                    "健身": [
                        "健身",
                        "运动",
                        "锻炼",
                        "跑步",
                        "瑜伽",
                        "gym",
                        "fitness",
                        "exercise",
                    ],
                    "睡觉": ["睡觉", "休息", "睡眠", "午睡", "sleep", "rest"],
                    "工作": [
                        "工作",
                        "编程",
                        "开发",
                        "办公",
                        "写代码",
                        "work",
                        "coding",
                    ],
                    "学习": [
                        "学习",
                        "看书",
                        "阅读",
                        "上课",
                        "培训",
                        "study",
                        "learning",
                    ],
                    "娱乐": [
                        "娱乐",
                        "看电影",
                        "游戏",
                        "听音乐",
                        "entertainment",
                        "movie",
                        "game",
                    ],
                    "用餐": ["吃饭", "早餐", "午餐", "晚餐", "用餐", "meal", "eating"],
                }

                keywords = type_keywords.get(event_type, [event_type])
                if not any(kw in content_text for kw in keywords):
                    continue

        start_dt, end_dt = _parse_start_end(body)
        if start_dt and end_dt and end_dt > start_dt:
            minutes = int((end_dt - start_dt) / timedelta(minutes=1))
            seg = {
                "scene_id": body.get("scene_id"),
                "action": body.get("action"),
                "start_time": start_dt,
                "end_time": end_dt,
                "minutes": minutes,
                "is_meeting": _is_meeting(body),
                "category": _classify_category(body),
            }
            topic = _topic_from_events(body)
            if topic:
                seg["topic"] = topic
            segments.append(seg)

    # YAML 回退（如果没有找到episodic数据且没有指定事件类型过滤）
    if not segments and not event_type:
        graphiti_yaml_dir = CFG.yaml_dir
        graphiti_yaml_file = CFG.yaml_file
        candidates = [
            os.getenv("SCRIPT_YAML"),
            graphiti_yaml_file,
            "long_script_demo.yaml",
            "demo_script.yaml",
            "script.yaml",
        ]
        yaml_path = next((p for p in candidates if p and os.path.exists(p)), None)
        if yaml_path:
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    scenes = yaml.safe_load(f) or []
                for sc in scenes:
                    st = sc.get("start_time")
                    et = sc.get("end_time")
                    std = _parse_dt(st)
                    etd = _parse_dt(et)
                    if not std or not etd or std.date() != day:
                        continue
                    minutes = int((etd - std) / timedelta(minutes=1))
                    seg = {
                        "scene_id": sc.get("scene_id"),
                        "action": sc.get("action"),
                        "start_time": std,
                        "end_time": etd,
                        "minutes": minutes,
                        "is_meeting": _is_meeting(sc),
                        "category": _classify_category(sc),
                    }
                    topic = (
                        (sc.get("events") or [{}])[0].get("summary")
                        if sc.get("events")
                        else None
                    )
                    if topic:
                        seg["topic"] = str(topic).strip().splitlines()[0].strip()
                    segments.append(seg)
            except Exception:
                pass
        if not segments:
            yaml_dir = CFG.yaml_dir
            try:
                if os.path.isdir(yaml_dir):
                    from glob import glob

                    files = sorted(glob(os.path.join(yaml_dir, "*.yaml")))
                    for fp in files:
                        try:
                            with open(fp, "r", encoding="utf-8") as f:
                                scenes = yaml.safe_load(f) or []
                            for sc in scenes:
                                st = sc.get("start_time")
                                et = sc.get("end_time")
                                std = _parse_dt(st)
                                etd = _parse_dt(et)
                                if not std or not etd or std.date() != day:
                                    continue
                                minutes = int((etd - std) / timedelta(minutes=1))
                                seg = {
                                    "scene_id": sc.get("scene_id"),
                                    "action": sc.get("action"),
                                    "start_time": std,
                                    "end_time": etd,
                                    "minutes": minutes,
                                    "is_meeting": _is_meeting(sc),
                                    "category": _classify_category(sc),
                                }
                                topic = (
                                    (sc.get("events") or [{}])[0].get("summary")
                                    if sc.get("events")
                                    else None
                                )
                                if topic:
                                    seg["topic"] = (
                                        str(topic).strip().splitlines()[0].strip()
                                    )
                                segments.append(seg)
                        except Exception:
                            continue
            except Exception:
                pass

    total_minutes = sum(s["minutes"] for s in segments)
    return {
        "segments": segments,
        "total_minutes": total_minutes,
        "filtered_type": event_type,
    }


async def compute_meeting_durations_for_day(day: date, group_id: str | None = None):
    """
    兼容旧接口：从所有事件时长中过滤出会议事件。
    """
    agg = await compute_event_durations_for_day(day, group_id, event_type="会议")
    return agg


async def compute_typed_event_durations_for_day(
    day: date, event_type: str, group_id: str | None = None
):
    """
    通用的按事件类型计算时长的函数。

    参数：
    - day: 查询日期
    - event_type: 事件类型，如"通勤"、"会议"、"健身"等
    - group_id: 用户组ID

    返回：{"segments": [...], "total_minutes": int, "filtered_type": str}
    """
    return await compute_event_durations_for_day(day, group_id, event_type)


# ==== Debug utilities ====
DEBUG_QA = os.getenv("GRAPHITI_QA_DEBUG", "1") in {"1", "true", "True"}


def _dbg(msg: str):
    if DEBUG_QA:
        try:
            rprint(msg)
        except Exception:
            print(msg)


# ==== Parsing utilities ====


def _parse_body(body_val) -> dict | None:
    if isinstance(body_val, dict):
        return body_val
    if isinstance(body_val, (bytes, bytearray)):
        try:
            text = body_val.decode("utf-8", errors="ignore")
        except Exception:
            text = str(body_val)
    else:
        text = str(body_val)

    # 尝试直接解析
    try:
        obj = json.loads(text)
        # 如果结果是字符串，尝试再次解析
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except Exception:
                pass
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    # 尝试去除外层引号后解析
    try:
        t = text.strip()
        if (t.startswith('"') and t.endswith('"')) or (
            t.startswith("'") and t.endswith("'")
        ):
            t = t[1:-1]
        obj2 = json.loads(t)
        return obj2 if isinstance(obj2, dict) else None
    except Exception:
        pass

    # 尝试处理双重转义的情况
    try:
        t = text.strip()
        # 去除外层引号
        if (t.startswith('"') and t.endswith('"')) or (
            t.startswith("'") and t.endswith("'")
        ):
            t = t[1:-1]

        # 处理转义字符 - 先处理双反斜杠，再处理转义引号
        t = t.replace("\\\\\\\\", "\\\\").replace('\\\\"', '"')

        obj3 = json.loads(t)
        return obj3 if isinstance(obj3, dict) else None
    except Exception:
        return None


def _topic_from_events_local(body: dict) -> str | None:
    events = body.get("events") or []
    for ev in events:
        if isinstance(ev, dict) and ev.get("summary"):
            s = str(ev.get("summary"))
            first = s.strip().splitlines()[0].strip()
            return first if first else None
    return None


def _deepcopy_events(body: dict) -> list[dict]:
    raw_events = body.get("events") or []
    events: list[dict] = []
    for ev in raw_events:
        if isinstance(ev, dict):
            try:
                events.append(dict(ev))
            except Exception:
                events.append(ev)
    return events


def _overlap(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    return (a_end >= b_start) and (a_start <= b_end)


# ==== Core query ====
async def fetch_episodes_by_timerange(
    start: datetime, end: datetime, group_id: str | None = None
) -> list[dict]:
    """Query Episodic nodes that overlap with a given [start, end] interval (CST).
    Returns: list of segments with keys: scene_id, action, start_time, end_time, minutes, topic?, is_meeting, category, events
    """
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return []
    if end <= start:
        return []

    # Normalize CST
    try:
        start_cst = (
            start.replace(tzinfo=CHINA_TZ)
            if start.tzinfo is None
            else start.astimezone(CHINA_TZ)
        )
    except Exception:
        start_cst = start
    try:
        end_cst = (
            end.replace(tzinfo=CHINA_TZ)
            if end.tzinfo is None
            else end.astimezone(CHINA_TZ)
        )
    except Exception:
        end_cst = end

    gid = _norm_gid(group_id or _current_user_uid())
    _dbg(
        f"[debug] timerange query CST: start={start_cst.isoformat()} end={end_cst.isoformat()} gid={gid}"
    )

    driver = AsyncGraphDatabase.driver(
        CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password)
    )
    rows: list[dict] = []
    async with driver.session() as session:
        cypher = (
            "MATCH (e:Episodic) \n"
            "WHERE e.group_id = $gid AND e.valid_at IS NOT NULL \n"
            "  AND date(e.valid_at) >= date($start_date) AND date(e.valid_at) <= date($end_date) \n"
            "RETURN e.content AS body, e.valid_at AS ref ORDER BY ref ASC"
        )
        try:
            start_date = start_cst.date().isoformat()
            end_date = end_cst.date().isoformat()
            _dbg(
                f"[debug] cypher date filter: start_date={start_date} end_date={end_date}"
            )
            res = await session.run(
                cypher, gid=gid, start_date=start_date, end_date=end_date
            )
            async for rec in res:
                rows.append({"body": rec.get("body"), "ref": rec.get("ref")})
        except Exception as e:
            _dbg(f"[debug][error] cypher error: {e}")
            rows = []
        _dbg(f"[debug] cypher rows: {len(rows)} (gid={gid})")
    await driver.close()

    if not rows:
        _dbg(f"[debug] no rows after cypher date filter")
        return []

    segments: list[dict] = []
    for idx, item in enumerate(rows, start=1):
        body_val = item.get("body")
        body = _parse_body(body_val)
        if body is None:
            _dbg(f"[debug] row#{idx} body parse failed")
            continue

        # parse times (content CST semantics)
        std, etd = _parse_content_start_end(body)
        if DEBUG_QA:
            try:
                sid = body.get("scene_id")
                ev_len = len(body.get("events") or [])
                keys = list(body.keys())
                _dbg(f"[debug] row#{idx} scene_id={sid} keys={keys} events={ev_len}")
            except Exception:
                pass
        if not std or not etd:
            _dbg(f"[debug] row#{idx} skip: no parsed start/end")
            continue

        if _overlap(std, etd, start_cst, end_cst):
            minutes = int((etd - std) / timedelta(minutes=1)) if etd > std else 0
            seg = {
                "scene_id": body.get("scene_id"),
                "action": body.get("action"),
                "start_time": std,
                "end_time": etd,
                "minutes": max(0, minutes),
                "is_meeting": _is_meeting(body),
                "category": _classify_category(body),
                "events": _deepcopy_events(body),
            }
            topic = _topic_from_events_local(body)
            if topic:
                seg["topic"] = topic
            segments.append(seg)
            _dbg(
                f"[debug] row#{idx} excluded (OVERLAP), start_time={std}, end_time={etd}"
            )

    _dbg(f"[debug] total segments matched: {len(segments)}")
    segments.sort(key=lambda s: s.get("start_time"))
    return segments
