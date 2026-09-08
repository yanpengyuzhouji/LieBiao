from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from .adapters import AdapterError, BaseAdapter, NoticeData, first_date, make_adapter
from .config import settings
from .db import beijing_time, get_db, json_load, log_event, now_iso
from .matching import match_sources
from .maintenance import tracked
from .parsers import archive_type, file_mime, is_office_lock_file, parse_document, safe_extract_zip, sha256_file
from .storage import absolute_from_relative, attachment_directory, extraction_directory, relative_to_data, safe_name, save_raw_html


STATUS_LABELS = {
    "discovered": "已发现", "detail_collected": "详情已采集", "attachments_processing": "附件处理中",
    "parsed": "解析完成", "partial": "部分解析成功", "failed": "采集失败", "pending_login": "待登录",
}
MARK_LABELS = {"pending": "待确认", "relevant": "相关", "irrelevant": "不相关", "supplement": "待补充", "focus": "重点关注", "processed": "已处理"}
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def ensure_run_tracking_schema(connection) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(crawl_runs)")}
    additions = {
        "config_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        "heartbeat_at": "TEXT",
        "stop_reason": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE crawl_runs ADD COLUMN {name} {definition}")


def ensure_notice_binding_schema(connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notice_keyword_bindings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
        job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
        keyword_group_id INTEGER NOT NULL REFERENCES keyword_groups(id) ON DELETE CASCADE,
        first_run_id INTEGER REFERENCES crawl_runs(id) ON DELETE SET NULL,
        last_run_id INTEGER REFERENCES crawl_runs(id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(notice_id,job_id,keyword_group_id))"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_notice_keyword_bindings_notice ON notice_keyword_bindings(notice_id)")


def parse_notice_datetime(value: str | None) -> datetime | None:
    """Parse a source publication time and normalize it to Beijing time."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).strip())
    if not text:
        return None
    normalized = (
        text.replace("年", "-").replace("月", "-").replace("日", "")
        .replace("/", "-").replace("：", ":").replace("Z", "+00:00")
    )
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        match = re.fullmatch(
            r"(?:发布时间|发布日期|公告时间|发布于)?\s*:?\s*(20\d{2})[-.]?(\d{1,2})[-.]?(\d{1,2})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*",
            normalized,
        )
        if not match:
            return None
        try:
            parsed = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4) or 0), int(match.group(5) or 0), int(match.group(6) or 0),
            )
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed.astimezone(BEIJING_TZ)


def lookback_cutoff(lookback_days: int, reference_time: datetime | None = None) -> datetime:
    """Return the inclusive Beijing-time day boundary for a crawl run."""
    current = reference_time or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    local = current.astimezone(BEIJING_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(0, int(lookback_days)))


def notice_policy_rejection(
    published_at: str | None,
    notice_type: str | None,
    cutoff: datetime,
    allowed_categories: set[str],
) -> str | None:
    """Return a rejection reason when a candidate violates crawl policy."""
    normalized_type = str(notice_type or "").strip()
    def category_family(value: str) -> str:
        compact = re.sub(r"\s+", "", value)
        if compact in {"变更公告", "招标公告变更", "招标变更公告"} or ("招标" in compact and "变更" in compact):
            return "招标公告"
        return compact
    normalized_allowed = {category_family(item) for item in allowed_categories}
    if normalized_allowed and category_family(normalized_type) not in normalized_allowed:
        return f"公告类型“{normalized_type or '未知'}”不在任务范围"
    published = parse_notice_datetime(published_at)
    if published is None:
        return "缺少可解析的发布时间"
    if published < cutoff:
        return f"发布时间 {published.strftime('%Y-%m-%d %H:%M')} 早于回溯边界 {cutoff.strftime('%Y-%m-%d 00:00')}"
    return None


def frontend_status(status: str) -> str:
    return {"parsed": "done", "detail_collected": "processing", "attachments_processing": "processing", "partial": "partial", "failed": "failed"}.get(status, "processing")


def opening_countdown(value: str | None) -> str:
    opening = parse_notice_datetime(value)
    if opening is None:
        return "待确认"
    seconds = (opening - datetime.now(BEIJING_TZ)).total_seconds()
    if seconds < 0:
        return "已截止"
    days = int((seconds + 86399) // 86400)
    return "今天" if days == 0 else f"{days} 天后"


def site_name(connection, site_id: int | None) -> tuple[str, str, str]:
    if not site_id:
        return "人工导入", "", ""
    row = connection.execute("SELECT code,name,base_url FROM sites WHERE id=?", (site_id,)).fetchone()
    return (row["code"], row["name"], row["base_url"]) if row else ("unknown", "未知平台", "")


def get_hits(connection, notice_id: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT h.*,g.name AS keyword_group_name FROM keyword_hits h "
        "JOIN keyword_groups g ON g.id=h.keyword_group_id "
        "WHERE h.notice_id=? AND h.is_negative=0 ORDER BY h.id",
        (notice_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_files(connection, notice_id: int) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM attachments WHERE notice_id=? ORDER BY parent_attachment_id IS NOT NULL, id", (notice_id,)).fetchall()
    return [dict(row) for row in rows]


def text_fragment_url(source_url: str | None, keyword: str) -> str | None:
    if not source_url or not keyword:
        return source_url
    directive = f":~:text={quote(keyword, safe='')}"
    if "#" in source_url:
        return f"{source_url}{'' if source_url.endswith('#') else '&'}{directive}"
    return f"{source_url}#{directive}"


def frontend_notice(connection, row) -> dict[str, Any]:
    code, name, base_url = site_name(connection, row["site_id"])
    hits = get_hits(connection, row["id"])
    files = get_files(connection, row["id"])
    published = beijing_time(row["published_at"] or row["created_at"]) or row["created_at"]
    opening = (beijing_time(row["opening_at"]) or "待确认").replace("T", " ")[:19]
    hit_names = list(dict.fromkeys(hit["keyword"] for hit in hits))
    matching_rules = list(dict.fromkeys(hit["keyword_group_name"] for hit in hits if hit.get("keyword_group_name")))
    hit_tones = ["mint" if any(char in keyword for char in ("储能", "新能源", "配网", "在线")) else "orange" if keyword else "" for keyword in hit_names]
    key_count = sum(1 for file in files if file["is_key_file"])
    file_urls = {
        file["name"]: f"/api/files/{file['id']}/open-location"
        for file in files
        if file["relative_path"]
    }
    evidence = [{
        "id": str(hit["id"]), "key": hit["keyword"], "loc": hit["location"] or "正文", "copy": hit["snippet"] or "命中关键词：" + hit["keyword"],
        "source": (hit["source_file"] or "公告正文.html"), "sourceType": hit["source_type"],
        "locatorUrl": file_urls.get(hit["source_file"]) if hit["source_type"].startswith("attachment") else text_fragment_url(row["source_url"], hit["keyword"]),
        "locatorFileId": next((str(file["id"]) for file in files if file["name"] == hit["source_file"] and file["relative_path"]), None) if hit["source_type"].startswith("attachment") else None,
        "locatorLabel": "定位本地文件" if hit["source_type"].startswith("attachment") and file_urls.get(hit["source_file"]) else "附件未落盘" if hit["source_type"].startswith("attachment") else "定位 ↗",
    } for hit in hits]
    file_items = [{
        "id": str(file["id"]), "name": file["name"], "type": Path(file["name"]).suffix.lower().lstrip(".") or "file", "size": format_bytes(file["size_bytes"]),
        "nested": bool(file["parent_attachment_id"]), "key": bool(file["is_key_file"]), "error": file["error_message"],
        "previewUrl": f"/api/files/{file['id']}/preview" if file["relative_path"] else None,
        "openLocationUrl": f"/api/files/{file['id']}/open-location" if file["relative_path"] else None,
    } for file in files]
    detail = {
        "project": row["project_name"] or row["title"], "agent": row["procuring_agent"] or "—", "location": row["project_location"] or "待确认",
        "budget": row["budget"] or "待确认", "method": "公开招标",
    }
    return {
        "id": str(row["id"]), "external_id": row["external_id"], "platform": code, "platformName": name, "platformClass": code,
        "type": row["notice_type"] or "招标公告", "title": row["title"], "date": published.replace("T", " ")[:19], "opening": opening,
        "openingText": opening_countdown(row["opening_at"]), "number": row["project_number"] or "待确认", "unit": row["demand_unit"] or "待确认",
        "summary": row["summary"] or "待生成项目摘要", "hits": hit_names, "matchingRules": matching_rules, "hitTone": hit_tones, "attachments": len(files), "keyFiles": key_count,
        "status": frontend_status(row["ingest_status"]), "statusText": STATUS_LABELS.get(row["ingest_status"], row["ingest_status"]),
        "mark": row["business_mark"], "markText": MARK_LABELS.get(row["business_mark"], row["business_mark"]),
        "bestHit": hits[0]["snippet"] if hits else "暂未命中关键词", "sourceUrl": row["source_url"], "detail": detail,
        "evidence": evidence, "files": file_items,
        "isDeleted": bool(row["deleted_at"]), "deletedAt": beijing_time(row["deleted_at"]) if row["deleted_at"] else None,
    }


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def find_site(connection, source_url: str, preferred_code: str | None = None):
    hostname = (urlparse(source_url).hostname or "").lower().rstrip(".")
    def matches(row) -> bool:
        site_hostname = (urlparse(row["base_url"]).hostname or "").lower().rstrip(".")
        return bool(site_hostname and (hostname == site_hostname or hostname.endswith("." + site_hostname)))
    if preferred_code:
        row = connection.execute("SELECT * FROM sites WHERE code=?", (preferred_code,)).fetchone()
        if row and matches(row):
            return row
    for row in connection.execute("SELECT * FROM sites").fetchall():
        if matches(row):
            return row
    return None


def extract_field(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" ：:;；，,。")
    return None


def derive_fields(data: NoticeData) -> dict[str, tuple[str | None, str, float]]:
    body = data.body_text or ""
    number = extract_field([r"(?:项目\s*编号|招标\s*编号|采购\s*编号|标段\s*编号)[：:\s]*([A-Za-z0-9][A-Za-z0-9_./-]{3,})"], body)
    unit = extract_field([
        r"招标人为\s*([^\n。；;]{2,80})",
        r"(?:需求\s*单位|采购人|招标人|项目\s*单位)[：:\s]+([^\n。；;/]{2,80})",
    ], body)
    location = extract_field([r"(?:项目地点|交货地点|实施地点)[：:\s]+([^\n。；;]{2,80})"], body)
    budget = extract_field([r"(?:预算金额|项目预算|最高限价|采购预算)[：:\s]+([^\n。；;]{1,50})"], body)
    agent = extract_field([r"(?:招标代理机构|采购代理机构|代理机构)[：:\s]+([^\n。；;]{2,100})"], body)
    return {
        "project_number": (number, "正文 · 字段标签", .88), "project_name": (data.title, "详情页标题", .95),
        "demand_unit": (unit, "正文 · 字段标签", .9), "procuring_agent": (agent, "正文 · 字段标签", .9),
        "project_location": (location, "正文 · 字段标签", .86), "budget": (budget, "正文 · 字段标签", .86),
    }


def upsert_field(connection, notice_id: int, field_name: str, value: str | None, source_location: str, confidence: float, attachment_id: int | None = None) -> None:
    if not value:
        return
    timestamp = now_iso()
    connection.execute("DELETE FROM extracted_fields WHERE notice_id=? AND field_name=? AND manual_value IS NULL", (notice_id, field_name))
    connection.execute(
        "INSERT INTO extracted_fields(notice_id,field_name,value,source_attachment_id,source_location,extraction_method,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (notice_id, field_name, value, attachment_id, source_location, "rule", confidence, timestamp, timestamp),
    )


def create_attachment(connection, notice_id: int, name: str, source_url: str | None, status: str = "discovered", parent_id: int | None = None) -> int:
    timestamp = now_iso()
    existing = connection.execute("SELECT id FROM attachments WHERE notice_id=? AND name=? AND IFNULL(source_url,'')=IFNULL(?, '') AND parent_attachment_id IS ?", (notice_id, name, source_url, parent_id)).fetchone()
    if existing:
        return int(existing["id"])
    cursor = connection.execute(
        "INSERT INTO attachments(notice_id,parent_attachment_id,name,source_url,status,created_at) VALUES(?,?,?,?,?,?)",
        (notice_id, parent_id, name, source_url, status, timestamp),
    )
    return int(cursor.lastrowid)


def attachment_tree_needs_reparse(connection, attachment_id: int) -> bool:
    return connection.execute(
        """WITH RECURSIVE tree(id,name,parse_status) AS (
        SELECT id,name,parse_status FROM attachments WHERE id=?
        UNION ALL SELECT a.id,a.name,a.parse_status FROM attachments a JOIN tree t ON a.parent_attachment_id=t.id)
        SELECT 1 FROM tree WHERE parse_status IN ('failed','pending')
        OR (parse_status='unsupported' AND lower(name) LIKE '%.doc') LIMIT 1""",
        (attachment_id,),
    ).fetchone() is not None


def save_streamed_attachment(adapter: BaseAdapter, notice_id: int, attachment_id: int, name: str, source_url: str) -> tuple[Path, int, str]:
    directory = attachment_directory(notice_id)
    path = directory / safe_name(name)
    stem, suffix = path.stem, path.suffix
    counter = 1
    while path.exists():
        path = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    digest = hashlib.sha256()
    total = 0
    max_bytes = settings.max_attachment_mb * 1024 * 1024
    try:
        with adapter.client.stream("GET", source_url) as response:
            response.raise_for_status()
            advertised = int(response.headers.get("content-length", "0") or 0)
            if advertised > max_bytes:
                raise ValueError(f"附件超过大小限制（{settings.max_attachment_mb} MB）")
            with path.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"附件超过大小限制（{settings.max_attachment_mb} MB）")
                    digest.update(chunk)
                    handle.write(chunk)
    except Exception:
        if path.exists():
            path.unlink()
        raise
    return path, total, digest.hexdigest()


def process_local_attachment(connection, notice_id: int, attachment_id: int, path: Path, name: str, parent_id: int | None = None) -> list[dict[str, str]]:
    """Persist parser output and return searchable source blocks."""
    sources: list[dict[str, str]] = []
    if is_office_lock_file(name):
        connection.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
        return sources
    if archive_type(path):
        if path.suffix.lower() != ".zip":
            error = "RAR/7z 需要在部署机安装对应解压组件，系统不会执行或暴力破解压缩包"
            connection.execute("UPDATE attachments SET status='needs_tool',parse_status='unsupported',error_message=? WHERE id=?", (error, attachment_id))
            return sources
        try:
            connection.commit()
            destination = extraction_directory(notice_id) / safe_name(path.stem)
            extracted = safe_extract_zip(path, destination, settings.max_archive_mb * 1024 * 1024, settings.max_expanded_mb * 1024 * 1024, settings.max_archive_files, settings.max_archive_depth)
            connection.execute("UPDATE attachments SET status='extracted',parse_status='parsed',error_message=NULL WHERE id=?", (attachment_id,))
            for child in extracted:
                child_name = child.relative_to(destination).as_posix()
                relative = relative_to_data(child)
                existing_child = connection.execute(
                    "SELECT id FROM attachments WHERE notice_id=? AND parent_attachment_id=? AND relative_path=?",
                    (notice_id, attachment_id, relative),
                ).fetchone()
                child_id = int(existing_child["id"]) if existing_child else create_attachment(connection, notice_id, child_name, None, "extracted", attachment_id)
                connection.execute("UPDATE attachments SET relative_path=?,mime_type=?,size_bytes=?,sha256=? WHERE id=?", (relative, file_mime(child), child.stat().st_size, sha256_file(child), child_id))
                sources.extend(process_local_attachment(connection, notice_id, child_id, child, child_name, attachment_id))
            return sources
        except Exception as exc:
            error = str(exc)
            connection.execute("UPDATE attachments SET status='failed',parse_status='failed',error_message=? WHERE id=?", (error, attachment_id))
            return sources
    # Parsing may call Word for tens of seconds; release SQLite's writer first.
    connection.commit()
    result = parse_document(path)
    if result.status == "parsed":
        connection.execute("DELETE FROM extracted_documents WHERE attachment_id=?", (attachment_id,))
    if result.status == "parsed" or not connection.execute("SELECT 1 FROM extracted_documents WHERE attachment_id=?", (attachment_id,)).fetchone():
        connection.execute("INSERT INTO extracted_documents(attachment_id,text_content,structure_json,parser,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (attachment_id, result.text, json.dumps(result.structure, ensure_ascii=False), result.parser, result.status, now_iso(), now_iso()))
    status = "parsed" if result.status == "parsed" else result.status
    connection.execute("UPDATE attachments SET status='stored',parse_status=?,error_message=? WHERE id=?", (status, result.error, attachment_id))
    if result.text:
        sources.append({"source_type": "attachment_body", "source_file": name, "location": "文件正文", "text": result.text})
    return sources


def _notice_analysis_sources(connection, notice_id: int, title: str, body: str) -> list[dict[str, str]]:
    sources = [{"source_type": "title", "source_file": "公告标题", "location": "详情页标题", "text": title}, {"source_type": "body", "source_file": "公告正文.html", "location": "正文", "text": body}]
    for row in connection.execute("SELECT * FROM attachments WHERE notice_id=?", (notice_id,)).fetchall():
        document = connection.execute("SELECT text_content FROM extracted_documents WHERE attachment_id=? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        if document and document["text_content"]:
            sources.append({"source_type": "attachment_body", "source_file": row["name"], "location": "文件正文", "text": document["text_content"]})
        sources.append({"source_type": "attachment_name", "source_file": row["name"], "location": "附件名", "text": row["name"]})
    return sources


def _persist_keyword_group_hits(connection, notice_id: int, group, sources: list[dict[str, str]]) -> bool:
    group_hits = match_sources(dict(group), sources)
    has_negative = any(hit.is_negative for hit in group_hits)
    # 排除词命中后，该规则组整体不成立；只保留负向证据。
    persisted_hits = [hit for hit in group_hits if hit.is_negative] if has_negative else group_hits
    for hit in persisted_hits:
        connection.execute("INSERT INTO keyword_hits(notice_id,keyword_group_id,keyword,rule_type,source_type,source_file,location,snippet,context_before,context_after,is_negative,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (notice_id, group["id"], hit.keyword, hit.rule_type, hit.source_type, hit.source_file, hit.location, hit.snippet, hit.context_before, hit.context_after, int(hit.is_negative), now_iso()))
    return bool(persisted_hits and not has_negative)


def refresh_notice_analysis(connection, notice_id: int, title: str, body: str, keyword_group_id: int | None = None) -> int:
    if keyword_group_id is None:
        connection.execute("DELETE FROM keyword_hits WHERE notice_id=?", (notice_id,))
    else:
        connection.execute("DELETE FROM keyword_hits WHERE notice_id=? AND keyword_group_id=?", (notice_id, keyword_group_id))
    sources = _notice_analysis_sources(connection, notice_id, title, body)
    if keyword_group_id is None:
        groups = connection.execute("SELECT * FROM keyword_groups WHERE enabled=1 ORDER BY priority DESC").fetchall()
    else:
        groups = connection.execute("SELECT * FROM keyword_groups WHERE id=? AND enabled=1", (keyword_group_id,)).fetchall()
    matched_groups = 0
    for group in groups:
        if _persist_keyword_group_hits(connection, notice_id, group, sources):
            matched_groups += 1
    return matched_groups


def rebuild_keyword_group_analysis(connection, group_id: int) -> dict[str, int]:
    """Recalculate one rule group so saved edits never leave stale evidence."""
    group = connection.execute("SELECT * FROM keyword_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        return {"notice_count": 0, "matched_notice_count": 0}
    ensure_notice_binding_schema(connection)
    rows = connection.execute(
        """SELECT n.id,n.title,
        COALESCE((SELECT v.body_text FROM notice_versions v WHERE v.notice_id=n.id ORDER BY v.version_no DESC LIMIT 1),'') AS body_text
        FROM notices n
        WHERE EXISTS (SELECT 1 FROM notice_keyword_bindings b WHERE b.notice_id=n.id AND b.keyword_group_id=?)
           OR EXISTS (SELECT 1 FROM keyword_hits h WHERE h.notice_id=n.id AND h.keyword_group_id=?)
        ORDER BY n.id""",
        (group_id, group_id),
    ).fetchall()
    matched_notice_count = 0
    for row in rows:
        connection.execute("DELETE FROM keyword_hits WHERE notice_id=? AND keyword_group_id=?", (row["id"], group_id))
        if group["enabled"] and _persist_keyword_group_hits(connection, row["id"], group, _notice_analysis_sources(connection, row["id"], row["title"], row["body_text"] or "")):
            matched_notice_count += 1
    return {"notice_count": len(rows), "matched_notice_count": matched_notice_count}


def discard_unmatched_notice(connection, notice_id: int) -> None:
    """Remove a newly fetched candidate that did not match the task rules."""
    connection.execute("DELETE FROM notices WHERE id=?", (notice_id,))
    for directory in (
        settings.raw_dir / "notices" / str(notice_id),
        settings.extracted_dir / "notices" / str(notice_id),
    ):
        try:
            if directory.exists():
                shutil.rmtree(directory)
        except OSError:
            # Database truth is more important than best-effort file cleanup.
            pass


def ingest_notice_data(connection, site_id: int | None, data: NoticeData, adapter: BaseAdapter | None = None, source_type: str = "crawl", download_attachments: bool = True, keyword_group_id: int | None = None, filter_unmatched: bool = False, crawl_job_id: int | None = None, crawl_run_id: int | None = None) -> int | None:
    timestamp = now_iso()
    fingerprint = hashlib.sha256((data.title + "\n" + data.body_text).encode("utf-8", errors="ignore")).hexdigest()
    existing = connection.execute("SELECT * FROM notices WHERE site_id IS ? AND (external_id=? OR source_url=?) ORDER BY id LIMIT 1", (site_id, data.external_id, data.url)).fetchone()
    if existing and existing["deleted_at"]:
        if source_type == "crawl":
            return None
        raise AdapterError("该公告位于回收站，请先在回收站中恢复")
    created_new = existing is None
    content_changed = bool(existing and existing["content_fingerprint"] != fingerprint)
    if existing:
        notice_id = int(existing["id"])
        version = int(existing["current_version"])
        if existing["content_fingerprint"] != fingerprint:
            version += 1
            raw_path = save_raw_html(notice_id, data.raw_html or data.body_text)
            connection.execute("INSERT INTO notice_versions(notice_id,version_no,raw_html_path,body_text,published_at,content_fingerprint,captured_at) VALUES(?,?,?,?,?,?,?)", (notice_id, version, raw_path, data.body_text, data.published_at, fingerprint, timestamp))
        else:
            raw_path = None
        effective_source_type = existing["source_type"] if source_type == "crawl" else source_type
        connection.execute("UPDATE notices SET title=?,notice_type=?,published_at=?,opening_at=?,source_type=?,content_fingerprint=?,current_version=?,deleted_at=NULL,ingest_status='detail_collected',updated_at=? WHERE id=?", (data.title, data.notice_type, data.published_at, data.opening_at, effective_source_type, fingerprint, version, timestamp, notice_id))
    else:
        cursor = connection.execute("INSERT INTO notices(site_id,external_id,source_type,source_url,title,notice_type,published_at,opening_at,ingest_status,content_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (site_id, data.external_id, source_type, data.url, data.title, data.notice_type, data.published_at, data.opening_at, "detail_collected", fingerprint, timestamp, timestamp))
        notice_id = int(cursor.lastrowid)
        raw_path = save_raw_html(notice_id, data.raw_html or data.body_text)
        connection.execute("INSERT INTO notice_versions(notice_id,version_no,raw_html_path,body_text,published_at,content_fingerprint,captured_at) VALUES(?,?,?,?,?,?,?)", (notice_id, 1, raw_path, data.body_text, data.published_at, fingerprint, timestamp))
        connection.execute("UPDATE notices SET current_version=1 WHERE id=?", (notice_id,))
    fields = derive_fields(data)
    for field_name, (value, location, confidence) in fields.items():
        if connection.execute("SELECT 1 FROM extracted_fields WHERE notice_id=? AND field_name=? AND manual_value IS NOT NULL", (notice_id, field_name)).fetchone():
            continue
        upsert_field(connection, notice_id, field_name, value, location, confidence)
        if value and field_name != "project_number":
            connection.execute(f"UPDATE notices SET {field_name if field_name != 'demand_unit' else 'demand_unit'}=? WHERE id=?", (value, notice_id))
        elif value and field_name == "project_number":
            connection.execute("UPDATE notices SET project_number=? WHERE id=?", (value, notice_id))
    project_name = fields["project_name"][0] or data.title
    summary = f"【项目】{project_name}；【单位】{fields['demand_unit'][0] or '待确认'}；【范围】{data.title}；【时间】{data.opening_at or '待确认'}；【附件】{len(data.attachments)} 个。"
    connection.execute("UPDATE notices SET summary=?,ingest_status='attachments_processing',updated_at=? WHERE id=?", (summary, timestamp, notice_id))
    # Release metadata writes before potentially slow network and parsing work.
    connection.commit()
    if adapter and download_attachments:
        for item in data.attachments:
            attachment_id = create_attachment(connection, notice_id, item.name, item.url, "downloading")
            connection.commit()
            try:
                saved = connection.execute("SELECT relative_path,sha256,status FROM attachments WHERE id=?", (attachment_id,)).fetchone()
                if saved["relative_path"] and saved["status"] in ("stored", "extracted"):
                    saved_path = absolute_from_relative(saved["relative_path"])
                    if saved_path.is_file() and sha256_file(saved_path) == saved["sha256"]:
                        if attachment_tree_needs_reparse(connection, attachment_id):
                            process_local_attachment(connection, notice_id, attachment_id, saved_path, item.name)
                        else:
                            continue
                        continue
                path, size, digest = save_streamed_attachment(adapter, notice_id, attachment_id, item.name, item.url)
                connection.execute("UPDATE attachments SET relative_path=?,mime_type=?,sha256=?,size_bytes=?,status='stored' WHERE id=?", (relative_to_data(path), file_mime(path), digest, size, attachment_id))
                connection.commit()
                process_local_attachment(connection, notice_id, attachment_id, path, item.name)
            except Exception as exc:
                connection.execute("UPDATE attachments SET status='failed',parse_status='failed',error_message=? WHERE id=?", (f"下载失败：{exc}", attachment_id))
                log_event(connection, "attachment.download", f"{item.name}：{exc}", "WARNING", notice_id=notice_id)
    elif adapter:
        for item in data.attachments:
            create_attachment(connection, notice_id, item.name, item.url, "not_downloaded")
    if crawl_job_id is not None and keyword_group_id is not None:
        ensure_notice_binding_schema(connection)
        connection.execute("INSERT INTO notice_keyword_bindings(notice_id,job_id,keyword_group_id,first_run_id,last_run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(notice_id,job_id,keyword_group_id) DO UPDATE SET last_run_id=excluded.last_run_id,updated_at=excluded.updated_at", (notice_id, crawl_job_id, keyword_group_id, crawl_run_id, crawl_run_id, timestamp, timestamp))
    matched_groups = refresh_notice_analysis(connection, notice_id, data.title, data.body_text, keyword_group_id)
    if filter_unmatched and matched_groups == 0:
        if created_new:
            discard_unmatched_notice(connection, notice_id)
        return None
    attachment_rows = connection.execute("SELECT status,parse_status FROM attachments WHERE notice_id=?", (notice_id,)).fetchall()
    has_attachment_issue = any(row["status"] in ("failed", "needs_tool") or row["parse_status"] in ("failed", "unsupported", "ocr_pending") for row in attachment_rows)
    connection.execute("UPDATE notices SET ingest_status=?,updated_at=? WHERE id=?", ("partial" if has_attachment_issue else "parsed", now_iso(), notice_id))
    if created_new:
        log_event(connection, "notice.ingest", f"公告新增入库：{data.title}", notice_id=notice_id)
    elif content_changed:
        log_event(connection, "notice.ingest", f"公告内容更新：{data.title}", notice_id=notice_id)
    return notice_id


def ingest_url(source_url: str, preferred_site: str | None = None) -> tuple[int, bool]:
    with get_db() as connection:
        site = find_site(connection, source_url, preferred_site)
        if not site:
            raise AdapterError("未找到可用平台配置")
        adapter = make_adapter(site["code"], site["base_url"])
        try:
            data = adapter.fetch_notice(source_url)
            existing = connection.execute("SELECT id FROM notices WHERE site_id IS ? AND (external_id=? OR source_url=?) ORDER BY id LIMIT 1", (site["id"], data.external_id, data.url)).fetchone()
            notice_id = ingest_notice_data(connection, site["id"], data, adapter, source_type="url_import")
            return notice_id, existing is None
        finally:
            adapter.close()


@tracked
def run_crawl(job_id: int, run_id: int, overrides: dict[str, Any] | None = None, preserve_schedule_anchor: bool = False) -> None:
    with get_db() as connection:
        ensure_run_tracking_schema(connection)
        job = connection.execute("SELECT j.*,s.code,s.base_url,a.session_status,a.credential_ref FROM crawl_jobs j JOIN sites s ON s.id=j.site_id LEFT JOIN site_accounts a ON a.id=j.account_id WHERE j.id=?", (job_id,)).fetchone()
        if not job:
            return
        job = dict(job)
        for key, value in (overrides or {}).items():
            if key in {"lookback_days", "max_pages", "max_notices", "download_attachments"}:
                job[key] = value
        cutoff = lookback_cutoff(job["lookback_days"])
        allowed_categories = {
            str(item).strip() for item in json_load(job["categories_json"], []) if str(item).strip()
        }
        retry_config = json_load(job["retry_json"], {})
        max_attempts = max(1, int(retry_config.get("max_attempts", 1) or 1))
        interval_seconds = max(0, int(job["interval_ms"] or 0)) / 1000
        started_at = now_iso()
        snapshot = {key: job.get(key) for key in ("name", "site_id", "keyword_group_id", "categories_json", "schedule_text", "timezone", "lookback_days", "max_pages", "max_notices", "interval_ms", "retry_json", "download_attachments")}
        connection.execute("UPDATE crawl_runs SET status='running',started_at=?,heartbeat_at=?,config_snapshot_json=? WHERE id=?", (started_at, started_at, json.dumps(snapshot, ensure_ascii=False), run_id))
        categories_label = "、".join(sorted(allowed_categories)) or "全部类型"
        log_event(
            connection,
            "crawl.start",
            f"开始运行任务：{job['name']}；回溯边界 {cutoff.strftime('%Y-%m-%d 00:00')}（北京时间）；类型 {categories_label}；最多尝试 {max_attempts} 次",
            crawl_run_id=run_id,
        )
    unsupported = []
    if int(job.get("concurrency") or 1) != 1:
        unsupported.append("并发采集")
    if bool(job.get("ocr_enabled")):
        unsupported.append("OCR")
    if unsupported:
        message = f"当前版本不支持{'、'.join(unsupported)}，任务已停止；请修正任务配置"
        with get_db() as connection:
            connection.execute("UPDATE crawl_runs SET status='failed',finished_at=?,failure_reason=? WHERE id=?", (now_iso(), message, run_id))
            log_event(connection, "crawl.unsupported_config", message, "ERROR", crawl_run_id=run_id)
        return
    if job["account_id"] and job["session_status"] not in ("verified", "public"):
        with get_db() as connection:
            message = "账号需要人工完成登录或会话续期，任务未发起下载"
            connection.execute("UPDATE crawl_runs SET status='failed',finished_at=?,failure_reason=? WHERE id=?", (now_iso(), message, run_id))
            log_event(connection, "crawl.wait_manual", message, "WARNING", crawl_run_id=run_id)
        return
    adapter: BaseAdapter | None = None
    discovered = details = created = duplicates = filtered = attachments = parsed = failed = 0
    reason = None
    def record_filter(title: str, rejection: str) -> None:
        nonlocal filtered
        filtered += 1
        with get_db() as connection:
            log_event(connection, "crawl.policy_filter", f"{title}：{rejection}", "INFO", crawl_run_id=run_id)

    try:
        adapter = make_adapter(job["code"], job["base_url"], session_cookie=job["credential_ref"] if job["session_status"] == "verified" else None)
        if hasattr(adapter, "set_request_interval"):
            adapter.set_request_interval(job["interval_ms"])
        with get_db() as connection:
            existing_ids = {
                str(row["external_id"])
                for row in connection.execute(
                    "SELECT n.external_id FROM notices n "
                    "WHERE n.site_id=? AND n.external_id IS NOT NULL",
                    (job["site_id"],),
                ).fetchall()
            }
        # Date/type policy is applied twice: list metadata avoids unnecessary
        # detail requests, while detail metadata is authoritative before write.
        candidate_limit = max(1, int(job["max_notices"]))
        summaries = None
        for attempt in range(1, max_attempts + 1):
            try:
                summaries = adapter.list_notices(job["max_pages"], candidate_limit, exclude_external_ids=existing_ids)
                break
            except Exception as exc:
                if attempt >= max_attempts:
                    raise
                with get_db() as connection:
                    log_event(connection, "crawl.retry", f"公告列表：第 {attempt} 次尝试失败，将重试：{exc}", "WARNING", crawl_run_id=run_id)
                time.sleep(min(max(interval_seconds, 0.2) * (2 ** (attempt - 1)), 30.0))
        if summaries is None:
            raise AdapterError("公告列表采集未返回结果")
        discovered = len(summaries)
        for summary in summaries:
            with get_db() as connection:
                connection.execute("UPDATE crawl_runs SET heartbeat_at=?,discovered=?,detail_success=?,created_count=?,duplicate_count=?,filtered_count=?,failed_count=? WHERE id=?", (now_iso(), discovered, details, created, duplicates, filtered, failed, run_id))
            if details >= job["max_notices"]:
                break
            if summary.published_at:
                rejection = notice_policy_rejection(summary.published_at, summary.notice_type, cutoff, allowed_categories)
                if rejection:
                    record_filter(summary.title, rejection)
                    continue
            elif allowed_categories:
                probe_rejection = notice_policy_rejection(now_iso(), summary.notice_type, cutoff, allowed_categories)
                if probe_rejection and "公告类型" in probe_rejection:
                    record_filter(summary.title, probe_rejection)
                    continue
            try:
                data: NoticeData | None = None
                for attempt in range(1, max_attempts + 1):
                    try:
                        data = adapter.fetch_notice(summary.url, summary.external_id, summary.detail_id)
                        break
                    except Exception as exc:
                        if attempt >= max_attempts:
                            raise
                        with get_db() as connection:
                            log_event(
                                connection,
                                "crawl.retry",
                                f"{summary.title}：第 {attempt} 次尝试失败，将重试：{exc}",
                                "WARNING",
                                crawl_run_id=run_id,
                            )
                        backoff = min(max(interval_seconds, 0.2) * (2 ** (attempt - 1)), 30.0)
                        time.sleep(backoff)
                if data is None:
                    raise AdapterError("详情采集未返回结果")
                effective_published_at = data.published_at or summary.published_at
                rejection = notice_policy_rejection(effective_published_at, data.notice_type, cutoff, allowed_categories)
                if rejection:
                    record_filter(summary.title, rejection)
                    continue
                if not data.published_at:
                    data.published_at = effective_published_at
                with get_db() as connection:
                    existing = connection.execute("SELECT id FROM notices WHERE site_id IS ? AND (external_id=? OR source_url=?) ORDER BY id LIMIT 1", (job["site_id"], data.external_id, data.url)).fetchone()
                    notice_id = ingest_notice_data(connection, job["site_id"], data, adapter, download_attachments=bool(job["download_attachments"]), keyword_group_id=job["keyword_group_id"], filter_unmatched=True, crawl_job_id=job_id, crawl_run_id=run_id)
                if notice_id is None:
                    filtered += 1
                    continue
                details += 1
                if existing is None:
                    created += 1
                else:
                    duplicates += 1
                attachments += len(data.attachments)
                parsed += 1
            except Exception as exc:
                failed += 1
                reason = str(exc)
                with get_db() as connection:
                    log_event(connection, "notice.failed", f"{summary.title}：{exc}", "WARNING", crawl_run_id=run_id)
    except Exception as exc:
        reason = str(exc)
        failed += 1
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                reason = reason or str(exc)
                failed += 1
        with get_db() as connection:
            status = "failed" if reason and not details else "partial" if reason else "completed"
            stop_reason = "failed" if status == "failed" else "target_reached" if details >= job["max_notices"] else "completed_with_errors" if reason else "candidate_exhausted"
            finished_at = now_iso()
            connection.execute("UPDATE crawl_runs SET status=?,finished_at=?,heartbeat_at=?,stop_reason=?,discovered=?,detail_success=?,created_count=?,duplicate_count=?,filtered_count=?,attachment_count=?,parsed_count=?,failed_count=?,failure_reason=? WHERE id=?", (status, finished_at, finished_at, stop_reason, discovered, details, created, duplicates, filtered, attachments, parsed, failed, reason, run_id))
            if preserve_schedule_anchor:
                connection.execute("UPDATE crawl_jobs SET last_run_at=? WHERE id=?", (now_iso(), job_id))
            else:
                connection.execute("UPDATE crawl_jobs SET last_run_at=?,schedule_anchor_at=NULL WHERE id=?", (now_iso(), job_id))
            log_event(connection, "crawl.finish", f"采集批次完成：发现 {discovered} 条，命中 {details} 条，新增入库 {created} 条，重复命中 {duplicates} 条，策略/关键词过滤 {filtered} 条，失败 {failed} 条", "WARNING" if reason else "INFO", crawl_run_id=run_id)


def create_run(job_id: int) -> int:
    run_id = try_create_run(job_id)
    if run_id is None:
        raise RuntimeError("该任务已有采集批次正在运行")
    return run_id


def try_create_run(job_id: int, reset_schedule: bool = False) -> int | None:
    with get_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM crawl_runs WHERE job_id=? AND status IN ('queued','running')", (job_id,)).fetchone():
            return None
        timestamp = now_iso()
        cursor = connection.execute("INSERT INTO crawl_runs(job_id,status,created_at) VALUES(?,?,?)", (job_id, "queued", timestamp))
        run_id = int(cursor.lastrowid)
        if reset_schedule:
            # The manual action is the new scheduling baseline, even if the
            # worker starts a little later in the background.
            connection.execute("UPDATE crawl_jobs SET schedule_anchor_at=? WHERE id=?", (timestamp, job_id))
        else:
            # A due scheduled run has consumed any previous manual/enable
            # anchor. Its completion timestamp becomes the next baseline.
            connection.execute("UPDATE crawl_jobs SET schedule_anchor_at=NULL WHERE id=?", (job_id,))
    return run_id


@tracked
def reparse_notice(notice_id: int) -> None:
    with get_db() as connection:
        notice = connection.execute("SELECT title FROM notices WHERE id=?", (notice_id,)).fetchone()
        version = connection.execute("SELECT body_text FROM notice_versions WHERE notice_id=? ORDER BY version_no DESC LIMIT 1", (notice_id,)).fetchone()
        if not notice or not version:
            raise ValueError("公告原始正文不存在")
        for attachment in connection.execute("SELECT id,name FROM attachments WHERE notice_id=?", (notice_id,)).fetchall():
            if is_office_lock_file(attachment["name"]):
                connection.execute("DELETE FROM attachments WHERE id=?", (attachment["id"],))
        # Older reparses could register the same extracted path more than once.
        # Keep one canonical row so a stale duplicate cannot continue exposing
        # an obsolete parse error after the real file has parsed successfully.
        duplicate_paths = connection.execute(
            """SELECT relative_path,MIN(id) AS keep_id,MAX(is_key_file) AS key_flag
            FROM attachments WHERE notice_id=? AND relative_path IS NOT NULL
            GROUP BY relative_path HAVING COUNT(*)>1""",
            (notice_id,),
        ).fetchall()
        for duplicate in duplicate_paths:
            connection.execute("UPDATE attachments SET is_key_file=? WHERE id=?", (duplicate["key_flag"], duplicate["keep_id"]))
            connection.execute(
                "DELETE FROM attachments WHERE notice_id=? AND relative_path=? AND id<>?",
                (notice_id, duplicate["relative_path"], duplicate["keep_id"]),
            )
        connection.commit()
        for attachment in connection.execute("SELECT id,relative_path,name FROM attachments WHERE notice_id=? AND parent_attachment_id IS NULL", (notice_id,)).fetchall():
            if attachment["relative_path"]:
                path = absolute_from_relative(attachment["relative_path"])
                if path.exists():
                    process_local_attachment(connection, notice_id, attachment["id"], path, attachment["name"])
                else:
                    connection.execute("UPDATE attachments SET parse_status='failed',error_message='本地文件不存在，保留上次解析结果' WHERE id=?", (attachment["id"],))
        ensure_notice_binding_schema(connection)
        bindings = connection.execute("SELECT DISTINCT keyword_group_id FROM notice_keyword_bindings WHERE notice_id=?", (notice_id,)).fetchall()
        if not bindings:
            bindings = connection.execute("SELECT DISTINCT keyword_group_id FROM keyword_hits WHERE notice_id=? AND keyword_group_id IS NOT NULL", (notice_id,)).fetchall()
        if bindings:
            for binding in bindings:
                refresh_notice_analysis(connection, notice_id, notice["title"], version["body_text"] or "", binding["keyword_group_id"])
        else:
            refresh_notice_analysis(connection, notice_id, notice["title"], version["body_text"] or "")
        issues = connection.execute("SELECT 1 FROM attachments WHERE notice_id=? AND (parse_status IN ('failed','unsupported','ocr_pending','pending') OR status IN ('failed','needs_tool','not_downloaded')) LIMIT 1", (notice_id,)).fetchone()
        connection.execute("UPDATE notices SET ingest_status=?,updated_at=? WHERE id=?", ("partial" if issues else "parsed", now_iso(), notice_id))
        log_event(connection, "notice.reparse", f"完成重新解析：{notice['title']}", notice_id=notice_id)
