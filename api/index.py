"""
Calendar Planner MVP — Vercel Serverless Backend
Handles: AI analysis (DeepSeek), Google Calendar, file upload/OCR
"""
import base64, json, hashlib, os, re, time, io
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, request, jsonify, Response, send_from_directory

app = Flask(__name__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# Environment variables (set in Vercel dashboard)
# ============================================================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")

# ============================================================
# Prompts (from server.py)
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
- aiEstimatedDuration 必须是合理的数值，参考上面的典型耗时"""

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
# Google Calendar helpers
# ============================================================
_google_creds = None
_google_last_error = ""


def _get_google_creds():
    """Get or refresh Google Calendar credentials."""
    global _google_creds, _google_last_error
    if _google_creds and _google_creds.valid:
        return _google_creds

    cid = GOOGLE_CLIENT_ID
    csecret = GOOGLE_CLIENT_SECRET
    rtoken = GOOGLE_REFRESH_TOKEN
    
    # Also try reading full token JSON from env
    google_token_json = os.environ.get("GOOGLE_TOKEN_JSON", "")

    if not all([cid, csecret, rtoken]):
        _google_last_error = f"Missing env vars: CLIENT_ID={'✓' if cid else '✗'}, CLIENT_SECRET={'✓' if csecret else '✗'}, REFRESH_TOKEN={'✓' if rtoken else '✗'}"
        return None

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request as GRequest
    
    # Try with the stored access token if available (from full JSON)
    stored_token = None
    if google_token_json:
        try:
            token_data = json.loads(google_token_json)
            stored_token = token_data.get("token")
        except json.JSONDecodeError:
            pass

    creds = Credentials(
        token=stored_token,
        refresh_token=rtoken,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cid,
        client_secret=csecret,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    try:
        import traceback
        # Only refresh if token is expired or missing
        if not creds.valid:
            creds.refresh(GRequest())
        _google_creds = creds
        _google_last_error = ""
        return creds
    except Exception as e:
        _google_last_error = f"{type(e).__name__}: {e}"
        print(f"[GOOGLE] Failed to refresh token: {_google_last_error}\n{traceback.format_exc()}")
        return None


def _read_calendar_events(days=14):
    """Read upcoming calendar events."""
    creds = _get_google_creds()
    if not creds:
        return []

    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=creds)
    now = datetime.utcnow()
    end = now + timedelta(days=days)

    try:
        result = service.events().list(
            calendarId="primary",
            timeMin=now.isoformat() + "Z",
            timeMax=end.isoformat() + "Z",
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
        events = result.get("items", [])
        simplified = []
        for e in events:
            start = e.get("start", {})
            end_dt = e.get("end", {})
            simplified.append({
                "id": e.get("id", ""),
                "summary": e.get("summary", "无标题"),
                "start": start.get("dateTime", start.get("date", "")),
                "end": end_dt.get("dateTime", end_dt.get("date", "")),
            })
        return simplified
    except Exception as e:
        print(f"[CALENDAR] Read error: {e}")
        return []


def _create_calendar_event(summary, start_dt, end_dt, description="", location=""):
    """Create a Google Calendar event."""
    creds = _get_google_creds()
    if not creds:
        return {"status": "error", "message": "Google 日历未连接，请配置 GOOGLE_REFRESH_TOKEN"}

    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=creds)

    event_body = {
        "summary": summary,
        "start": {"dateTime": start_dt, "timeZone": "Asia/Shanghai"},
        "end": {"dateTime": end_dt, "timeZone": "Asia/Shanghai"},
    }
    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location

    try:
        event = service.events().insert(calendarId="primary", body=event_body).execute()
        return {
            "status": "created",
            "id": event.get("id", ""),
            "summary": event.get("summary", summary),
            "htmlLink": event.get("htmlLink", ""),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# DeepSeek helpers
# ============================================================
def _call_deepseek(messages, temperature=0.1, max_tokens=300, timeout=15):
    """Call DeepSeek API and return response content."""
    req = Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps({
            "model": "deepseek-chat",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode(),
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    resp = urlopen(req, timeout=timeout)
    result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"].strip()


def _format_ocr_layout_lines(layout_lines, limit=120):
    """Format OCR layout lines for LLM input/debug previews."""
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


def _extract_tasks_with_deepseek(text, layout_lines=None):
    """Extract task list with DeepSeek, retrying once with OCR-specific instructions."""
    today = time.strftime("%Y-%m-%d")
    layout_text = _format_ocr_layout_lines(layout_lines or [], limit=120)
    prompt = BATCH_EXTRACT_PROMPT.format(
        raw_text=text[:6000],
        layout_text=layout_text[:6000],
        today=today,
    )
    content = _call_deepseek(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请提取所有任务。即使日期或时间缺失，也要输出课程/任务对象，未知字段填 null。"}
        ],
        temperature=0.1,
        max_tokens=3000,
        timeout=30,
    )
    tasks = _extract_json(content)
    if isinstance(tasks, list) and tasks:
        return tasks, "ai"

    retry_prompt = STRICT_OCR_EXTRACT_PROMPT.format(
        raw_text=text[:6000],
        layout_text=layout_text[:6000],
    )
    retry_content = _call_deepseek(
        messages=[
            {"role": "system", "content": retry_prompt},
            {"role": "user", "content": "严格按 JSON 数组输出候选课程/任务，不要返回空数组。"}
        ],
        temperature=0,
        max_tokens=3000,
        timeout=30,
    )
    retry_tasks = _extract_json(retry_content)
    if isinstance(retry_tasks, list):
        return retry_tasks, "ai_retry"
    return [], "ai_empty"


def _extract_json(content):
    """Robust JSON extraction from LLM response."""
    # Strategy 1: Remove ```json ... ``` blocks
    md_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
    if md_match:
        content = md_match.group(1).strip()

    # Strategy 2: Find the first { or [ and matching close
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
        if end != -1:
            json_str = content[start:end + 1]
            # Fix common issues
            json_str = json_str.replace(': None', ': null').replace(': True', ': true').replace(': False', ': false')
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                continue

    return None


def _vertex_xy(vertex):
    return vertex.get("x", 0), vertex.get("y", 0)


def _word_from_vision(word):
    text = "".join(symbol.get("text", "") for symbol in word.get("symbols", []))
    vertices = (word.get("boundingBox") or {}).get("vertices") or []
    if not text or not vertices:
        return None
    xs, ys = zip(*[_vertex_xy(vertex) for vertex in vertices])
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return {
        "text": text,
        "x": min_x,
        "y": min_y,
        "right": max_x,
        "bottom": max_y,
        "center_y": (min_y + max_y) / 2,
        "height": max(1, max_y - min_y),
    }


def _extract_vision_layout_lines(response):
    """Rebuild approximate OCR lines from Google Vision word coordinates."""
    words = []
    annotation = response.get("fullTextAnnotation") or {}
    for page in annotation.get("pages", []) or []:
        for block in page.get("blocks", []) or []:
            for paragraph in block.get("paragraphs", []) or []:
                for word in paragraph.get("words", []) or []:
                    parsed = _word_from_vision(word)
                    if parsed:
                        words.append(parsed)

    if not words:
        raw_text = annotation.get("text", "")
        return [
            {
                "line": i + 1,
                "text": line.strip(),
                "x": 0,
                "y": i,
                "width": 0,
                "height": 0,
            }
            for i, line in enumerate(raw_text.splitlines())
            if line.strip()
        ]

    heights = sorted(word["height"] for word in words)
    median_height = heights[len(heights) // 2] if heights else 16
    y_tolerance = max(10, int(median_height * 0.75))

    lines = []
    for word in sorted(words, key=lambda item: (item["center_y"], item["x"])):
        target = None
        for line in lines:
            if abs(word["center_y"] - line["center_y"]) <= y_tolerance:
                target = line
                break
        if target is None:
            target = {"center_y": word["center_y"], "words": []}
            lines.append(target)
        target["words"].append(word)
        count = len(target["words"])
        target["center_y"] = ((target["center_y"] * (count - 1)) + word["center_y"]) / count

    layout_lines = []
    for idx, line in enumerate(sorted(lines, key=lambda item: item["center_y"]), start=1):
        line_words = sorted(line["words"], key=lambda item: item["x"])
        text = " ".join(word["text"] for word in line_words).strip()
        min_x = min(word["x"] for word in line_words)
        max_x = max(word["right"] for word in line_words)
        min_y = min(word["y"] for word in line_words)
        max_y = max(word["bottom"] for word in line_words)
        if text:
            layout_lines.append({
                "line": idx,
                "text": text,
                "x": int(round(min_x)),
                "y": int(round(min_y)),
                "width": int(round(max_x - min_x)),
                "height": int(round(max_y - min_y)),
            })
    return layout_lines


def _call_google_vision_ocr(file_data):
    """Extract text from an image with Google Cloud Vision OCR."""
    if not GOOGLE_VISION_API_KEY:
        raise RuntimeError("未配置 GOOGLE_VISION_API_KEY，无法使用图片 OCR")
    if len(file_data) > 10 * 1024 * 1024:
        raise RuntimeError("图片太大，请压缩到 10MB 以内后再上传")

    body = {
        "requests": [
            {
                "image": {
                    "content": base64.b64encode(file_data).decode("ascii")
                },
                "features": [
                    {"type": "DOCUMENT_TEXT_DETECTION"}
                ],
                "imageContext": {
                    "languageHints": ["zh", "en"]
                }
            }
        ]
    }
    req = Request(
        "https://vision.googleapis.com/v1/images:annotate?key=" + quote(GOOGLE_VISION_API_KEY),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    try:
        resp = urlopen(req, timeout=30)
        result = json.loads(resp.read())
    except HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:
            detail = str(e)
        raise RuntimeError("Google Vision OCR 失败：" + (detail or str(e)))

    response = (result.get("responses") or [{}])[0]
    if response.get("error"):
        raise RuntimeError("Google Vision OCR 失败：" + response["error"].get("message", "未知错误"))

    full_text = (response.get("fullTextAnnotation") or {}).get("text", "")
    if not full_text and response.get("textAnnotations"):
        full_text = response["textAnnotations"][0].get("description", "")
    layout_lines = _extract_vision_layout_lines(response)
    return {
        "raw_text": full_text.strip(),
        "layout_lines": layout_lines,
        "layout_preview": _format_ocr_layout_lines(layout_lines, limit=80),
    }


def _keyword_analyze(text):
    """Keyword-based fallback analysis (no AI needed)."""
    cat_keywords = {
        "学习": ["学习", "作业", "课程", "考试", "阅读", "复习", "论文", "课题", "笔记", "预习", "听课", "上课"],
        "工作": ["工作", "实习", "客户", "达人", "运营", "筛选", "面试", "简历", "求职", "报告", "汇报", "数据"],
        "项目": ["项目", "创业", "自媒体", "网站", "开发", "上线", "产品", "方案"],
        "生活": ["签证", "购物", "出行", "旅游", "搬家", "打扫", "做饭", "买菜", "账单", "房租"],
        "内容创作": ["小红书", "抖音", "TikTok", "视频", "脚本", "内容创作", "剪辑", "拍摄", "发布", "公众号"],
        "会议": ["会议", "开会", "沟通", "讨论", "面谈", "小组", "约谈", "同步"],
        "提醒": ["提醒", "记得", "别忘了", "备忘", "不要忘"],
    }
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
            "reason": "关键词匹配（离线模式）"
        }
    }


# ============================================================
# CORS helper
# ============================================================
def _cors_response(data, status=200):
    """JSON response with CORS headers."""
    resp = jsonify(data)
    resp.status_code = status
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ============================================================
# Routes
# ============================================================
@app.route("/", methods=["GET"])
def web_index():
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/<path:path>", methods=["GET"])
def web_fallback(path):
    file_path = os.path.join(PROJECT_ROOT, path)
    if os.path.isfile(file_path):
        return send_from_directory(PROJECT_ROOT, path)
    return send_from_directory(PROJECT_ROOT, "index.html")


@app.route("/api/analyze", methods=["POST", "OPTIONS"])
def api_analyze():
    if request.method == "OPTIONS":
        return _cors_response({})

    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return _cors_response({"status": "error", "message": "Empty text"}, 400)

    if not DEEPSEEK_API_KEY:
        return _cors_response(_keyword_analyze(text))

    try:
        content = _call_deepseek(
            messages=[
                {"role": "system", "content": ANALYZE_PROMPT},
                {"role": "user", "content": f"分析这个任务：{text}"}
            ],
            temperature=0.1,
            max_tokens=300,
        )
        analysis = _extract_json(content)
        if analysis:
            return _cors_response({"status": "ok", "analysis": analysis})
        else:
            return _cors_response(_keyword_analyze(text))
    except Exception as e:
        print(f"[ANALYZE] Error: {e}")
        return _cors_response(_keyword_analyze(text))


@app.route("/api/calendar", methods=["POST", "OPTIONS"])
def api_calendar():
    if request.method == "OPTIONS":
        return _cors_response({})

    data = request.get_json(silent=True) or {}
    summary = data.get("summary", "").strip()
    start_dt = data.get("start")
    end_dt = data.get("end")
    description = data.get("description", "")
    location = data.get("location", "")

    if not summary:
        return _cors_response({"status": "error", "message": "Missing: summary"}, 400)
    if not start_dt or not end_dt:
        return _cors_response({"status": "error", "message": "Missing: start/end time"}, 400)

    result = _create_calendar_event(summary, start_dt, end_dt, description, location)
    code = 200 if result.get("status") == "created" else 500
    return _cors_response(result, code)


@app.route("/api/calendar/read", methods=["GET", "OPTIONS"])
def api_calendar_read():
    if request.method == "OPTIONS":
        return _cors_response({})

    days = request.args.get("days", 14, type=int)
    events = _read_calendar_events(days)
    return _cors_response({"status": "ok", "events": events, "count": len(events)})


@app.route("/api/schedule", methods=["POST", "OPTIONS"])
def api_schedule():
    if request.method == "OPTIONS":
        return _cors_response({})

    data = request.get_json(silent=True) or {}
    tasks = data.get("tasks", [])
    if not tasks:
        return _cors_response({"status": "error", "message": "No tasks provided"}, 400)

    if not DEEPSEEK_API_KEY:
        return _cors_response({
            "status": "ok", "source": "keyword",
            "schedule": [], "message": "需要 DeepSeek API Key 才能智能排班"
        })

    # Read calendar events
    events = _read_calendar_events(14)

    # Format calendar events
    events_text = ""
    for e in events:
        summary = e.get("summary", "无标题")
        start = e.get("start", "")
        end = e.get("end", "")
        if "T" in start:
            date_part = start[:10]
            start_time = start[11:16]
            end_time = end[11:16] if "T" in end else "?"
            events_text += f"  {date_part} {start_time}-{end_time}: {summary}\n"
        else:
            events_text += f"  {start} (全天): {summary}\n"

    # Format tasks
    tasks_text = ""
    for i, t in enumerate(tasks):
        title = t.get("title", f"任务{i+1}")
        priority = t.get("priority", "P3")
        category = t.get("category", "")
        deadline = t.get("deadline", "")
        tasks_text += f"  [{i}] {title} | 等级:{priority} | 分类:{category}"
        if deadline:
            tasks_text += f" | 截止:{deadline}"
        tasks_text += "\n"

    today = time.strftime("%Y-%m-%d")
    prompt = SCHEDULE_PROMPT.format(
        calendar_events=events_text or "（暂无日历事件）",
        tasks_text=tasks_text,
        today=today
    )

    try:
        content = _call_deepseek(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "请为这些任务安排最佳时间"}
            ],
            temperature=0.3,
            max_tokens=2000,
            timeout=30,
        )
        schedule = _extract_json(content)
        if schedule and isinstance(schedule, list):
            return _cors_response({"status": "ok", "source": "ai", "schedule": schedule})
        else:
            return _cors_response({"status": "ok", "source": "parse_error", "schedule": [], "raw": content[:500]})
    except Exception as e:
        return _cors_response({"status": "error", "message": str(e)}, 500)


@app.route("/api/upload", methods=["POST", "OPTIONS"])
def api_upload():
    if request.method == "OPTIONS":
        return _cors_response({})

    if "file" not in request.files:
        return _cors_response({"status": "error", "message": "No file uploaded"}, 400)

    file = request.files["file"]
    filename = file.filename or "unknown"
    file_data = file.read()

    # Extract text from file
    ocr_payload = {"raw_text": "", "layout_lines": [], "layout_preview": ""}
    text = ""
    suffix = os.path.splitext(filename)[1].lower()

    if suffix == ".txt":
        text = file_data.decode("utf-8", errors="replace")
        ocr_payload = {
            "raw_text": text,
            "layout_lines": [],
            "layout_preview": "",
        }
    elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"):
        try:
            ocr_payload = _call_google_vision_ocr(file_data)
            text = ocr_payload.get("raw_text", "")
        except Exception as e:
            return _cors_response({
                "status": "error",
                "message": str(e)
            }, 400)
    elif suffix == ".pdf":
        return _cors_response({
            "status": "error",
            "message": "PDF OCR 暂不支持。请先上传课表截图/JPG/PNG，或复制 PDF 文字直接粘贴。"
        }, 400)
    else:
        # Try as text
        try:
            text = file_data.decode("utf-8", errors="replace")
            ocr_payload = {
                "raw_text": text,
                "layout_lines": [],
                "layout_preview": "",
            }
        except Exception:
            return _cors_response({"status": "error", "message": f"不支持的文件格式: {suffix}"}, 400)

    if not text.strip():
        return _cors_response({
            "status": "ok", "source": "keyword",
            "raw_text": "", "tasks": [],
            "message": "文件中没有识别出文字内容"
        })

    if not DEEPSEEK_API_KEY:
        return _cors_response({
            "status": "error",
            "source": "ocr",
            "raw_text": text[:5000],
            "ocr_layout_preview": ocr_payload.get("layout_preview", ""),
            "total_chars": len(text),
            "tasks": [],
            "message": "OCR 已识别文字，但必须配置 DEEPSEEK_API_KEY 才能解析任务。"
        }, 500)

    try:
        tasks, source = _extract_tasks_with_deepseek(text, ocr_payload.get("layout_lines", []))
        if not tasks:
            return _cors_response({
                "status": "error",
                "source": source,
                "raw_text": text[:5000],
                "ocr_layout_preview": ocr_payload.get("layout_preview", ""),
                "total_chars": len(text),
                "tasks": [],
                "message": "DeepSeek 两次解析后仍未能从 OCR 布局/文字中解析出任务。请查看下方 OCR 布局预览，确认课表区域是否被清晰识别。"
            }, 422)
        return _cors_response({
            "status": "ok",
            "source": source,
            "raw_text": text[:5000],
            "ocr_layout_preview": ocr_payload.get("layout_preview", ""),
            "total_chars": len(text),
            "tasks": tasks,
            "message": f"共识别 {len(text)} 字，DeepSeek 提取 {len(tasks)} 个任务"
        })
    except Exception as e:
        return _cors_response({
            "status": "error",
            "source": "ai",
            "raw_text": text[:5000],
            "ocr_layout_preview": ocr_payload.get("layout_preview", ""),
            "total_chars": len(text),
            "tasks": [],
            "message": "DeepSeek 解析失败：" + str(e)
        }, 500)


# ============================================================
# Health check
# ============================================================
@app.route("/api/health")
def api_health():
    gc_status = False
    gc_error = _google_last_error or ""
    try:
        gc_status = bool(_get_google_creds())
        if not gc_status and not gc_error:
            gc_error = _google_last_error or "unknown"
    except Exception as e:
        gc_error = str(e)
    return _cors_response({
        "status": "ok",
        "deepseek": bool(DEEPSEEK_API_KEY),
        "google_vision_ocr": bool(GOOGLE_VISION_API_KEY),
        "google_calendar": gc_status,
        "google_error": gc_error,
        "google_debug": {
            "client_id_set": bool(GOOGLE_CLIENT_ID),
            "client_secret_len": len(GOOGLE_CLIENT_SECRET),
            "client_secret_hash": hashlib.md5(GOOGLE_CLIENT_SECRET.encode()).hexdigest()[:8] if GOOGLE_CLIENT_SECRET else "N/A",
            "refresh_token_len": len(GOOGLE_REFRESH_TOKEN),
            "refresh_token_first10": GOOGLE_REFRESH_TOKEN[:10] if GOOGLE_REFRESH_TOKEN else "N/A",
        },
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/history/log", methods=["POST", "OPTIONS"])
def api_history_log():
    """Log task history (stub — Vercel has no persistent storage)."""
    if request.method == "OPTIONS":
        return _cors_response({})
    data = request.get_json(silent=True) or {}
    task_id = f"task_{int(time.time() * 1000)}"
    print(f"[HISTORY] Logged: {data.get('event_type', 'unknown')} — {data.get('title', 'untitled')}")
    return _cors_response({"status": "ok", "task_id": task_id})


@app.route("/api/profile", methods=["GET", "OPTIONS"])
def api_profile():
    """Task profile stats (stub — Vercel has no persistent storage)."""
    if request.method == "OPTIONS":
        return _cors_response({})
    return _cors_response({
        "status": "ok",
        "stats": {
            "total_tasks": 0,
            "hour_heatmap": {},
            "by_category": {},
            "message": "分析功能需要本地桥接服务器。在 Vercel 上暂不可用。"
        }
    })


# Vercel Python entry point
# Flask app is auto-detected as 'app'
