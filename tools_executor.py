"""
Tool Execution Layer
执行具体的检索工具调用，并格式化返回结果
"""
import json
from datetime import date, datetime, timedelta
from typing import Any

try:
    from .search_helper import (
        hybrid_search,
        fetch_episodes_by_timerange,
        compute_event_durations_for_day,
        compute_typed_event_durations_for_day,
        extract_date,
        CHINA_TZ,
    )
    from .qa_demo import _find_latest_duration, _parse_recent_period, _parse_user_time, _to_cst
except Exception:
    from search_helper import (
        hybrid_search,
        fetch_episodes_by_timerange,
        compute_event_durations_for_day,
        compute_typed_event_durations_for_day,
        extract_date,
        CHINA_TZ,
    )
    from qa_demo import _find_latest_duration, _parse_recent_period, _parse_user_time, _to_cst


def _format_search_result(nodes: list, edges: list) -> str:
    """格式化混合检索结果为可读文本"""
    if not nodes and not edges:
        return "未找到相关记忆。"
    
    lines = []
    
    if nodes:
        lines.append("【相关记忆节点】")
        for idx, n in enumerate(nodes, 1):
            content = (n.summary or n.name or "").strip()
            if content:
                lines.append(f"{idx}. {content}")
    
    if edges:
        lines.append("\n【相关事实】")
        for idx, e in enumerate(edges, 1):
            fact = (getattr(e, "fact", "") or "").strip()
            if not fact:
                continue
            ts = getattr(e, "valid_at", None)
            ts_str = ""
            if ts:
                try:
                    if isinstance(ts, datetime):
                        ts_str = f" [{_to_cst(ts).strftime('%Y-%m-%d %H:%M')}]"
                    else:
                        ts_str = f" [{ts}]"
                except Exception:
                    pass
            lines.append(f"{idx}. {fact}{ts_str}")
    
    return "\n".join(lines)


def _format_time_range_result(segments: list[dict]) -> str:
    """格式化时间范围检索结果"""
    if not segments:
        return "该时间范围内未找到事件记录。"
    
    lines = [f"共找到 {len(segments)} 条事件记录：\n"]
    
    for idx, seg in enumerate(segments, 1):
        try:
            st = _to_cst(seg["start_time"]).strftime("%Y-%m-%d %H:%M:%S")
            et = _to_cst(seg["end_time"]).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            st = str(seg.get("start_time", ""))
            et = str(seg.get("end_time", ""))
        
        action = seg.get("action", "")
        minutes = seg.get("minutes", 0)
        
        # 提取会议/对话摘要
        events = seg.get("events", [])
        summaries = []
        for ev in events:
            if isinstance(ev, dict):
                ev_type = str(ev.get("event_type", "")).lower()
                summ = str(ev.get("summary", "")).strip()
                if summ:
                    if ev_type == "meeting":
                        summaries.append(f"[会议] {summ}")
                    elif ev_type == "dialogue":
                        summaries.append(f"[对话] {summ}")
                    else:
                        summaries.append(summ)
        
        line = f"{idx}. {st} ~ {et} ({minutes}分钟) - {action}"
        if summaries:
            line += "\n   内容：" + "\n   ".join(summaries)
        
        lines.append(line)
    
    return "\n".join(lines)


def _format_duration_result(agg: dict, label: str = "事件") -> str:
    """格式化时长统计结果"""
    segments = agg.get("segments", [])
    total = agg.get("total_minutes", 0)
    
    if not segments:
        return f"未找到{label}相关的时长记录。"
    
    # 格式化总时长
    hours = total // 60
    minutes = total % 60
    if hours > 0:
        time_str = f"{hours}小时{minutes}分钟" if minutes > 0 else f"{hours}小时"
    else:
        time_str = f"{minutes}分钟"
    
    lines = [f"{label}总时长：{time_str}（{total}分钟）\n"]
    lines.append("详细时段：")
    
    for idx, seg in enumerate(segments, 1):
        try:
            st = _to_cst(seg["start_time"]).strftime("%H:%M:%S")
            et = _to_cst(seg["end_time"]).strftime("%H:%M:%S")
        except Exception:
            st = str(seg.get("start_time", ""))[:8]
            et = str(seg.get("end_time", ""))[:8]
        
        seg_minutes = seg.get("minutes", 0)
        action = seg.get("action", "")
        topic = seg.get("topic")
        
        seg_hours = seg_minutes // 60
        seg_mins = seg_minutes % 60
        if seg_hours > 0:
            seg_time = f"{seg_hours}h{seg_mins}m" if seg_mins > 0 else f"{seg_hours}h"
        else:
            seg_time = f"{seg_mins}m"
        
        line = f"{idx}. {st}~{et} ({seg_time}) {action}"
        if topic:
            line += f" - {topic}"
        lines.append(line)
    
    return "\n".join(lines)


