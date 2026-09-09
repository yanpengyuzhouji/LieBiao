"""Opt-in live task smoke tests; never writes to the user's database.

py tests/live_crawl_matrix.py --session-db D:/.../data/scout.db
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def child(code, root, session_db):
    from backend.config import settings
    from backend.db import get_db, init_db, now_iso
    from backend.adapters import make_adapter
    from backend.service import run_crawl, try_create_run
    settings.data_dir = Path(root) / code
    settings.max_attachment_mb = 20
    settings.max_archive_mb = 50
    init_db()
    cookie = None
    if session_db:
        with sqlite3.connect(Path(session_db).resolve().as_uri() + '?mode=ro', uri=True) as source:
            row = source.execute("SELECT a.credential_ref FROM site_accounts a JOIN sites s ON s.id=a.site_id WHERE s.code=? AND a.enabled=1 AND a.session_status='verified' ORDER BY a.id DESC LIMIT 1", (code,)).fetchone()
            cookie = row[0] if row else None
    with get_db() as db:
        site = db.execute('SELECT * FROM sites WHERE code=?', (code,)).fetchone()
        db.execute("UPDATE keyword_groups SET include_any_json='[\"招标\"]',include_all_json='[]',exclude_json='[]',synonyms_json='{}'")
        group = db.execute('SELECT id FROM keyword_groups LIMIT 1').fetchone()[0]
        job_id = db.execute("INSERT INTO crawl_jobs(name,site_id,keyword_group_id,schedule_text,lookback_days,max_pages,max_notices,interval_ms,retry_json,created_at) VALUES(?,?,?,'手动',30,2,3,1500,'{\"max_attempts\":1}',?)", ('隔离采集测试', site['id'], group, now_iso())).lastrowid
    def factory(code, base_url, session_cookie=None):
        return make_adapter(code, base_url, session_cookie=cookie or session_cookie)
    started = time.monotonic()
    runs = []
    trashed = None
    with patch('backend.service.make_adapter', side_effect=factory):
        for round_number in range(2):
            run_id = try_create_run(job_id)
            run_crawl(job_id, run_id)
            with get_db() as db:
                runs.append(dict(db.execute('SELECT status,discovered,detail_success,created_count,duplicate_count,filtered_count,failed_count,failure_reason FROM crawl_runs WHERE id=?', (run_id,)).fetchone()))
                if round_number == 0:
                    row = db.execute('SELECT id,external_id FROM notices ORDER BY id LIMIT 1').fetchone()
                    if row:
                        trashed = tuple(row)
                        db.execute('UPDATE notices SET deleted_at=? WHERE id=?', (now_iso(), row['id']))
                    if runs[0]['failed_count'] and not runs[0]['detail_success']:
                        break
    with get_db() as db:
        notices = [dict(row) for row in db.execute('SELECT title,source_url,published_at,notice_type,ingest_status,deleted_at FROM notices')]
        attachments = [dict(row) for row in db.execute('SELECT name,status,parse_status,error_message FROM attachments')]
        logs = [dict(row) for row in db.execute("SELECT event_type,message FROM system_logs WHERE level IN ('WARNING','ERROR') OR event_type IN ('crawl.finish','crawl.policy_filter')")]
        evidence = db.execute('SELECT COUNT(*) FROM keyword_hits WHERE is_negative=0').fetchone()[0]
        duplicate_ids = db.execute('SELECT COUNT(*) FROM (SELECT external_id FROM notices GROUP BY external_id HAVING COUNT(*)>1)').fetchone()[0]
        trash_ok = not trashed or bool(db.execute('SELECT deleted_at FROM notices WHERE id=?', (trashed[0],)).fetchone()[0])
    return dict(code=code, used_saved_session=bool(cookie), elapsed=round(time.monotonic()-started, 1), runs=runs, notices=notices, attachments=attachments, logs=logs, evidence=evidence, no_duplicate_ids=duplicate_ids == 0, trash_preserved=trash_ok)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--site', action='append')
    parser.add_argument('--session-db')
    parser.add_argument('--child')
    parser.add_argument('--root')
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(args.child, args.root, args.session_db), ensure_ascii=False), flush=True)
        return
    root = Path(tempfile.mkdtemp(prefix='liebiao-live-matrix-'))
    print('RESULT_DIR ' + str(root), flush=True)
    codes = args.site or ['csg', 'ecp', 'sgcc', 'epec', 'chng', 'cdt', 'ceb', 'yfb', 'chnenergy', 'espic', 'cgn', 'chdtp']
    def run(code):
        command = [sys.executable, __file__, '--child', code, '--root', str(root)]
        if args.session_db:
            command += ['--session-db', args.session_db]
        try:
            process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=240)
            # PDF/Office libraries can print diagnostics before the JSON report.
            reports = [line for line in process.stdout.splitlines() if line.startswith('{"code":')]
            result = json.loads(reports[-1]) if process.returncode == 0 and reports else dict(code=code, error=process.stderr[-2000:] or '测试进程未输出结果')
        except subprocess.TimeoutExpired:
            result = dict(code=code, error='240秒测试超时，已停止本测试子进程')
        except Exception as exc:
            result = dict(code=code, error=str(exc))
        (root / (code + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in result.items() if k not in ('notices','attachments','logs')}, ensure_ascii=False), flush=True)
        return result
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, codes))
    (root / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
