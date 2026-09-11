"""Persistent status for single-process desktop reparse/OCR jobs."""
from concurrent.futures import ThreadPoolExecutor
from .db import get_db, now_iso, log_event


def initialize(recover=False):
    with get_db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS reparse_tasks (
            id INTEGER PRIMARY KEY, notice_id INTEGER NOT NULL REFERENCES notices(id) ON DELETE CASCADE,
            status TEXT NOT NULL, error TEXT, use_ocr INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, finished_at TEXT)""")
        columns = {row['name'] for row in c.execute('PRAGMA table_info(reparse_tasks)')}
        if 'use_ocr' not in columns:
            c.execute('ALTER TABLE reparse_tasks ADD COLUMN use_ocr INTEGER NOT NULL DEFAULT 0')
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS reparse_active_notice ON reparse_tasks(notice_id) WHERE status IN ('queued','running')")
        if recover:
            c.execute("UPDATE reparse_tasks SET status='failed',error='服务重启，解析中断，请重试',finished_at=? WHERE status IN ('queued','running')", (now_iso(),))


def queue(notice_id, use_ocr=False):
    initialize()
    with get_db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM notices WHERE id=? AND deleted_at IS NULL', (notice_id,)).fetchone():
            raise ValueError('公告不存在或已删除')
        active = c.execute("SELECT id FROM reparse_tasks WHERE notice_id=? AND status IN ('queued','running')", (notice_id,)).fetchone()
        if active:
            return active['id'], False
        task_id = c.execute("INSERT INTO reparse_tasks(notice_id,status,use_ocr,created_at) VALUES(?,'queued',?,?)", (notice_id,int(use_ocr),now_iso())).lastrowid
        return task_id, True


def submit(task_id):
    _EXECUTOR.submit(execute, task_id)


def execute(task_id):
    from .service import reparse_notice
    with get_db() as c:
        task = c.execute('SELECT * FROM reparse_tasks WHERE id=?', (task_id,)).fetchone()
        if not task or task['status'] != 'queued':
            return
        c.execute("UPDATE reparse_tasks SET status='running' WHERE id=?", (task_id,))
    status, error = 'failed', None
    try:
        reparse_notice(task['notice_id'], enable_ocr=bool(task['use_ocr']))
        with get_db() as c:
            notice = c.execute('SELECT ingest_status FROM notices WHERE id=?', (task['notice_id'],)).fetchone()
            status = 'completed' if notice and notice['ingest_status']=='parsed' else 'partial'
    except Exception as exc:
        error = str(exc)
    finally:
        with get_db() as c:
            c.execute('UPDATE reparse_tasks SET status=?,error=?,finished_at=? WHERE id=?', (status,error,now_iso(),task_id))
            log_event(c, 'notice.reparse.result', f'重新解析任务 #{task_id}：{status}' + (f'，{error}' if error else ''), 'INFO' if status=='completed' else 'WARNING', notice_id=task['notice_id'])


def get(task_id):
    initialize()
    with get_db() as c:
        task = c.execute('SELECT * FROM reparse_tasks WHERE id=?', (task_id,)).fetchone()
        return dict(task) if task else None
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="liebia-parse")
