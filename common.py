"""
Common utilities shared between local bridge server (server.py)
and Vercel serverless backend (api/index.py).

This module contains:
  - AI prompts (analyze, schedule, batch extraction)
  - JSON extraction from LLM responses
  - Keyword-based fallback analysis
  - Board time helpers (parse, format, conflict detection)
  - DeepSeek API calling helper
"""

import json
import re
import time
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

# ============================================================
# AI Prompts
# ============================================================

ANALYZE_PROMPT = """你是一个智能日程规划助手，服务于一位大学生（王世宇）。你不仅解析任务，还会根据任务类型智能估算耗时并建议开始时间。

用户背景：
- 大学生，有时区 UTC+8
- 常见任务类型及典型耗时参考：
  - MKT62704 课程作业（Part A/B/C）：2-4小时/部分
  - 论文写作：3-6小时
  - TikTok 达人筛选/运营：1-2小时
  - 课程预习/复习：1-2小时
  - 考试复习：2-4小时
  - 阅读文献/论文：1-2小时
  - 小组讨论/会议：0.5-1小时
  - 内容创作（小红书/TikTok）：1-2小时

必须严格按照以下JSON格式返回（只返回JSON，不要任何前缀、后缀或markdown标记）：
{"title":"精简标题","priority":"P1","priorityLabel":"紧急且重要","category":"学习","projectName":"建议项目名","dateISO":"2026-06-15","dateStr":"明天","startTime":"15:00","endTime":"17:00","durationMinutes":120,"durationStr":"2小时","location":"图书馆","reason":"分析理由","aiEstimatedDuration":120,"aiDurationReason":"该类型任务通常需要2小时","aiSuggestedStart":"建议周三下午开始，周四有课冲突"}

新增字段说明（智能估算）：
- aiEstimatedDuration: 根据任务类型和用户习惯估算的耗时（分钟），即使原文提到了时间也给出你的独立判断
- aiDurationReason: 简短说明为什么估算这个时长（如"MKT作业通常需要3小时"）
- aiSuggestedStart: 如果原文没有明确时间，给出一句话建议（如"建议今天下午开始，截止日期是周五"）

规则：
- 无法确定的字段填 null（不是字符串"null"）
- 时间用24小时制 HH:MM
- 中文数字时间要转换："九点半"→"09:30","下午三点"→"15:00"
- 无上下文的"X点到Y点"默认下午（13:00-23:00）
- "半小时"=30分钟，"两小时"=120分钟
- "明天/后天/下周X"需推算具体日期填入dateISO
- priority只返回P1/P2/P3/P4
- category只返回：学习/工作/项目/生活/内容创作/会议/提醒
- aiEstimatedDuration 必须是合理的数值，参考上面的典型耗时
- 今天是 __TODAY__，所有相对日期（明天/后天/下周X）都必须基于今天来计算"""

SCHEDULE_PROMPT = """你是一个智能排班助手。根据用户已有的日历事件和待安排的任务，为每个任务建议最佳时间。

当前已有的日历事件（这些时间段已被占用）：
{calendar_events}

待安排的任务列表（需排入空闲时段）：
{tasks_text}

用户背景：大学生，时区 UTC+8。偏好早上9点后开始，晚上10点前结束。喜欢连续工作不碎片化。

请为每个任务推荐一个最佳时间段。严格按以下JSON数组格式返回（只返回JSON数组）：
[
  {{"taskIndex":0,"title":"任务标题","suggestedDate":"2026-06-15","suggestedStart":"14:00","suggestedEnd":"16:30","durationMinutes":150,"reason":"周三下午空闲，放在周二作业之后"}},
  ...
]

排班规则：
- 优先安排 P1（紧急且重要）的任务
- 同类型任务尽量连续安排（如学习类任务集中处理）
- 避免在已有课程/会议前后安排高强度任务
- 每个任务之间留30分钟缓冲
- 如果一天排满了就推到下一天
- 一个任务块不超过3小时，超过则拆分为多天
- dateISO 必须是未来7天内的日期（今天是 {today}）
- reason 字段简要说明为何选择这个时间段"""

