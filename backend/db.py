from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .config import settings
from .maintenance import activity


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def beijing_time(value: str | None) -> str | None:
    """Return a timestamp formatted in Beijing time for API/UI display.

    Stored timestamps remain UTC. Date strings without an offset are source
    publication times and are therefore treated as already being local time.
    """
    if not value:
        return value
    text = str(value).strip()
    if not text or len(text) <= 10:
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ).isoformat(timespec="seconds")


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    with activity():
        with _connection() as connection:
            yield connection


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    settings.ensure_dirs()
    connection = sqlite3.connect(settings.db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    adapter TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    public_mode TEXT NOT NULL DEFAULT 'public',
    rate_limit_ms INTEGER NOT NULL DEFAULT 1500,
    health_status TEXT NOT NULL DEFAULT 'unknown',
    health_message TEXT,
    last_checked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    username TEXT,
    login_mode TEXT NOT NULL DEFAULT 'manual_session',
    credential_ref TEXT,
    session_status TEXT NOT NULL DEFAULT 'not_verified',
    last_login_at TEXT,
    expires_at TEXT,
    status_reason TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keyword_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    include_any_json TEXT NOT NULL DEFAULT '[]',
    include_all_json TEXT NOT NULL DEFAULT '[]',
    phrases_json TEXT NOT NULL DEFAULT '[]',
    exclude_json TEXT NOT NULL DEFAULT '[]',
    synonyms_json TEXT NOT NULL DEFAULT '{}',
    scopes_json TEXT NOT NULL DEFAULT '["title", "body", "attachment_name", "attachment_body"]',
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    account_id INTEGER REFERENCES site_accounts(id),
    keyword_group_id INTEGER REFERENCES keyword_groups(id),
    categories_json TEXT NOT NULL DEFAULT '["招标公告"]',
    schedule_text TEXT NOT NULL DEFAULT '每 30 分钟',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    lookback_days INTEGER NOT NULL DEFAULT 1,
    max_pages INTEGER NOT NULL DEFAULT 5,
    max_notices INTEGER NOT NULL DEFAULT 100,
    concurrency INTEGER NOT NULL DEFAULT 1,
    interval_ms INTEGER NOT NULL DEFAULT 1500,
    retry_json TEXT NOT NULL DEFAULT '{"max_attempts":3}',
    download_attachments INTEGER NOT NULL DEFAULT 1,
    max_file_size_mb INTEGER NOT NULL DEFAULT 500,
    ocr_enabled INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    schedule_anchor_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    started_at TEXT,
    finished_at TEXT,
    discovered INTEGER NOT NULL DEFAULT 0,
    detail_success INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    filtered_count INTEGER NOT NULL DEFAULT 0,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    parsed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER REFERENCES sites(id),
    external_id TEXT,
    source_type TEXT NOT NULL DEFAULT 'crawl',
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    notice_type TEXT,
    published_at TEXT,
    opening_at TEXT,
    project_number TEXT,
    project_name TEXT,
    demand_unit TEXT,
    procuring_agent TEXT,
    project_location TEXT,
    budget TEXT,
    summary TEXT,
    ingest_status TEXT NOT NULL DEFAULT 'discovered',
    business_mark TEXT NOT NULL DEFAULT 'pending',
    current_version INTEGER NOT NULL DEFAULT 1,
    content_fingerprint TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(site_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_notices_published ON notices(published_at);
CREATE INDEX IF NOT EXISTS idx_notices_status ON notices(ingest_status, business_mark);
CREATE TABLE IF NOT EXISTS notice_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    raw_html_path TEXT,
    body_text TEXT,
    published_at TEXT,
    content_fingerprint TEXT,
    captured_at TEXT NOT NULL,
    UNIQUE(notice_id, version_no)
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
    parent_attachment_id INTEGER REFERENCES attachments(id),
    name TEXT NOT NULL,
    source_url TEXT,
    relative_path TEXT,
    mime_type TEXT,
    sha256 TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'discovered',
    is_key_file INTEGER NOT NULL DEFAULT 0,
    parse_status TEXT NOT NULL DEFAULT 'pending',
    error_message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_notice ON attachments(notice_id);
CREATE TABLE IF NOT EXISTS extracted_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id INTEGER NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
    text_content TEXT,
    structure_json TEXT NOT NULL DEFAULT '{}',
    preview_path TEXT,
    parser TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extracted_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    value TEXT,
    source_attachment_id INTEGER REFERENCES attachments(id),
    source_location TEXT,
    extraction_method TEXT,
    confidence REAL,
    manual_value TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keyword_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
    keyword_group_id INTEGER REFERENCES keyword_groups(id),
    keyword TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_file TEXT,
    location TEXT,
    snippet TEXT,
    context_before TEXT,
    context_after TEXT,
    is_negative INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_keyword_hits_notice ON keyword_hits(notice_id);
CREATE TABLE IF NOT EXISTS import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_name TEXT,
    preview_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'preflight',
    total_count INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL DEFAULT 'INFO',
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    notice_id INTEGER REFERENCES notices(id),
    crawl_run_id INTEGER REFERENCES crawl_runs(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def json_load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def init_db() -> None:
    with get_db() as connection:
        connection.executescript(SCHEMA)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(crawl_jobs)").fetchall()}
        if "schedule_anchor_at" not in columns:
            connection.execute("ALTER TABLE crawl_jobs ADD COLUMN schedule_anchor_at TEXT")
        run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(crawl_runs)").fetchall()}
        if "filtered_count" not in run_columns:
            connection.execute("ALTER TABLE crawl_runs ADD COLUMN filtered_count INTEGER NOT NULL DEFAULT 0")
        if "created_count" not in run_columns:
            connection.execute("ALTER TABLE crawl_runs ADD COLUMN created_count INTEGER NOT NULL DEFAULT 0")
        if "duplicate_count" not in run_columns:
            connection.execute("ALTER TABLE crawl_runs ADD COLUMN duplicate_count INTEGER NOT NULL DEFAULT 0")
        extracted_cleanup = connection.execute("SELECT 1 FROM app_settings WHERE key=?", ("migration.extracted_documents.unique.v1",)).fetchone()
        if not extracted_cleanup:
            connection.execute("DELETE FROM extracted_documents WHERE id NOT IN (SELECT MAX(id) FROM extracted_documents GROUP BY attachment_id)")
            connection.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)", ("migration.extracted_documents.unique.v1", "1", now_iso()))
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_extracted_documents_attachment_unique ON extracted_documents(attachment_id)")
        cleanup_marker = connection.execute("SELECT 1 FROM app_settings WHERE key=?", ("migration.keyword_hits.v1",)).fetchone()
        if not cleanup_marker:
            # 旧版本删除关键词组时把命中记录的外键置空；这些记录已经没有
            # 可解释的规则归属，不能继续作为当前命中展示。
            connection.execute("DELETE FROM keyword_hits WHERE keyword_group_id IS NULL")
            connection.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)", ("migration.keyword_hits.v1", "1", now_iso()))
        unmatched_marker = connection.execute("SELECT 1 FROM app_settings WHERE key=?", ("migration.unmatched_crawl.v1",)).fetchone()
        if not unmatched_marker:
            # 旧版本把所有发现的公告都写入了公告库。仅软删除历史采集且
            # 没有正向命中的记录，保留追溯能力，不影响人工导入数据。
            timestamp = now_iso()
            connection.execute(
                "UPDATE notices SET deleted_at=?,updated_at=? "
                "WHERE source_type='crawl' AND deleted_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM keyword_hits h WHERE h.notice_id=notices.id AND h.is_negative=0)",
                (timestamp, timestamp),
            )
            connection.execute("INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)", ("migration.unmatched_crawl.v1", "1", timestamp))
        seed_base_data(connection)


def recover_incomplete_runs() -> int:
    """Close batches left active by an application/process interruption."""
    timestamp = now_iso()
    recovered = 0
    with get_db() as connection:
        rows = connection.execute("SELECT id FROM crawl_runs WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            reason = "应用重启，上一批次未完成；请重新运行任务"
            connection.execute("UPDATE crawl_runs SET status='failed',finished_at=?,failure_reason=? WHERE id=?", (timestamp, reason, row["id"]))
            log_event(connection, "crawl.recovered", reason, "WARNING", crawl_run_id=row["id"])
            recovered += 1
    return recovered


def seed_base_data(connection: sqlite3.Connection) -> None:
    timestamp = now_iso()
    # 基础数据只允许在首次初始化时生成一次。旧逻辑每次启动都会
    # 在找不到默认关键词组时补种，导致用户删除后重启又出现。
    seeded = connection.execute(
        "SELECT 1 FROM app_settings WHERE key = ? LIMIT 1",
        ("seed.base_data.v1",),
    ).fetchone()
    existing_data = connection.execute(
        """SELECT 1 WHERE EXISTS (SELECT 1 FROM sites)
        OR EXISTS (SELECT 1 FROM keyword_groups)
        OR EXISTS (SELECT 1 FROM crawl_jobs)
        OR EXISTS (SELECT 1 FROM notices) LIMIT 1"""
    ).fetchone()
    # 只有真正的空数据库才创建默认关键词组和默认任务；老版本数据库
    # 即使用户已删掉默认组，也不能在升级或重启时再次补种。
    first_install = not seeded and not existing_data
    sites = [
        ("csg", "南方电网", "https://www.bidding.csg.cn/", "csg", "public"),
        ("ecp", "ECP2.0", "https://ecp.sgcc.com.cn/ecp2.0/portal/#/", "ecp", "public_or_session"),
        ("sgcc", "国网交易专区", "https://sgccetp.com.cn/portal/#/", "sgcc", "public_or_session"),
    ]
    for code, name, url, adapter, public_mode in sites:
        connection.execute(
            "INSERT OR IGNORE INTO sites(code,name,base_url,adapter,public_mode,health_status,created_at) VALUES(?,?,?,?,?,'unknown',?)",
            (code, name, url, adapter, public_mode, timestamp),
        )
    keyword = connection.execute("SELECT id FROM keyword_groups WHERE name = '储能与新能源'").fetchone()
    if first_install and not keyword:
        connection.execute(
            """INSERT INTO keyword_groups(name,category,include_any_json,include_all_json,phrases_json,exclude_json,synonyms_json,priority,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "储能与新能源", "新能源业务", json.dumps(["储能", "电池储能", "BESS"], ensure_ascii=False),
                json.dumps(["采购", "招标"], ensure_ascii=False), json.dumps([], ensure_ascii=False),
                json.dumps(["招聘", "培训"], ensure_ascii=False), json.dumps({"储能变流器": ["PCS"]}, ensure_ascii=False),
                100, timestamp, timestamp,
            ),
        )
    site_count = connection.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    job_count = connection.execute("SELECT COUNT(*) FROM crawl_jobs").fetchone()[0]
    if first_install and site_count and job_count == 0:
        csg_id = connection.execute("SELECT id FROM sites WHERE code='csg'").fetchone()[0]
        keyword_id = connection.execute("SELECT id FROM keyword_groups WHERE name='储能与新能源'").fetchone()[0]
        connection.execute(
            """INSERT INTO crawl_jobs(name,site_id,keyword_group_id,schedule_text,lookback_days,max_pages,max_notices,interval_ms,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            ("南方电网 · 招标公告增量采集", csg_id, keyword_id, "每 30 分钟", 1, 5, 100, 1500, timestamp),
        )
    connection.execute(
        "INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
        ("seed.base_data.v1", "1", timestamp),
    )
    defaults = {
        "storage.root": str(settings.data_dir),
        "storage.raw": str(settings.raw_dir),
        "storage.extracted": str(settings.extracted_dir),
        "storage.preview": str(settings.preview_dir),
        "storage.temp": str(settings.temp_dir),
        "storage.logs": str(settings.log_dir),
        "storage.max_attachment_mb": str(settings.max_attachment_mb),
        "storage.max_archive_mb": str(settings.max_archive_mb),
    }
    for key, value in defaults.items():
        connection.execute("INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)", (key, value, timestamp))


def log_event(connection: sqlite3.Connection, event_type: str, message: str, level: str = "INFO", notice_id: int | None = None, crawl_run_id: int | None = None) -> None:
    connection.execute(
        "INSERT INTO system_logs(level,event_type,message,notice_id,crawl_run_id,created_at) VALUES(?,?,?,?,?,?)",
        (level, event_type, message, notice_id, crawl_run_id, now_iso()),
    )


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None
