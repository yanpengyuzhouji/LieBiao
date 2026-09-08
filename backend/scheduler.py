from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import beijing_time, get_db, log_event, now_iso
from .service import run_crawl, try_create_run


_EVERY_RE = re.compile(r"每\s*(\d+)\s*(分钟|分|小时|时)")
_CLOCK_RE = re.compile(r"(?:每天|工作日)\s*(\d{1,2}):(\d{2})")


def parse_schedule(schedule_text: str) -> dict[str, Any] | None:
    """Parse the human-readable V1 schedule format into a deterministic rule."""
    text = re.sub(r"\s+", "", schedule_text or "")
    if not text or text in {"手动", "手动运行", "不自动运行"}:
        return None
    match = _EVERY_RE.fullmatch(text)
    if match:
        amount = int(match.group(1))
        if amount <= 0:
            return None
        unit = "hours" if match.group(2) in {"小时", "时"} else "minutes"
        return {"kind": "interval", "seconds": amount * (3600 if unit == "hours" else 60)}
    match = _CLOCK_RE.fullmatch(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return {"kind": "clock", "hour": hour, "minute": minute, "weekdays": text.startswith("工作日")}
    return None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")


def is_due(schedule_text: str, last_run_at: str | None, created_at: str | None, now: datetime | None = None, timezone_name: str = "Asia/Shanghai", schedule_anchor_at: str | None = None) -> bool:
    rule = parse_schedule(schedule_text)
    if not rule:
        return False
    local_zone = _zone(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current_local = current.astimezone(local_zone)
    previous_text = schedule_anchor_at or last_run_at or created_at
    if not previous_text:
        return True
    try:
        previous = datetime.fromisoformat(previous_text)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        previous_local = previous.astimezone(local_zone)
    except ValueError:
        return True
    if rule["kind"] == "interval":
        return (current - previous.astimezone(timezone.utc)).total_seconds() >= rule["seconds"]
    if rule["weekdays"] and current_local.weekday() >= 5:
        return False
    target = current_local.replace(hour=rule["hour"], minute=rule["minute"], second=0, microsecond=0)
    return current_local >= target and previous_local < target


class Scheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_tick: str | None = None
        self._last_error: str | None = None
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._started = True
            self._thread = threading.Thread(target=self._loop, name="lieBiao-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        self._thread = None

    def status(self) -> dict[str, Any]:
        thread = self._thread
        with get_db() as connection:
            running = connection.execute("SELECT COUNT(*) FROM crawl_runs WHERE status IN ('queued','running')").fetchone()[0]
        return {"enabled": bool(thread and thread.is_alive()), "timezone": "Asia/Shanghai", "timezone_label": "北京时间（UTC+08:00）", "last_tick": beijing_time(self._last_tick), "last_error": self._last_error, "running_count": running, "supported_formats": ["每 N 分钟", "每 N 小时", "每天 HH:MM", "工作日 HH:MM", "手动"]}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # scheduler must not terminate the API process
                self._last_error = str(exc)
            self._stop.wait(10)

    def _tick(self) -> None:
        current = datetime.now(timezone.utc)
        self._last_tick = current.isoformat(timespec="seconds")
        with get_db() as connection:
            jobs = connection.execute("SELECT id,name,schedule_text,timezone,created_at,last_run_at,schedule_anchor_at,enabled FROM crawl_jobs WHERE enabled=1").fetchall()
        for job in jobs:
            if not is_due(job["schedule_text"], job["last_run_at"], job["created_at"], current, "Asia/Shanghai", job["schedule_anchor_at"]):
                continue
            run_id = try_create_run(job["id"])
            if run_id:
                with get_db() as connection:
                    log_event(connection, "scheduler.queue", f"定时调度任务：{job['name']}", crawl_run_id=run_id)
                worker = threading.Thread(target=run_crawl, args=(job["id"], run_id, {}), name=f"lieBiao-crawl-{run_id}", daemon=True)
                worker.start()


scheduler = Scheduler()