BATCH_EXTRACT_PROMPT = """你是一个严格的日程/课表解析助手。从以下 OCR 文档/图片文字中提取所有独立的任务、事件、课程、lecture、tutorial、assignment、quiz、deadline。

OCR 布局行（按 y 坐标聚合同一行、按 x 坐标排序，优先参考）：
{layout_text}

OCR 全文：
{raw_text}

请将每个独立的任务/事件/课程拆解出来，以JSON数组格式返回（只返回JSON数组，不要任何前缀、后缀或markdown标记）：
[
  {{"title":"精简标题","dateISO":"2026-06-19","dateStr":"周四","startTime":"09:00","endTime":"10:30","durationMinutes":90,"location":"教室A","category":"学习","projectName":"课程名称","reason":"周一第1节课"}},
  {{"title":"另一个任务","dateISO":"2026-06-19","dateStr":"周四","startTime":"14:00","endTime":"16:00","durationMinutes":120,"location":null,"category":"工作"}}
]

规则：
- 每个任务/课程必须独立一行
- 优先根据 OCR 布局行判断课程、日期、时间、section；布局行里的 y 坐标相近表示同一行，x 坐标接近表示同一格/同一列
- 如果 OCR 里出现类似 "[Tutorial] Class 01 Sec 08"、"[Lecture] Class 01 Sec 04"、"Final quiz"、"assignment due"，即使没有日期或时间，也必须作为一个任务输出
- 不允许因为日期/时间缺失而丢弃任务；无法确定的 dateISO/startTime/endTime/durationMinutes 填 null
- 不允许编造日期、时间、地点；只从 OCR 文字中明确出现的信息提取
- 没有明确日期/时间时必须填 null，不能输出 "?"，不能根据行号或课程顺序猜时间
- 如果一行只有课程名，没有时间，也输出该课程，reason 写 "OCR 中缺少日期/时间"
- 今天日期是 {today}，根据文档中的星期/日期推算出 dateISO
- 如果只有星期没有具体日期（如"周四"），推算最近的下一个该星期几
- 时间用24小时制 HH:MM
- 无法确定的字段填 null
- category只能是：学习/工作/项目/生活/内容创作/会议/提醒
- 从课表中提取的课程 category 应为"学习"
- 不要漏掉任何任务
- 如果 OCR 文本中存在任何疑似任务/课程，返回数组不能是空数组"""

STRICT_OCR_EXTRACT_PROMPT = """你是课表 OCR 纠错解析器。上一次解析返回了空结果，这是错误的。请只根据 OCR 原文强制提取候选课程/任务。

OCR 布局行（优先参考）：
{layout_text}

OCR 原文：
{raw_text}

输出要求：
- 只返回 JSON 数组，不要 markdown
- 每个 "[Tutorial]"、"[Lecture]"、"Class"、"Sec"、"quiz"、"assignment"、"due"、"deadline"、"meeting" 都应形成一个独立对象
- 没有明确日期/时间时，dateISO/startTime/endTime/durationMinutes 必须填 null，不要猜
- 没有明确日期/时间时不能输出 "?"、"未知"、"待定" 到时间字段
- title 保留课程/任务名称，例如 "[Tutorial] Class 01 Sec 08"
- category 对课程填 "学习"
- reason 说明信息来源或缺失项，例如 "OCR 中缺少日期/时间"

格式：
[
  {{"title":"[Tutorial] Class 01 Sec 08","dateISO":null,"dateStr":null,"startTime":null,"endTime":null,"durationMinutes":null,"location":null,"category":"学习","projectName":"Class 01","reason":"OCR 中缺少日期/时间"}}
]
"""

# ============================================================
# Keyword-based fallback analysis
# ============================================================