async def execute_tool(
    tool_name: str, 
    arguments: dict, 
    user_id: str | None = None,
    use_cache: bool = True
) -> str:
    """
    执行指定的工具调用
    
    Args:
        tool_name: 工具名称
        arguments: 工具参数（已解析的dict）
        user_id: 用户ID（用于group_id隔离）
        use_cache: 是否使用缓存（默认True）
    
    Returns:
        格式化的工具执行结果（字符串）
    """
    # 尝试从缓存获取
    if use_cache:
        try:
            from .tools_cache import get_cache
            cache = get_cache()
            cached_result = cache.get(tool_name, arguments, user_id)
            if cached_result is not None:
                return cached_result
        except Exception:
            pass  # 缓存失败不影响主流程
    
    try:
        if tool_name == "hybrid_semantic_search":
            query = arguments["query"]
            top_k = arguments.get("top_k", 8)
            nodes, edges = await hybrid_search(query, top_k, debug=False, group_id=user_id)
            return _format_search_result(nodes, edges)
        
        elif tool_name == "fetch_time_range_episodes":
            start_str = arguments["start_time"]
            end_str = arguments["end_time"]
            start = _parse_user_time(start_str)
            end = _parse_user_time(end_str)
            
            if not start or not end:
                return f"时间解析失败：start={start_str}, end={end_str}"
            if end <= start:
                return "结束时间必须晚于开始时间"
            
            segments = await fetch_episodes_by_timerange(start, end, group_id=user_id)
            return _format_time_range_result(segments)
        
        elif tool_name == "compute_event_duration":
            event_type = arguments["event_type"]
            date_str = arguments.get("date")
            time_window = arguments.get("time_window")
            
            # 单日查询
            if date_str:
                try:
                    d = date.fromisoformat(date_str)
                except Exception:
                    return f"日期格式错误：{date_str}，应为 YYYY-MM-DD"
                
                if event_type and event_type != "all":
                    agg = await compute_typed_event_durations_for_day(d, event_type, group_id=user_id)
                    label = f"{d} {event_type}"
                else:
                    agg = await compute_event_durations_for_day(d, group_id=user_id)
                    label = f"{d} 所有事件"
                
                return _format_duration_result(agg, label)
            
            # 时间窗口查询
            elif time_window:
                today = date.today()
                dates = []
                
                if time_window == "today":
                    dates = [today]
                    tag = "今天"
                elif time_window == "yesterday":
                    dates = [today - timedelta(days=1)]
                    tag = "昨天"
                elif time_window == "this_week":
                    weekday = today.weekday()
                    monday = today - timedelta(days=weekday)
                    dates = [monday + timedelta(days=i) for i in range((today - monday).days + 1)]
                    tag = "本周"
                elif time_window == "this_month":
                    first = today.replace(day=1)
                    days = (today - first).days + 1
                    dates = [first + timedelta(days=i) for i in range(days)]
                    tag = "本月"
                elif time_window == "recent_7_days":
                    dates = [today - timedelta(days=i) for i in range(7)]
                    dates.reverse()
                    tag = "近7天"
                elif time_window == "recent_30_days":
                    dates = [today - timedelta(days=i) for i in range(30)]
                    dates.reverse()
                    tag = "近30天"
                else:
                    return f"不支持的时间窗口：{time_window}"
                
                total = 0
                all_segs = []
                for d in dates:
                    if event_type and event_type != "all":
                        agg = await compute_typed_event_durations_for_day(d, event_type, group_id=user_id)
                    else:
                        agg = await compute_event_durations_for_day(d, group_id=user_id)
                    total += agg.get("total_minutes", 0)
                    all_segs.extend(agg.get("segments", []))
                
                # 按结束时间排序
                all_segs.sort(key=lambda s: s.get("end_time"))
                
                label = f"{tag} {event_type}" if event_type != "all" else f"{tag} 所有事件"
                return _format_duration_result({"segments": all_segs, "total_minutes": total}, label)
            
            else:
                return "必须指定 date 或 time_window 参数"
        
        elif tool_name == "find_latest_event":
            event_type = arguments["event_type"]
            max_days = arguments.get("max_days_back", 30)
            result = await _find_latest_duration(event_type, max_days)
            return result
        
        else:
            result = f"未知工具：{tool_name}"
    
    except Exception as e:
        result = f"工具执行失败 [{tool_name}]：{str(e)}"
    
    # 缓存成功的结果
    if use_cache and not result.startswith("工具执行失败") and not result.startswith("未知工具"):
        try:
            from .tools_cache import get_cache
            cache = get_cache()
            cache.set(tool_name, arguments, user_id, result)
        except Exception:
            pass
    
    return result

