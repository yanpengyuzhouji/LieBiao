from __future__ import annotations

from .db import get_db, now_iso


def migrate_stabilization_schema() -> None:
    """Apply additive V1 stability migrations; never delete business data."""
    with get_db() as connection:
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
        connection.execute("CREATE INDEX IF NOT EXISTS idx_notice_keyword_bindings_group ON notice_keyword_bindings(keyword_group_id)")

        run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(crawl_runs)")}
        additions = {
            "config_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            "heartbeat_at": "TEXT",
            "stop_reason": "TEXT",
        }
        for name, definition in additions.items():
            if name not in run_columns:
                connection.execute(f"ALTER TABLE crawl_runs ADD COLUMN {name} {definition}")

        marker = connection.execute(
            "SELECT 1 FROM app_settings WHERE key=?",
            ("migration.notice_keyword_bindings.v1",),
        ).fetchone()
        if not marker:
            timestamp = now_iso()
            connection.execute(
                """INSERT OR IGNORE INTO notice_keyword_bindings(
                notice_id,job_id,keyword_group_id,created_at,updated_at)
                SELECT DISTINCT h.notice_id,j.id,h.keyword_group_id,?,?
                FROM keyword_hits h
                JOIN notices n ON n.id=h.notice_id
                JOIN crawl_jobs j ON j.site_id=n.site_id AND j.keyword_group_id=h.keyword_group_id
                WHERE h.keyword_group_id IS NOT NULL""",
                (timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                ("migration.notice_keyword_bindings.v1", "1", timestamp),
            )