CAT_KEYWORDS = {
    "学习": ["学习", "作业", "课程", "考试", "阅读", "复习", "论文", "课题", "笔记", "预习", "听课", "上课"],
    "工作": ["工作", "实习", "客户", "达人", "运营", "筛选", "面试", "简历", "求职", "报告", "汇报", "数据"],
    "项目": ["项目", "创业", "自媒体", "网站", "开发", "上线", "产品", "方案"],
    "生活": ["签证", "购物", "出行", "旅游", "搬家", "打扫", "做饭", "买菜", "账单", "房租"],
    "内容创作": ["小红书", "抖音", "TikTok", "视频", "脚本", "内容创作", "剪辑", "拍摄", "发布", "公众号"],
    "会议": ["会议", "开会", "沟通", "讨论", "面谈", "小组", "约谈", "同步"],
    "提醒": ["提醒", "记得", "别忘了", "备忘", "不要忘"],
}


def keyword_analyze(text: str, cat_keywords: dict | None = None) -> dict:
    """Keyword-based fallback analysis when AI is unavailable.

    Returns a dict with status, source, and analysis fields.
    """
    if cat_keywords is None:
        cat_keywords = CAT_KEYWORDS

    matched_cat = ""
    max_score = 0
    for cat, keywords in cat_keywords.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > max_score:
            max_score = score
            matched_cat = cat

    return {
        "status": "ok",
        "source": "keyword",
        "analysis": {
            "title": text[:40],
            "priority": "P2",
            "priorityLabel": "重要但不紧急",
            "category": matched_cat or "工作",
            "projectName": matched_cat + "相关任务" if matched_cat else "待分类",
            "reason": "关键词匹配（离线模式）",
        },
    }


# ============================================================
# JSON extraction from LLM responses
# ============================================================

def extract_json(content: str):
    """Robust JSON extraction from LLM response.

    Handles markdown code fences, extra text, and common formatting issues.
    Returns a dict/list or None.
    """
    # Strategy 1: Remove ```json ... ``` blocks
    md_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
    if md_match:
        content = md_match.group(1).strip()

    # Strategy 2: Find the first valid { or [ with matching close
    for open_char, close_char in [("[", "]"), ("{", "}")]:
        start = content.find(open_char)
        if start == -1:
            continue
        depth = 0
        end = -1
        for i in range(start, len(content)):
            if content[i] == open_char:
                depth += 1
            elif content[i] == close_char:
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            continue

        json_str = content[start : end + 1]
        # Fix common issues
        json_str = json_str.replace(": None", ": null").replace(": True", ": true").replace(": False", ": false")
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            continue

    return None


def extract_json_array(content: str) -> list:
    """Extract a JSON array from LLM response. Returns a list (may be empty)."""
    result = extract_json(content)
    if isinstance(result, list):
        return result
    return []


# ============================================================
# Board time helpers (shared between server.py and api/index.py)
# ============================================================

