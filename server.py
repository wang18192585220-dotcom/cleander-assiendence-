#!/usr/bin/env python3
"""Bridge server: AI analysis + Google Calendar integration."""

import json
import subprocess
import sys
import os
import time
import hashlib
import urllib.request
import tempfile
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from http.server import HTTPServer, BaseHTTPRequestHandler

import user_profile
import work_profile
import common

HERMES_HOME = "/Users/wangshiyu/.hermes/profiles/energy-management"
GAPI = os.path.join(HERMES_HOME, "skills/productivity/google-workspace/scripts/google_api.py")
HERMES_PYTHON = "/Users/wangshiyu/.hermes/hermes-agent/venv/bin/python"

# Read DeepSeek API key from .env
ENV_PATH = os.path.join(HERMES_HOME, ".env")
DEEPSEEK_API_KEY = ""
try:
    with open(ENV_PATH) as f:
        for line in f:
            if line.startswith("DEEPSEEK_API_KEY="):
                DEEPSEEK_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
except FileNotFoundError:
    pass

PORT = 8765

# ==================== Task History Database ====================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_history.db")

def _init_db():
    """Initialize SQLite database with tasks and events tables."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT,
            priority TEXT,
            project_name TEXT,
            scheduled_start TEXT,
            scheduled_end TEXT,
            estimated_duration_minutes INTEGER,
            actual_duration_minutes INTEGER,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT REFERENCES tasks(id),
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON task_events(event_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_events_created ON task_events(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    c.execute("PRAGMA table_info(tasks)")
    existing_cols = {row[1] for row in c.fetchall()}
    task_columns = {
        "task_type": "TEXT",
        "completion_percentage": "INTEGER DEFAULT 0",
        "actual_duration_source": "TEXT",
        "is_delayed": "INTEGER DEFAULT 0",
        "original_scheduled_start": "TEXT",
        "original_scheduled_end": "TEXT",
        "reschedule_count": "INTEGER DEFAULT 0",
        "ai_energy_cost": "REAL",
        "ai_recovery_value": "REAL",
        "ai_energy_confidence": "REAL",
        "ai_energy_reason": "TEXT",
        "is_milestone": "INTEGER DEFAULT 0",
        "result_summary": "TEXT",
        "updated_at": "TEXT",
    }
    for col, ddl in task_columns.items():
        if col not in existing_cols:
            c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()

_init_db()

def _db_conn():
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# In-memory cache for repeated queries (task text → analysis result)
import hashlib
_ANALYSIS_CACHE = {}  # {text_hash: (analysis_dict, timestamp)}

# Prompts now in common.py:
# common.ANALYZE_PROMPT, common.SCHEDULE_PROMPT, common.BATCH_EXTRACT_PROMPT



class BridgeHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """Handle GET requests (calendar read, profile, stats)."""
        if self.path.startswith("/api/calendar/read"):
            self._handle_calendar_read()
        elif self.path.startswith("/api/profile"):
            self._handle_profile()
        elif self.path.startswith("/api/history/stats"):
            self._handle_history_stats()
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")

        # Handle multipart file upload
        if content_type.startswith("multipart/form-data"):
            self._handle_upload()
            return

        # Handle JSON requests (existing)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            self._json_response(400, {"status": "error", "message": f"Invalid JSON: {e}"})
            return

        if self.path == "/api/calendar":
            self._handle_calendar(data)
        elif self.path == "/api/analyze":
            self._handle_analyze(data)
        elif self.path == "/api/schedule":
            self._handle_schedule(data)
        elif self.path == "/api/board/conflicts":
            self._handle_board_conflicts(data)
        elif self.path == "/api/work-profile":
            self._handle_work_profile(data)
        elif self.path == "/api/work-profile/daily-summary":
            self._handle_work_profile_summary(data, "daily")
        elif self.path == "/api/work-profile/yearly-summary":
            self._handle_work_profile_summary(data, "yearly")
        elif self.path == "/api/tasks/reschedule-suggestions":
            self._handle_task_reschedule_suggestions(data)
        elif self.path == "/api/tasks/accept-reschedule":
            self._handle_accept_reschedule(data)
        elif self.path == "/api/history/log":
            self._handle_history_log(data)
        else:
            self.send_error(404, "Not found")

    def _handle_analyze(self, data):
        text = data.get("text", "").strip()
        if not text:
            self._json_response(400, {"status": "error", "message": "Empty text"})
            return

        # Check cache first
        cache_key = hashlib.md5(text.encode()).hexdigest()
        if cache_key in _ANALYSIS_CACHE:
            cached_result, ts = _ANALYSIS_CACHE[cache_key]
            if time.time() - ts < 300:  # 5 min cache
                print(f"[ANALYZE] Cache hit for: {text[:50]}", flush=True)
                # Mark as cached
                resp = dict(cached_result)
                resp["source"] = "cached"
                self._json_response(200, resp)
                return

        if not DEEPSEEK_API_KEY:
            result = common.keyword_analyze(text)
            self._json_response(200, result)
            return

        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-chat",  # v4-flash: ~1s vs v4-pro: ~3s
                    "messages": [
                        {"role": "system", "content": self._build_analyze_prompt()},
                        {"role": "user", "content": f"分析这个任务：{text}"}
                    ],
                    "temperature": 0.1,      # lower = more deterministic
                    "max_tokens": 300
                }).encode(),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                }
            )

            print(f"[ANALYZE] Calling DeepSeek for: {text[:80]}", flush=True)
            t0 = time.time()
            resp = urllib.request.urlopen(req, timeout=10)  # shorter timeout
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"].strip()
            elapsed = time.time() - t0
            print(f"[ANALYZE] API took {elapsed:.1f}s", flush=True)

            # Robust JSON extraction
            analysis = common.extract_json(content)

            if analysis:
                response_data = {"status": "ok", "analysis": analysis}
                _ANALYSIS_CACHE[cache_key] = (response_data, time.time())
                self._json_response(200, response_data)
            else:
                print(f"[ANALYZE] JSON extraction failed, raw: {content[:200]}", flush=True)
                result = common.keyword_analyze(text)
                self._json_response(200, result)

        except Exception as e:
            print(f"[ANALYZE] Error: {e}", flush=True)
            result = common.keyword_analyze(text)
            self._json_response(200, result)

    # Now uses common.extract_json

    # Now uses common.keyword_analyze

    def _handle_calendar(self, data):
        summary = data.get("summary", "").strip()
        start_dt = data.get("start")
        end_dt = data.get("end")
        description = data.get("description", "")
        location = data.get("location", "")

        if not summary:
            self._json_response(400, {"status": "error", "message": "Missing: summary"})
            return
        if not start_dt or not end_dt:
            self._json_response(400, {"status": "error", "message": "Missing: start/end time"})
            return

        cmd = [HERMES_PYTHON, GAPI, "calendar", "create",
               "--summary", summary, "--start", start_dt, "--end", end_dt]
        if description:
            cmd += ["--description", description]
        if location:
            cmd += ["--location", location]

        env = os.environ.copy()
        env["HERMES_HOME"] = HERMES_HOME

        try:
            print(f"[CALENDAR] Creating: {summary[:50]} | {start_dt} → {end_dt}", flush=True)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
            output = result.stdout.strip()
            if result.returncode == 0:
                try:
                    resp = json.loads(output)
                    self._json_response(200, {
                        "status": "created",
                        "id": resp.get("id", ""),
                        "summary": resp.get("summary", summary),
                        "htmlLink": resp.get("htmlLink", "")
                    })
                except json.JSONDecodeError:
                    self._json_response(200, {"status": "created", "raw": output})
            else:
                err = result.stderr.strip() or output or "Unknown error"
                print(f"[CALENDAR] Error: {err[:300]}", flush=True)
                self._json_response(500, {"status": "error", "message": err})
        except Exception as e:
            self._json_response(500, {"status": "error", "message": str(e)})

    # _normalize_board_task_window → common.normalize_board_task_window
    # _find_board_recommendations → common.find_board_recommendations

    def _handle_board_conflicts(self, data):
        """POST /api/board/conflicts — detect local board task overlaps."""
        task = data.get("task") or {}
        existing_tasks = data.get("existingTasks") or []
        days = data.get("days", 7)

        date_iso, start, end, message = common.normalize_board_task_window(task)
        if message:
            self._json_response(400, {
                "status": "error",
                "message": message,
                "hasConflict": False,
                "conflicts": [],
                "recommendations": [],
            })
            return

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
        self._json_response(200, {
            "status": "ok",
            "hasConflict": bool(conflicts),
            "conflicts": conflicts,
            "recommendations": recommendations,
            "message": "发现时间冲突，请选择推荐时间或强行添加" if conflicts else "该时间段没有冲突",
        })

    def _handle_work_profile(self, data):
        """POST /api/work-profile — aggregate local board tasks."""
        try:
            profile = work_profile.build_profile(
                data.get("tasks") or [],
                period_type=data.get("periodType", "week"),
                start_date=data.get("startDate"),
                end_date=data.get("endDate"),
                project_name=data.get("projectName", ""),
                status=data.get("status", "all"),
            )
            self._json_response(200, {"status": "ok", **profile})
        except Exception as e:
            print(f"[WORK_PROFILE] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})

    def _handle_work_profile_summary(self, data, summary_type):
        """POST /api/work-profile/*-summary — structured coach summary."""
        profile = data.get("profile")
        if not profile:
            profile = work_profile.build_profile(
                data.get("tasks") or [],
                period_type="year" if summary_type == "yearly" else "day",
                start_date=data.get("date") or data.get("startDate"),
                end_date=data.get("endDate"),
            )

        fallback = work_profile.rule_summary(profile, summary_type=summary_type)
        if not DEEPSEEK_API_KEY:
            self._json_response(200, {"status": "ok", "source": "rule_fallback", "summary": fallback})
            return

        try:
            prompt = (
                "你是私人效率教练。只返回 JSON，不要 markdown。字段必须包含："
                "overview, achievements, problems, energyInsight, planningInsight, "
                "tomorrowSuggestion, mainFocus。语气直接、客观、有行动建议，不羞辱用户。"
            )
            payload = {
                "overview": profile.get("overview", {}),
                "taskTypeDistribution": profile.get("taskTypeDistribution", []),
                "energyDistribution": profile.get("energyDistribution", []),
                "projects": profile.get("projects", [])[:8],
                "achievements": profile.get("achievements", [])[:8],
                "delayAnalysis": profile.get("delayAnalysis", []),
            }
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
                    ],
                    "temperature": 0.2,
                    "max_tokens": 900,
                }).encode(),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp = urllib.request.urlopen(req, timeout=25)
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"].strip()
            summary = work_profile.validate_summary(common.extract_json(content))
            if not summary:
                summary = fallback
                source = "rule_fallback"
            else:
                source = "ai"
            self._json_response(200, {"status": "ok", "source": source, "summary": summary})
        except Exception as e:
            print(f"[WORK_PROFILE] Summary fallback: {e}", flush=True)
            self._json_response(200, {"status": "ok", "source": "rule_fallback", "summary": fallback})

    def _handle_task_reschedule_suggestions(self, data):
        """POST /api/tasks/reschedule-suggestions — suggest new slots for a delayed task."""
        task = data.get("task") or {}
        tasks = data.get("tasks") or []
        suggestions = work_profile.reschedule_suggestions(task, tasks)
        self._json_response(200, {"status": "ok", "suggestions": suggestions})

    def _handle_accept_reschedule(self, data):
        """POST /api/tasks/accept-reschedule — return a task patch for localStorage."""
        task = data.get("task") or {}
        suggestion = data.get("suggestion") or {}
        if not task or not suggestion:
            self._json_response(400, {"status": "error", "message": "Missing task or suggestion"})
            return
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
        self._json_response(200, {"status": "ok", "taskId": task.get("id"), "patch": patch})

    def _handle_upload(self):
        """Handle file upload: extract text, AI decompose into tasks."""
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", 0))

        # Parse multipart form data
        boundary = content_type.split("boundary=")[1].strip()
        raw = self.rfile.read(content_length)

        # Extract file data from multipart
        file_data, filename = self._parse_multipart(raw, boundary)
        if not file_data:
            self._json_response(400, {"status": "error", "message": "No file found in upload"})
            return

        print(f"[UPLOAD] Received: {filename} ({len(file_data)} bytes)", flush=True)

        # Save to temp file
        suffix = os.path.splitext(filename)[1].lower() if filename else ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(file_data)
            tmp_path = tf.name

        try:
            # Extract text from file
            text = self._extract_text(tmp_path, suffix)
            if not text or not text.strip():
                self._json_response(200, {
                    "status": "ok",
                    "source": "ocr",
                    "raw_text": "",
                    "tasks": [],
                    "message": "无法从文件中识别出文字内容"
                })
                return

            print(f"[UPLOAD] Extracted {len(text)} chars of text", flush=True)

            # Send to AI for task decomposition
            if DEEPSEEK_API_KEY:
                tasks = self._batch_extract_tasks(text)
            else:
                tasks = []

            self._json_response(200, {
                "status": "ok",
                "source": "ai" if DEEPSEEK_API_KEY else "keyword",
                "raw_text": text[:5000],
                "total_chars": len(text),
                "tasks": tasks,
                "message": f"共识别 {len(text)} 字，提取 {len(tasks)} 个任务" if tasks else "未能从文档中识别出任务"
            })

        except Exception as e:
            print(f"[UPLOAD] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _parse_multipart(self, raw, boundary):
        """Parse multipart form data, return (file_bytes, filename)."""
        boundary_bytes = boundary.encode()
        parts = raw.split(b"--" + boundary_bytes)

        for part in parts:
            if b"Content-Disposition" not in part:
                continue

            # Extract filename
            header_end = part.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            headers = part[:header_end].decode(errors="replace")
            file_match = re.search(r'filename="([^"]*)"', headers)
            if not file_match:
                continue

            filename = file_match.group(1)
            content = part[header_end + 4:]  # After \r\n\r\n
            # Remove trailing boundary
            content = content.rstrip(b"\r\n-")
            # Remove trailing \r\n
            content = content.rstrip(b"\r\n")

            return content, filename

        return None, None

    def _extract_text(self, filepath, suffix):
        """Extract text from PDF or image file."""
        suffix = suffix.lower()

        if suffix == ".pdf":
            # Use pdftotext
            result = subprocess.run(
                ["pdftotext", filepath, "-"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return result.stdout.strip()
            print(f"[UPLOAD] pdftotext failed: {result.stderr[:200]}", flush=True)
            return ""

        elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp"):
            # Use tesseract OCR
            result = subprocess.run(
                ["tesseract", filepath, "stdout", "-l", "chi_sim+eng", "--psm", "3"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                text = result.stdout.strip()
                # Also capture stderr (tesseract prints version info there)
                if not text and result.stderr:
                    text = result.stderr.strip()
                return text
            print(f"[UPLOAD] tesseract failed: {result.stderr[:200]}", flush=True)
            return ""

        else:
            return ""

    def _batch_extract_tasks(self, text):
        """Send extracted text to DeepSeek to decompose into individual tasks.
        For long documents, use chunking to avoid truncation."""
        today = time.strftime("%Y-%m-%d")

        # For short texts: single pass with full content
        if len(text) <= 8000:
            prompt = common.BATCH_EXTRACT_PROMPT.format(text=text, today=today)
            return self._call_deepseek_batch(prompt)

        # For long texts: chunk by sections, merge results
        print(f"[UPLOAD] Long doc ({len(text)} chars), chunking...", flush=True)
        all_tasks = []
        chunks = self._chunk_text(text, 6000)

        for i, chunk in enumerate(chunks):
            chunk_prompt = common.BATCH_EXTRACT_PROMPT.format(
                text=f"(第 {i+1}/{len(chunks)} 部分)\n\n{chunk}",
                today=today
            )
            tasks = self._call_deepseek_batch(chunk_prompt)
            all_tasks.extend(tasks)
            print(f"[UPLOAD] Chunk {i+1}/{len(chunks)}: {len(tasks)} tasks", flush=True)

        # Deduplicate by title similarity
        all_tasks = self._deduplicate_tasks(all_tasks)
        return all_tasks

    def _chunk_text(self, text, chunk_size):
        """Split text into chunks at natural boundaries (paragraphs, lines)."""
        chunks = []
        paragraphs = text.split('\n\n')
        current = ''
        for para in paragraphs:
            if len(current) + len(para) > chunk_size and current:
                chunks.append(current.strip())
                current = para
            else:
                current += ('\n\n' if current else '') + para
        if current.strip():
            chunks.append(current.strip())
        return chunks

    def _deduplicate_tasks(self, tasks):
        """Remove duplicate tasks based on title similarity."""
        seen = set()
        unique = []
        for t in tasks:
            key = (t.get('title', '')[:20] + '|' + 
                   str(t.get('startTime', '')) + '|' + 
                   str(t.get('dateISO', '')))
            if key not in seen:
                seen.add(key)
                unique.append(t)
        return unique

    def _call_deepseek_batch(self, prompt):
        """Single DeepSeek API call for batch extraction."""
        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-v4-pro",  # v4-pro: more accurate for document decomposition
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "请提取所有任务"}
                    ],
                    "temperature": 0.1,
                    "max_tokens": 4000      # ↑ 2000 → 4000 for longer docs
                }).encode(),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                }
            )

            print(f"[UPLOAD] Calling DeepSeek (prompt {len(prompt)} chars)...", flush=True)
            t0 = time.time()
            resp = urllib.request.urlopen(req, timeout=45)  # ↑ 30 → 45s for long docs
            result = json.loads(resp.read())
            elapsed = time.time() - t0
            content = result["choices"][0]["message"]["content"].strip()
            print(f"[UPLOAD] AI took {elapsed:.1f}s, response {len(content)} chars", flush=True)

            tasks = common.extract_json_array(content)
            if not tasks:
                # Diagnostic: log raw response when extraction fails
                print(f"[UPLOAD] JSON extraction failed! Raw preview: {content[:300]}", flush=True)
            return tasks

        except Exception as e:
            print(f"[UPLOAD] AI error: {e}", flush=True)
            return []

    # _extract_json_array → common.extract_json_array

    # ==================== NEW: Calendar Read ====================
    def _handle_calendar_read(self):
        """GET /api/calendar/read?days=14 — read upcoming calendar events."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        days = int(params.get("days", [14])[0])

        events = self._read_calendar_events(days)
        free_slots = self._find_free_slots(events, days)

        self._json_response(200, {
            "status": "ok",
            "events": events,
            "freeSlots": free_slots,
            "totalEvents": len(events),
            "totalFreeSlots": len(free_slots)
        })

    def _read_calendar_events(self, days=14):
        """Call google_api.py to list calendar events."""
        from datetime import datetime, timedelta
        now = datetime.now()
        time_min = now.strftime("%Y-%m-%dT00:00:00+08:00")
        time_max = (now + timedelta(days=days)).strftime("%Y-%m-%dT23:59:59+08:00")

        cmd = [HERMES_PYTHON, GAPI, "calendar", "list",
               "--start", time_min, "--end", time_max, "--max", "100"]
        env = os.environ.copy()
        env["HERMES_HOME"] = HERMES_HOME

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
            if result.returncode == 0 and result.stdout.strip():
                events = json.loads(result.stdout)
                # Filter out cancelled events
                events = [e for e in events if e.get("status") != "cancelled"]
                print(f"[CALENDAR] Read {len(events)} events", flush=True)
                return events
            else:
                print(f"[CALENDAR] Read failed: {result.stderr[:200]}", flush=True)
        except Exception as e:
            print(f"[CALENDAR] Read error: {e}", flush=True)
        return []

    def _find_free_slots(self, events, days=7):
        """Find free time slots between calendar events (9:00-22:00)."""
        from datetime import datetime, timedelta
        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        free_slots = []

        for day_offset in range(days):
            day = today + timedelta(days=day_offset)
            day_str = day.strftime("%Y-%m-%d")

            # Get events for this day
            day_events = []
            for e in events:
                start_str = e.get("start", "")
                if start_str.startswith(day_str) or (
                   "T" in start_str and start_str[:10] == day_str):
                    day_events.append(e)

            if not day_events:
                # Entire day is free (9:00-22:00)
                free_slots.append({
                    "date": day_str,
                    "start": "09:00",
                    "end": "22:00",
                    "label": "全天空闲"
                })
                continue

            # Sort by start time
            day_events.sort(key=lambda e: e.get("start", ""))

            # Find gaps between events
            day_start = 9 * 60  # 9:00 in minutes
            day_end = 22 * 60   # 22:00 in minutes
            occupied = []

            for e in day_events:
                s = e.get("start", "")
                e_time = e.get("end", "")
                try:
                    if "T" in s:
                        sh, sm = int(s[11:13]), int(s[14:16])
                        eh, em = int(e_time[11:13]), int(e_time[14:16])
                        occupied.append((sh * 60 + sm, eh * 60 + em))
                except (ValueError, IndexError):
                    continue

            occupied.sort()

            current = day_start
            for (occ_start, occ_end) in occupied:
                if current < occ_start:
                    gap = occ_start - current
                    if gap >= 60:  # Only report gaps >= 1 hour
                        free_slots.append({
                            "date": day_str,
                            "start": f"{current // 60:02d}:{current % 60:02d}",
                            "end": f"{occ_start // 60:02d}:{occ_start % 60:02d}",
                            "durationMinutes": gap,
                            "label": f"空闲 {gap // 60}小时{gap % 60}分钟"
                        })
                current = max(current, occ_end)

            # Remaining time after last event
            if current < day_end:
                gap = day_end - current
                if gap >= 60:
                    free_slots.append({
                        "date": day_str,
                        "start": f"{current // 60:02d}:{current % 60:02d}",
                        "end": "22:00",
                        "durationMinutes": gap,
                        "label": f"空闲 {gap // 60}小时{gap % 60}分钟"
                    })

        return free_slots

    # ==================== NEW: Smart Schedule (Auto-排班) ====================
    def _handle_schedule(self, data):
        """POST /api/schedule — AI auto-scheduling for multiple tasks."""
        tasks = data.get("tasks", [])
        if not tasks:
            self._json_response(400, {"status": "error", "message": "No tasks provided"})
            return

        print(f"[SCHEDULE] Auto-scheduling {len(tasks)} tasks...", flush=True)

        # Read calendar events
        events = self._read_calendar_events(14)

        # Format calendar events for the prompt
        events_text = ""
        for e in events:
            summary = e.get("summary", "无标题")
            start = e.get("start", "")
            end = e.get("end", "")
            # Extract date + time for readability
            if "T" in start:
                date_part = start[:10]
                start_time = start[11:16]
                end_time = end[11:16] if "T" in end else "?"
                events_text += f"  {date_part} {start_time}-{end_time}: {summary}\n"
            else:
                events_text += f"  {start} (全天): {summary}\n"

        # Format tasks for the prompt
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

        if not DEEPSEEK_API_KEY:
            self._json_response(200, {
                "status": "ok",
                "source": "keyword",
                "schedule": [],
                "message": "需要 DeepSeek API Key 才能智能排班"
            })
            return

        today = time.strftime("%Y-%m-%d")
        prompt = self._build_schedule_prompt(
            events_text or "（暂无日历事件）",
            tasks_text
        )

        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps({
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "请为这些任务安排最佳时间"}
                    ],
                    "temperature": 0.3,
                    "max_tokens": 2000
                }).encode(),
                headers={
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json"
                }
            )

            print(f"[SCHEDULE] Calling DeepSeek...", flush=True)
            t0 = time.time()
            resp = urllib.request.urlopen(req, timeout=30)
            result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"].strip()
            elapsed = time.time() - t0
            print(f"[SCHEDULE] AI took {elapsed:.1f}s", flush=True)

            schedule = common.extract_json_array(content)
            print(f"[SCHEDULE] AI suggested {len(schedule)} time slots", flush=True)

            self._json_response(200, {
                "status": "ok",
                "source": "ai",
                "schedule": schedule,
                "calendarEvents": events,
                "freeSlotsCount": len(self._find_free_slots(events))
            })

        except Exception as e:
            print(f"[SCHEDULE] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})

    # ==================== History Logging (Task Historian) ====================
    def _handle_history_log(self, data):
        """POST /api/history/log — record a task lifecycle event."""
        event_type = data.get("event_type", "")
        if not event_type:
            self._json_response(400, {"status": "error", "message": "Missing: event_type"})
            return

        task_id = data.get("task_id", str(uuid.uuid4()))
        now = datetime.now().isoformat()

        conn = _db_conn()
        c = conn.cursor()

        # Handle different event types
        if event_type == "task_created":
            # Upsert the task record
            c.execute("""
                INSERT INTO tasks (id, title, category, priority, project_name,
                                   scheduled_start, scheduled_end,
                                   estimated_duration_minutes, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, category=excluded.category,
                    priority=excluded.priority, project_name=excluded.project_name,
                    scheduled_start=excluded.scheduled_start,
                    scheduled_end=excluded.scheduled_end,
                    estimated_duration_minutes=excluded.estimated_duration_minutes
            """, (
                task_id,
                data.get("title", ""),
                data.get("category", ""),
                data.get("priority", ""),
                data.get("project_name", ""),
                data.get("scheduled_start", ""),
                data.get("scheduled_end", ""),
                data.get("estimated_duration_minutes"),
                now,
            ))
            print(f"[HISTORY] Task created: {data.get('title', task_id)[:50]}", flush=True)

        elif event_type == "task_completed":
            actual_duration = data.get("actual_duration_minutes")
            c.execute("""
                UPDATE tasks SET status='completed', completed_at=?,
                actual_duration_minutes=?
                WHERE id=?
            """, (now, actual_duration, task_id))
            print(f"[HISTORY] Task completed: {task_id}", flush=True)

        elif event_type == "task_rescheduled":
            c.execute("""
                UPDATE tasks SET scheduled_start=?, scheduled_end=?
                WHERE id=?
            """, (
                data.get("new_start", ""),
                data.get("new_end", ""),
                task_id,
            ))
            print(f"[HISTORY] Task rescheduled: {task_id}", flush=True)

        elif event_type == "task_cancelled":
            c.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
            print(f"[HISTORY] Task cancelled: {task_id}", flush=True)

        elif event_type == "ai_correction":
            # User manually overrode AI suggestion
            print(f"[HISTORY] AI correction: {task_id}", flush=True)

        # Log the event
        c.execute("""
            INSERT INTO task_events (task_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?)
        """, (
            task_id,
            event_type,
            json.dumps(data.get("payload", {}), ensure_ascii=False),
            now,
        ))

        conn.commit()
        conn.close()

        self._json_response(200, {"status": "ok", "task_id": task_id})

    # ==================== User Profile ====================
    def _handle_profile(self):
        """GET /api/profile?days=30 — return user profile summary."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        days = int(params.get("days", [30])[0])

        try:
            summary = user_profile.build_profile_summary(DB_PATH, days)
            stats = user_profile.get_detailed_stats(DB_PATH, days)
            self._json_response(200, {
                "status": "ok",
                "summary": summary,
                "stats": stats,
            })
        except Exception as e:
            print(f"[PROFILE] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})

    def _handle_history_stats(self):
        """GET /api/history/stats?days=30 — return detailed history stats."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        days = int(params.get("days", [30])[0])

        try:
            stats = user_profile.get_detailed_stats(DB_PATH, days)
            self._json_response(200, {"status": "ok", **stats})
        except Exception as e:
            print(f"[STATS] Error: {e}", flush=True)
            self._json_response(500, {"status": "error", "message": str(e)})

    # ==================== Profile-Injected Prompts ====================
    def _build_analyze_prompt(self):
        """Build ANALYZE_PROMPT with today's date and user profile injected."""
        today = time.strftime("%Y-%m-%d")
        prompt = common.ANALYZE_PROMPT.replace('__TODAY__', today)
        profile_text = user_profile.build_profile_summary(DB_PATH, 30)
        return user_profile.inject_profile_into_prompt(prompt, profile_text)

    def _build_schedule_prompt(self, calendar_events, tasks_text):
        """Build SCHEDULE_PROMPT with user profile injected."""
        today = time.strftime("%Y-%m-%d")
        base_prompt = common.SCHEDULE_PROMPT.format(
            calendar_events=calendar_events,
            tasks_text=tasks_text,
            today=today,
        )
        profile_text = user_profile.build_profile_summary(DB_PATH, 30)
        return user_profile.inject_profile_into_prompt(base_prompt, profile_text)

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}", flush=True)


def main():
    print(f"🚀 Bridge running on http://localhost:{PORT}")
    print(f"   GET  /api/calendar/read — Read calendar events + find free slots")
    print(f"   POST /api/analyze       — AI task analysis (smart duration estimate)")
    print(f"   POST /api/calendar      — Google Calendar write")
    print(f"   POST /api/schedule      — AI auto-scheduling (智能排班)")
    print(f"   POST /api/upload        — File upload + OCR + multi-task extraction")
    print(f"   POST /api/history/log   — Task lifecycle event logging 🆕")
    print(f"   GET  /api/profile       — User profile summary (学习画像) 🆕")
    print(f"   GET  /api/history/stats — Detailed task history stats 🆕")
    print(f"   DeepSeek API: {'✅ configured' if DEEPSEEK_API_KEY else '❌ not found (keyword fallback)'}")
    HTTPServer(("localhost", PORT), BridgeHandler).serve_forever()

if __name__ == "__main__":
    main()
