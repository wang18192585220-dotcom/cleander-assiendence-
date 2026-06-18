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

import work_profile
import common

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
# Prompts now in common.py:
# common.common.ANALYZE_PROMPT, common.common.SCHEDULE_PROMPT,
# common.common.BATCH_EXTRACT_PROMPT, common.common.STRICT_OCR_EXTRACT_PROMPT


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
    """Call DeepSeek API — thin wrapper around common.call_deepseek."""
    return common.call_deepseek(DEEPSEEK_API_KEY, messages, temperature, max_tokens, timeout)


# _format_ocr_layout_lines → common.format_ocr_layout_lines


def _extract_tasks_with_deepseek(text, layout_lines=None):
    """Extract task list with DeepSeek, retrying once with OCR-specific instructions."""
    today = time.strftime("%Y-%m-%d")
    layout_text = common.format_ocr_layout_lines(layout_lines or [], limit=120)
    prompt = common.BATCH_EXTRACT_PROMPT.format(
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
    tasks = common.extract_json(content)
    if isinstance(tasks, list) and tasks:
        return tasks, "ai"

    retry_prompt = common.STRICT_OCR_EXTRACT_PROMPT.format(
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
    retry_tasks = common.extract_json(retry_content)
    if isinstance(retry_tasks, list):
        return retry_tasks, "ai_retry"
    return [], "ai_empty"


# _extract_json → common.extract_json


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
        "layout_preview": common.format_ocr_layout_lines(layout_lines, limit=80),
    }


# keyword_analyze → common.keyword_analyze
# parse_board_minutes → common.parse_board_minutes
# format_board_minutes → common.format_board_minutes
# normalize_board_task_window → common.normalize_board_task_window
# find_board_recommendations → common.find_board_recommendations


# ============================================================
# CORS helper


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
        return _cors_response(common.keyword_analyze(text))

    try:
        today = time.strftime("%Y-%m-%d")
        content = _call_deepseek(
            messages=[
                {"role": "system", "content": common.ANALYZE_PROMPT.replace('__TODAY__', today)},
                {"role": "user", "content": f"分析这个任务：{text}"}
            ],
            temperature=0.1,
            max_tokens=300,
        )
        analysis = common.extract_json(content)
        if analysis:
            return _cors_response({"status": "ok", "analysis": analysis})
        else:
            return _cors_response(common.keyword_analyze(text))
    except Exception as e:
        print(f"[ANALYZE] Error: {e}")
        return _cors_response(common.keyword_analyze(text))


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


@app.route("/api/board/conflicts", methods=["POST", "OPTIONS"])
def api_board_conflicts():
    if request.method == "OPTIONS":
        return _cors_response({})

    data = request.get_json(silent=True) or {}
    task = data.get("task") or {}
    existing_tasks = data.get("existingTasks") or []
    days = data.get("days", 7)

    date_iso, start, end, message = common.normalize_board_task_window(task)
    if message:
        return _cors_response({
            "status": "error",
            "message": message,
            "hasConflict": False,
            "conflicts": [],
            "recommendations": [],
        }, 400)

    conflicts = []
    for existing in existing_tasks:
        ex_date, ex_start, ex_end, ex_message = common.normalize_board_task_window(existing)
        if ex_message or ex_date != date_iso:
            continue
        if start < ex_end and end > ex_start:
            conflicts.append({
                "id": existing.get("id", ""),
                "title": existing.get("title", "未命名任务"),
                "dateISO": ex_date,
                "startTime": common.format_board_minutes(ex_start),
                "endTime": common.format_board_minutes(ex_end),
            })

    recommendations = common.find_board_recommendations(task, existing_tasks, days)
    return _cors_response({
        "status": "ok",
        "hasConflict": bool(conflicts),
        "conflicts": conflicts,
        "recommendations": recommendations,
        "message": "发现时间冲突，请选择推荐时间或强行添加" if conflicts else "该时间段没有冲突",
    })


@app.route("/api/work-profile", methods=["POST", "OPTIONS"])
def api_work_profile():
    if request.method == "OPTIONS":
        return _cors_response({})
    data = request.get_json(silent=True) or {}
    try:
        profile = work_profile.build_profile(
            data.get("tasks") or [],
            period_type=data.get("periodType", "week"),
            start_date=data.get("startDate"),
            end_date=data.get("endDate"),
            project_name=data.get("projectName", ""),
            status=data.get("status", "all"),
        )
        return _cors_response({"status": "ok", **profile})
    except Exception as e:
        return _cors_response({"status": "error", "message": str(e)}, 500)


@app.route("/api/work-profile/daily-summary", methods=["POST", "OPTIONS"])
def api_work_profile_daily_summary():
    if request.method == "OPTIONS":
        return _cors_response({})
    data = request.get_json(silent=True) or {}
    profile = data.get("profile") or work_profile.build_profile(
        data.get("tasks") or [],
        period_type="day",
        start_date=data.get("date") or data.get("startDate"),
        end_date=data.get("endDate"),
    )
    return _cors_response({
        "status": "ok",
        "source": "rule_fallback",
        "summary": work_profile.rule_summary(profile, summary_type="daily"),
    })


@app.route("/api/work-profile/yearly-summary", methods=["POST", "OPTIONS"])
def api_work_profile_yearly_summary():
    if request.method == "OPTIONS":
        return _cors_response({})
    data = request.get_json(silent=True) or {}
    profile = data.get("profile") or work_profile.build_profile(
        data.get("tasks") or [],
        period_type="year",
        start_date=data.get("startDate"),
        end_date=data.get("endDate"),
    )
    return _cors_response({
        "status": "ok",
        "source": "rule_fallback",
        "summary": work_profile.rule_summary(profile, summary_type="yearly"),
    })


@app.route("/api/tasks/reschedule-suggestions", methods=["POST", "OPTIONS"])
def api_task_reschedule_suggestions():
    if request.method == "OPTIONS":
        return _cors_response({})
    data = request.get_json(silent=True) or {}
    suggestions = work_profile.reschedule_suggestions(data.get("task") or {}, data.get("tasks") or [])
    return _cors_response({"status": "ok", "suggestions": suggestions})


@app.route("/api/tasks/accept-reschedule", methods=["POST", "OPTIONS"])
def api_accept_reschedule():
    if request.method == "OPTIONS":
        return _cors_response({})
    data = request.get_json(silent=True) or {}
    task = data.get("task") or {}
    suggestion = data.get("suggestion") or {}
    if not task or not suggestion:
        return _cors_response({"status": "error", "message": "Missing task or suggestion"}, 400)
    original_start = task.get("originalScheduledStart") or f"{task.get('dateISO', '')}T{task.get('startTime', '')}"
    original_end = task.get("originalScheduledEnd") or f"{task.get('dateISO', '')}T{task.get('endTime', '')}"
    patch = {
        "dateISO": suggestion.get("suggestedDate"),
        "startTime": suggestion.get("suggestedStart"),
        "endTime": suggestion.get("suggestedEnd"),
        "originalScheduledStart": original_start,
        "originalScheduledEnd": original_end,
        "rescheduleCount": int(task.get("rescheduleCount") or 0) + 1,
        "status": "planned",
        "rescheduleReason": suggestion.get("reason", ""),
    }
    return _cors_response({"status": "ok", "taskId": task.get("id"), "patch": patch})


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
    prompt = common.SCHEDULE_PROMPT.format(
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
        schedule = common.extract_json(content)
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