def parse_board_minutes(value) -> int | None:
    """Convert HH:MM to minutes after midnight. Returns None on invalid input."""
    if not value or not isinstance(value, str):
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})$", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def format_board_minutes(minutes: int) -> str:
    """Convert minutes after midnight to HH:MM string."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_board_task_window(task: dict) -> tuple[str | None, int | None, int | None, str]:
    """Extract and validate a task's date/time window.

    Returns (dateISO, start_minute, end_minute, message).
    message is empty on success, or an error description.
    """
    date_iso = task.get("dateISO") or task.get("date")
    start = parse_board_minutes(task.get("startTime") or task.get("start"))
    end = parse_board_minutes(task.get("endTime") or task.get("end"))
    duration = task.get("durationMinutes")

    if start is not None and end is None and duration:
        try:
            end = start + int(duration)
        except (TypeError, ValueError):
            end = None

    if not date_iso:
        return None, None, None, "任务缺少日期，无法检测冲突"
    if start is None:
        return date_iso, None, None, "任务缺少开始时间，无法检测冲突"
    if end is None:
        return date_iso, start, None, "任务缺少结束时间或预计用时，无法检测冲突"
    if end <= start:
        return date_iso, start, end, "任务结束时间必须晚于开始时间"

    return date_iso, start, end, ""


def find_board_recommendations(
    task: dict,
    existing_tasks: list[dict],
    days: int = 7,
    work_start: int = 9 * 60,
    work_end: int = 22 * 60,
    max_results: int = 3,
) -> list[dict]:
    """Find recommended time slots for a task that conflicts with existing ones.

    Returns a list of recommendation dicts with dateISO, startTime, endTime,
    durationMinutes, and reason fields.
    """
    date_iso, start, end, message = normalize_board_task_window(task)
    if message:
        return []

    duration = end - start
    try:
        base_day = datetime.strptime(date_iso[:10], "%Y-%m-%d")
    except ValueError:
        return []

    recommendations = []

    for offset in range(max(1, min(days, 14))):
        day = base_day + timedelta(days=offset)
        day_iso = day.strftime("%Y-%m-%d")
        occupied = []

        for existing in existing_tasks:
            ex_date, ex_start, ex_end, ex_message = normalize_board_task_window(existing)
            if ex_message or ex_date != day_iso:
                continue
            occupied.append((max(work_start, ex_start), min(work_end, ex_end), existing))

        occupied.sort(key=lambda item: item[0])
        merged = []
        for occ_start, occ_end, existing in occupied:
            if occ_end <= work_start or occ_start >= work_end:
                continue
            if not merged or occ_start > merged[-1][1]:
                merged.append([occ_start, occ_end, [existing]])
            else:
                merged[-1][1] = max(merged[-1][1], occ_end)
                merged[-1][2].append(existing)

        cursor = work_start
        gaps = []
        for occ_start, occ_end, _items in merged:
            if cursor + duration <= occ_start:
                gaps.append((cursor, occ_start))
            cursor = max(cursor, occ_end)
        if cursor + duration <= work_end:
            gaps.append((cursor, work_end))

        for gap_start, gap_end in gaps:
            suggested_start = (
                max(gap_start, start)
                if offset == 0 and start >= gap_start and start + duration <= gap_end
                else gap_start
            )
            suggested_end = suggested_start + duration
            if suggested_end > gap_end:
                continue

            if not occupied:
                reason = (
                    f"这一天看板没有其他任务，{format_board_minutes(suggested_start)}-"
                    f"{format_board_minutes(suggested_end)} 满足 {duration} 分钟连续时长"
                )
            else:
                avoided = "、".join(
                    f"{format_board_minutes(s)}-{format_board_minutes(e)}"
                    for s, e, _ in merged[:3]
                )
                reason = f"避开了已有任务时段 {avoided}，并保留 {duration} 分钟连续空闲"

            recommendations.append({
                "dateISO": day_iso,
                "startTime": format_board_minutes(suggested_start),
                "endTime": format_board_minutes(suggested_end),
                "durationMinutes": duration,
                "reason": reason,
            })
            if len(recommendations) >= max_results:
                return recommendations

    return recommendations


# ============================================================
# DeepSeek API helper
# ============================================================

def call_deepseek(
    api_key: str,
    messages: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 300,
    timeout: int = 15,
    model: str = "deepseek-chat",
) -> str:
    """Call DeepSeek API and return the response content string."""
    req = Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    resp = urlopen(req, timeout=timeout)
    result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"].strip()


# ============================================================
# OCR layout formatting (used by api/index.py)
# ============================================================

def format_ocr_layout_lines(layout_lines: list | None, limit: int = 120) -> str:
    """Format OCR layout lines for LLM input / debug previews."""
    if not layout_lines:
        return "(无结构化布局行)"
    rows = []
    for item in layout_lines[:limit]:
        rows.append(
            f"[{item['line']:03d} y={item['y']} x={item['x']}] {item['text']}"
        )
    if len(layout_lines) > limit:
        rows.append(f"... 其余 {len(layout_lines) - limit} 行已省略")
    return "\n".join(rows)
