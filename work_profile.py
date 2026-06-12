"""Work Profile statistics for the local calendar board.

The board task source of truth is the browser localStorage payload.  These
helpers accept that payload, normalize old tasks, and return aggregate data
without requiring a separate task store.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta


TASK_TYPES = {
    "study": {"label": "学习", "energy": 4, "recovery": 0, "color": "#0071e3"},
    "work": {"label": "工作", "energy": 4, "recovery": 0, "color": "#ff9500"},
    "project": {"label": "项目", "energy": 4, "recovery": 0, "color": "#af52de"},
    "content_creation": {"label": "内容创作", "energy": 4, "recovery": 0, "color": "#ff2d55"},
    "communication_meeting": {"label": "沟通会议", "energy": 3, "recovery": 0, "color": "#ff3b30"},
    "life": {"label": "生活事务", "energy": 2, "recovery": 1, "color": "#34c759"},
    "rest_recovery": {"label": "休息恢复", "energy": 1, "recovery": 5, "color": "#5ac8fa"},
    "exercise": {"label": "身体锻炼", "energy": 3, "recovery": 4, "color": "#30d158"},
}

CATEGORY_TO_TYPE = {
    "学习": "study",
    "工作": "work",
    "项目": "project",
    "内容创作": "content_creation",
    "会议": "communication_meeting",
    "沟通会议": "communication_meeting",
    "生活": "life",
    "生活事务": "life",
    "提醒": "life",
    "休息恢复": "rest_recovery",
    "身体锻炼": "exercise",
}

STATUS_LABELS = {
    "planned": "计划中",
    "in_progress": "进行中",
    "completed": "已完成",
    "delayed": "已延期",
    "cancelled": "已取消",
}


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    value = str(value)
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00").split("+")[0])
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _parse_minutes(value):
    if not value or not isinstance(value, str) or ":" not in value:
        return None
    try:
        hour, minute = value.split(":", 1)
        hour, minute = int(hour), int(minute)
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def _format_minutes(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _clamp(value, low, high):
    return max(low, min(high, value))


def period_range(period_type="week", start_date=None, end_date=None, now=None):
    now = now or datetime.now()
    if start_date and end_date:
        start = _parse_date(start_date) or now
        end = _parse_date(end_date) or start
        return start.replace(hour=0, minute=0, second=0, microsecond=0), end.replace(hour=23, minute=59, second=59, microsecond=0)

    anchor = _parse_date(start_date) or now
    anchor = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    if period_type == "day":
        start = anchor
        end = start + timedelta(days=1) - timedelta(seconds=1)
    elif period_type == "month":
        start = anchor.replace(day=1)
        if start.month == 12:
            next_month = start.replace(year=start.year + 1, month=1)
        else:
            next_month = start.replace(month=start.month + 1)
        end = next_month - timedelta(seconds=1)
    elif period_type == "year":
        start = anchor.replace(month=1, day=1)
        end = start.replace(year=start.year + 1) - timedelta(seconds=1)
    else:
        start = anchor - timedelta(days=anchor.weekday())
        end = start + timedelta(days=7) - timedelta(seconds=1)
    return start, end


def _task_type(task):
    raw = task.get("taskType")
    if raw in TASK_TYPES:
        return raw
    return CATEGORY_TO_TYPE.get(task.get("category") or "", "work")


def estimate_energy(task_type, planned_minutes, actual_minutes, reschedule_count=0, priority=""):
    base = TASK_TYPES.get(task_type, TASK_TYPES["work"])
    energy = base["energy"]
    recovery = base["recovery"]
    reasons = [f"基础类型为{base['label']}"]
    duration = actual_minutes or planned_minutes or 0

    if duration >= 180:
        energy += 1
        reasons.append("任务时长超过3小时")
    elif duration >= 120:
        energy += 0.5
        reasons.append("任务时长超过2小时")
    if reschedule_count >= 2:
        energy += 0.5
        reasons.append("多次重新安排增加认知负担")
    if priority == "P1":
        energy += 0.5
        reasons.append("任务优先级较高")
    if task_type == "rest_recovery":
        recovery = max(recovery, 5)
    if task_type == "exercise" and duration >= 30:
        recovery = max(recovery, 4)

    return {
        "aiEnergyCost": round(_clamp(energy, 1, 5), 1),
        "aiRecoveryValue": round(_clamp(recovery, 0, 5), 1),
        "aiEnergyConfidence": 0.68,
        "aiEnergyReason": "；".join(reasons),
    }


def normalize_task(task, now=None):
    now = now or datetime.now()
    date_iso = task.get("dateISO") or ""
    start_time = task.get("startTime") or "09:00"
    end_time = task.get("endTime") or ""
    start_min = _parse_minutes(start_time)
    end_min = _parse_minutes(end_time)
    planned = task.get("durationMinutes") or task.get("estimatedDurationMinutes")

    if planned is None and start_min is not None and end_min is not None and end_min > start_min:
        planned = end_min - start_min
    try:
        planned = int(planned) if planned is not None else 0
    except (TypeError, ValueError):
        planned = 0
    if not end_time and start_min is not None and planned:
        end_time = _format_minutes(start_min + planned)
        end_min = start_min + planned

    scheduled_start = _parse_date(f"{date_iso}T{start_time}") if date_iso and start_time else None
    scheduled_end = _parse_date(f"{date_iso}T{end_time}") if date_iso and end_time else None
    completed_at = _parse_date(task.get("completedAt"))
    done = bool(task.get("done"))
    completion = task.get("completionPercentage")
    if completion is None:
        completion = 100 if done else 0
    try:
        completion = int(completion)
    except (TypeError, ValueError):
        completion = 100 if done else 0
    completion = _clamp(completion, 0, 100)

    status = task.get("status") or ("completed" if completion >= 100 else "planned")
    if completion >= 100:
        status = "completed"
        done = True
    elif status == "completed":
        completion = 100
        done = True
    elif scheduled_end and scheduled_end < now and status != "cancelled":
        status = "delayed"
    elif completion > 0:
        status = "in_progress"

    actual = task.get("actualDurationMinutes")
    actual_source = task.get("actualDurationSource")
    if actual is None:
        actual = planned
        actual_source = actual_source or "estimated"
    else:
        try:
            actual = int(actual)
        except (TypeError, ValueError):
            actual = planned
            actual_source = "estimated"
        actual_source = actual_source or "manual"

    task_type = _task_type(task)
    reschedules = int(task.get("rescheduleCount") or 0)
    energy = estimate_energy(task_type, planned, actual, reschedules, task.get("priority") or "")
    if task.get("aiEnergyCost"):
        energy["aiEnergyCost"] = task.get("aiEnergyCost")
    if task.get("aiRecoveryValue"):
        energy["aiRecoveryValue"] = task.get("aiRecoveryValue")
    if task.get("aiEnergyReason"):
        energy["aiEnergyReason"] = task.get("aiEnergyReason")

    original_end = _parse_date(task.get("originalScheduledEnd")) or scheduled_end
    delay_minutes = 0
    if original_end and status != "cancelled":
        compare = completed_at if completed_at else now
        if compare > original_end and completion < 100:
            delay_minutes = int((compare - original_end).total_seconds() // 60)
        elif completed_at and completed_at > original_end:
            delay_minutes = int((completed_at - original_end).total_seconds() // 60)

    normalized = dict(task)
    normalized.update({
        "id": task.get("id") or "",
        "title": task.get("title") or "未命名任务",
        "dateISO": date_iso,
        "startTime": start_time,
        "endTime": end_time,
        "taskType": task_type,
        "taskTypeLabel": TASK_TYPES[task_type]["label"],
        "category": task.get("category") or TASK_TYPES[task_type]["label"],
        "projectName": task.get("projectName") or "未归属项目",
        "durationMinutes": planned,
        "estimatedDurationMinutes": planned,
        "actualDurationMinutes": actual,
        "actualDurationSource": actual_source,
        "completionPercentage": completion,
        "status": status,
        "statusLabel": STATUS_LABELS.get(status, status),
        "done": done,
        "completedAt": task.get("completedAt") or (now.isoformat() if done and not task.get("completedAt") else task.get("completedAt")),
        "isDelayed": delay_minutes > 0 or status == "delayed",
        "delayMinutes": max(0, delay_minutes),
        "rescheduleCount": reschedules,
        **energy,
    })
    normalized["energyInvestment"] = round((actual or 0) * float(normalized["aiEnergyCost"]), 1)
    normalized["recoveryInvestment"] = round((actual or 0) * float(normalized["aiRecoveryValue"]), 1)
    return normalized


def normalize_tasks(tasks, now=None):
    return [normalize_task(task, now=now) for task in (tasks or []) if isinstance(task, dict)]


def _in_range(task, start, end):
    scheduled = _parse_date((task.get("dateISO") or "") + "T00:00:00")
    return bool(scheduled and start <= scheduled <= end)


def _bucket_key(dt, period_type):
    if period_type == "year":
        return dt.strftime("%Y-%m")
    if period_type == "month":
        week = ((dt.day - 1) // 7) + 1
        return f"第{week}周"
    return dt.strftime("%m-%d")


def _pct(part, total):
    return round(part / total * 100, 1) if total else 0


def _distribution(tasks, key):
    groups = defaultdict(lambda: {"minutes": 0, "energy": 0, "tasks": 0, "completed": 0})
    for task in tasks:
        group_key = task.get(key) or "未分类"
        groups[group_key]["minutes"] += task["actualDurationMinutes"] or 0
        groups[group_key]["energy"] += task["energyInvestment"] or 0
        groups[group_key]["tasks"] += 1
        if task["status"] == "completed":
            groups[group_key]["completed"] += 1
    total_minutes = sum(item["minutes"] for item in groups.values())
    total_energy = sum(item["energy"] for item in groups.values())
    rows = []
    for name, item in groups.items():
        task_type = name if name in TASK_TYPES else None
        rows.append({
            "key": name,
            "label": TASK_TYPES[task_type]["label"] if task_type else name,
            "color": TASK_TYPES[task_type]["color"] if task_type else "#8e8e93",
            "minutes": item["minutes"],
            "hours": round(item["minutes"] / 60, 1),
            "energy": round(item["energy"], 1),
            "taskCount": item["tasks"],
            "completedCount": item["completed"],
            "timeShare": _pct(item["minutes"], total_minutes),
            "energyShare": _pct(item["energy"], total_energy),
        })
    return sorted(rows, key=lambda row: row["minutes"], reverse=True)


def _competencies(tasks, active_days):
    effective = [t for t in tasks if t["status"] != "cancelled"]
    completed = [t for t in effective if t["status"] == "completed"]
    if len(completed) < 10 or active_days < 7:
        return {
            "isReliable": False,
            "message": "需要更多任务数据才能形成稳定画像",
            "scores": {},
            "trend": [],
        }

    completion_rate = len(completed) / max(len(effective), 1)
    on_time = [t for t in completed if not t["isDelayed"]]
    delayed_completed = [t for t in completed if t["isDelayed"]]
    delayed = [t for t in effective if t["isDelayed"]]
    estimate_tasks = [t for t in completed if t["estimatedDurationMinutes"] > 0 and t["actualDurationSource"] != "estimated"]
    if estimate_tasks:
        avg_error = sum(abs(t["actualDurationMinutes"] - t["estimatedDurationMinutes"]) / t["estimatedDurationMinutes"] for t in estimate_tasks) / len(estimate_tasks)
    else:
        avg_error = 0.45
    recovery_minutes = sum(t["actualDurationMinutes"] for t in effective if t["taskType"] in ("rest_recovery", "exercise"))
    total_minutes = sum(t["actualDurationMinutes"] for t in effective)
    high_energy_days = defaultdict(float)
    for task in effective:
        high_energy_days[task["dateISO"]] += task["energyInvestment"]
    overload_days = len([v for v in high_energy_days.values() if v > 1800])

    scores = {
        "execution": round(_clamp((completion_rate * 50) + (_pct(len(on_time), len(completed)) * 0.3) + (_pct(len(delayed_completed), max(len(delayed), 1)) * 0.2), 0, 100), 1),
        "focus": round(_clamp(70 - (len(effective) / max(active_days, 1) - 3) * 6, 30, 95), 1),
        "planning": round(_clamp(85 - _pct(len(delayed), max(len(effective), 1)) - overload_days * 3, 20, 95), 1),
        "time_estimation": round(_clamp(100 - avg_error * 100, 20, 95), 1),
        "consistency": round(_clamp(active_days / 20 * 100, 20, 95), 1),
        "project_progress": round(_clamp(_pct(len(completed), len(effective)), 20, 95), 1),
        "energy_management": round(_clamp(55 + _pct(recovery_minutes, total_minutes) - overload_days * 4, 20, 95), 1),
        "quality": round(_clamp(50 + _pct(len([t for t in completed if t.get("resultSummary") or t.get("isMilestone")]), max(len(completed), 1)) * 0.5, 30, 90), 1),
    }
    labels = {
        "execution": "执行力",
        "focus": "专注力",
        "planning": "计划能力",
        "time_estimation": "时间估算能力",
        "consistency": "持续性",
        "project_progress": "项目推进能力",
        "energy_management": "精力管理能力",
        "quality": "任务完成质量",
    }
    return {
        "isReliable": True,
        "message": "该分数根据日历任务行为估算",
        "scores": [{"key": key, "label": labels[key], "score": value} for key, value in scores.items()],
        "trend": [],
    }


def build_profile(tasks, period_type="week", start_date=None, end_date=None, project_name="", status="all", now=None):
    now = now or datetime.now()
    start, end = period_range(period_type, start_date, end_date, now=now)
    normalized = normalize_tasks(tasks, now=now)
    period_tasks = [t for t in normalized if _in_range(t, start, end)]
    if project_name:
        period_tasks = [t for t in period_tasks if t["projectName"] == project_name]
    if status and status != "all":
        period_tasks = [t for t in period_tasks if t["status"] == status]

    effective = [t for t in period_tasks if t["status"] != "cancelled"]
    completed = [t for t in effective if t["status"] == "completed"]
    delayed = [t for t in effective if t["isDelayed"]]
    planned_minutes = sum(t["estimatedDurationMinutes"] for t in effective)
    actual_minutes = sum(t["actualDurationMinutes"] for t in effective)
    estimated_minutes = sum(t["actualDurationMinutes"] for t in effective if t["actualDurationSource"] == "estimated")
    weighted = sum(t["completionPercentage"] for t in effective) / max(len(effective), 1)
    active_days = len(set(t["dateISO"] for t in effective if t["dateISO"]))

    trend_map = defaultdict(lambda: {"planned": 0, "completed": 0, "weighted": 0, "delay": 0, "actualMinutes": 0})
    for task in effective:
        dt = _parse_date((task["dateISO"] or "") + "T00:00:00")
        if not dt:
            continue
        key = _bucket_key(dt, period_type)
        trend_map[key]["planned"] += 1
        trend_map[key]["completed"] += 1 if task["status"] == "completed" else 0
        trend_map[key]["weighted"] += task["completionPercentage"]
        trend_map[key]["delay"] += 1 if task["isDelayed"] else 0
        trend_map[key]["actualMinutes"] += task["actualDurationMinutes"]
    completion_trend = []
    for key, item in sorted(trend_map.items()):
        completion_trend.append({
            "label": key,
            "completedCount": item["completed"],
            "weightedCompletionRate": round(item["weighted"] / max(item["planned"], 1), 1),
            "delayedCount": item["delay"],
            "actualHours": round(item["actualMinutes"] / 60, 1),
        })

    by_type = _distribution(effective, "taskType")
    energy_distribution = sorted(by_type, key=lambda row: row["energy"], reverse=True)
    project_rows = _distribution(effective, "projectName")
    projects = []
    for row in project_rows:
        project_tasks = [t for t in effective if t["projectName"] == row["key"]]
        projects.append({
            **row,
            "completionRate": round(sum(t["completionPercentage"] for t in project_tasks) / max(len(project_tasks), 1), 1),
            "delayedCount": len([t for t in project_tasks if t["isDelayed"]]),
            "recentAchievement": next(((t.get("resultSummary") or t["title"]) for t in project_tasks if t["status"] == "completed"), ""),
        })

    delay_groups = defaultdict(lambda: {"count": 0, "minutes": 0, "reschedules": 0})
    for task in delayed:
        key = task["taskType"]
        delay_groups[key]["count"] += 1
        delay_groups[key]["minutes"] += task["delayMinutes"]
        delay_groups[key]["reschedules"] += task["rescheduleCount"]
    delay_analysis = [{
        "key": key,
        "label": TASK_TYPES.get(key, {}).get("label", key),
        "count": item["count"],
        "delayRate": _pct(item["count"], len([t for t in effective if t["taskType"] == key])),
        "avgDelayMinutes": round(item["minutes"] / max(item["count"], 1)),
        "avgReschedules": round(item["reschedules"] / max(item["count"], 1), 1),
    } for key, item in delay_groups.items()]

    achievements = [
        {
            "dateISO": t["dateISO"],
            "title": t.get("resultSummary") or t["title"],
            "projectName": t["projectName"],
            "isMilestone": bool(t.get("isMilestone")),
        }
        for t in sorted(effective, key=lambda task: task.get("dateISO") or "", reverse=True)
        if t["status"] == "completed" and (t.get("isMilestone") or t.get("resultSummary") or t["actualDurationMinutes"] >= 120)
    ][:12]

    recovery_minutes = sum(t["actualDurationMinutes"] for t in effective if t["taskType"] in ("rest_recovery", "exercise"))
    overview = {
        "periodType": period_type,
        "periodStart": start.strftime("%Y-%m-%d"),
        "periodEnd": end.strftime("%Y-%m-%d"),
        "plannedTaskCount": len(effective),
        "completedTaskCount": len(completed),
        "fullCompletionRate": _pct(len(completed), len(effective)),
        "weightedCompletionRate": round(weighted, 1),
        "plannedDurationMinutes": planned_minutes,
        "actualDurationMinutes": actual_minutes,
        "actualDurationHours": round(actual_minutes / 60, 1),
        "estimatedActualMinutes": estimated_minutes,
        "delayedTaskCount": len(delayed),
        "delayedRate": _pct(len(delayed), len(effective)),
        "avgDelayMinutes": round(sum(t["delayMinutes"] for t in delayed) / max(len(delayed), 1)),
        "cancelledTaskCount": len([t for t in period_tasks if t["status"] == "cancelled"]),
        "totalEnergyCost": round(sum(t["energyInvestment"] for t in effective), 1),
        "totalRecoveryValue": round(sum(t["recoveryInvestment"] for t in effective), 1),
        "recoveryMinutes": recovery_minutes,
        "recoveryRate": _pct(recovery_minutes, actual_minutes),
        "activeDays": active_days,
        "hasEstimatedActualTime": estimated_minutes > 0,
    }

    return {
        "overview": overview,
        "tasks": effective,
        "completionTrend": completion_trend,
        "taskTypeDistribution": by_type,
        "energyDistribution": energy_distribution,
        "plannedVsActual": [
            {"label": row["label"], "plannedMinutes": sum(t["estimatedDurationMinutes"] for t in effective if t["taskType"] == row["key"]), "actualMinutes": row["minutes"]}
            for row in by_type
        ],
        "delayAnalysis": sorted(delay_analysis, key=lambda row: row["count"], reverse=True),
        "projects": projects,
        "competencyScores": _competencies(effective, active_days),
        "achievements": achievements,
        "projectOptions": sorted(set(t["projectName"] for t in normalized if t.get("projectName"))),
        "statusOptions": [{"key": key, "label": label} for key, label in STATUS_LABELS.items()],
        "sourceDataHash": hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16],
    }


def rule_summary(profile, summary_type="daily"):
    overview = profile.get("overview", {})
    delayed = overview.get("delayedTaskCount", 0)
    completed = overview.get("completedTaskCount", 0)
    planned = overview.get("plannedTaskCount", 0)
    top_type = (profile.get("energyDistribution") or [{}])[0].get("label", "暂无明显方向")
    achievements = [item["title"] for item in profile.get("achievements", [])[:3]]
    if not achievements:
        achievements = ["本周期暂无可识别的阶段成果"]
    problems = []
    if overview.get("hasEstimatedActualTime"):
        problems.append("部分任务缺少真实实际用时，当前时间分析包含估算")
    if delayed:
        problems.append(f"有 {delayed} 项任务出现延期")
    if not problems:
        problems.append("没有明显延期问题，继续保持计划与执行同步")

    return {
        "overview": f"本周期计划 {planned} 项任务，完成 {completed} 项，加权完成率 {overview.get('weightedCompletionRate', 0)}%。",
        "achievements": achievements,
        "problems": problems,
        "energyInsight": f"精力投入最高的方向是{top_type}，总精力投入值为 {overview.get('totalEnergyCost', 0)}。",
        "planningInsight": "当前判断基于日历任务行为估算；实际用时越完整，计划能力分析越稳定。",
        "tomorrowSuggestion": [
            "优先处理已延期且未完成的任务",
            "为高精力任务预留连续时间段",
            "保留至少一段恢复或身体活动时间",
        ],
        "mainFocus": "降低延期任务风险" if delayed else "保持当前任务推进节奏",
        "summaryType": summary_type,
        "source": "rule_fallback",
    }


def validate_summary(content):
    if not isinstance(content, dict):
        return None
    required = ["overview", "achievements", "problems", "energyInsight", "planningInsight", "tomorrowSuggestion", "mainFocus"]
    if any(key not in content for key in required):
        return None
    content["achievements"] = content.get("achievements") if isinstance(content.get("achievements"), list) else []
    content["problems"] = content.get("problems") if isinstance(content.get("problems"), list) else []
    content["tomorrowSuggestion"] = content.get("tomorrowSuggestion") if isinstance(content.get("tomorrowSuggestion"), list) else []
    return content


def reschedule_suggestions(task, tasks, now=None):
    now = now or datetime.now()
    normalized_task = normalize_task(task, now=now)
    remaining = max(30, round(normalized_task["estimatedDurationMinutes"] * (100 - normalized_task["completionPercentage"]) / 100))
    existing = normalize_tasks(tasks, now=now)
    start_day = max(now, _parse_date((normalized_task.get("dateISO") or now.strftime("%Y-%m-%d")) + "T00:00:00") or now)
    work_start, work_end = 9 * 60, 22 * 60

    candidates = []
    for offset in range(7):
        day = (start_day + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_iso = day.strftime("%Y-%m-%d")
        occupied = []
        for item in existing:
            if item["id"] == normalized_task["id"] or item["dateISO"] != day_iso or item["status"] == "cancelled":
                continue
            start = _parse_minutes(item["startTime"])
            end = _parse_minutes(item["endTime"])
            if start is not None and end is not None and end > start:
                occupied.append((start, end))
        occupied.sort()
        cursor = work_start
        for start, end in occupied:
            if cursor + remaining <= start:
                candidates.append((day_iso, cursor, cursor + remaining, len(occupied)))
            cursor = max(cursor, end)
        if cursor + remaining <= work_end:
            candidates.append((day_iso, cursor, cursor + remaining, len(occupied)))
        if len(candidates) >= 6:
            break

    labels = [
        ("earliest", "最早可执行时间"),
        ("balanced", "负荷最均衡时间"),
        ("lowest_risk", "风险最低时间"),
    ]
    suggestions = []
    if not candidates:
        return suggestions
    balanced = min(candidates, key=lambda item: (item[3], item[0], item[1]))
    lowest = max(candidates, key=lambda item: (item[2] - item[1], -item[3]))
    chosen = [candidates[0], balanced, lowest]
    for (kind, label), candidate in zip(labels, chosen):
        date_iso, start, end, density = candidate
        suggestions.append({
            "suggestionId": hashlib.md5(f"{normalized_task['id']}-{kind}-{date_iso}-{start}".encode()).hexdigest()[:12],
            "suggestionType": kind,
            "label": label,
            "taskId": normalized_task["id"],
            "suggestedDate": date_iso,
            "suggestedStart": _format_minutes(start),
            "suggestedEnd": _format_minutes(end),
            "durationMinutes": remaining,
            "hasConflict": False,
            "dayLoad": density,
            "confidence": max(55, min(92, 88 - density * 8 + (10 if kind == "lowest_risk" else 0))),
            "reason": f"{label}：当天已有 {density} 项任务，保留 {remaining} 分钟处理剩余工作量。",
        })
    return suggestions
