import shutil
import sqlite3
from pathlib import Path
from .config import settings
from .db import get_db
from .maintenance import exclusive
from .parsers import sha256_file


def migrate_storage(target: Path):
    """Use an empty destination; retain the source until independently backed up."""
    with exclusive():
        source = settings.data_dir.resolve()
        if target.exists() and any(target.iterdir()):
            raise ValueError('为保护已有数据，请选择空目录；迁移不会覆盖非空目录')
        with get_db() as c:
            if c.execute("SELECT 1 FROM crawl_runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise RuntimeError('仍有采集任务排队或运行，请完成后再迁移')
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='reparse_tasks'").fetchone() and c.execute("SELECT 1 FROM reparse_tasks WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise RuntimeError('仍有重新解析任务，请完成后再迁移')
            target.mkdir(parents=True, exist_ok=True)
            destination = sqlite3.connect(target / 'scout.db')
            try:
                c.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('目标数据库校验失败')
            finally:
                destination.close()
        for path in source.rglob('*'):
            if path.is_symlink():
                raise ValueError('数据目录包含符号链接，请先检查后再迁移')
            if not path.is_file() or path.name in {'scout.db','scout.db-wal','scout.db-shm'}:
                continue
            copied = target / path.relative_to(source)
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copied)
            if sha256_file(path) != sha256_file(copied):
                raise RuntimeError(f'文件校验失败：{path.name}')
        try:
            settings.data_dir = target
            settings.ensure_dirs()
            settings.persist_data_dir()
        except Exception:
            settings.data_dir = source
            settings.persist_data_dir()
            raise
        return str(source)


def adopt_storage(target: Path):
    """Switch to an existing LieBiao database without modifying its contents."""
    target = target.resolve()
    database = target / 'scout.db'
    if not database.is_file():
        raise ValueError('所选目录不是已有猎标数据目录：缺少 scout.db')
    check = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        if check.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('所选猎标数据库校验失败')
        if not check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notices'").fetchone():
            raise ValueError('所选目录中的数据库不是猎标数据库')
    finally:
        check.close()
    with exclusive():
        source = settings.data_dir.resolve()
        with get_db() as connection:
            if connection.execute("SELECT 1 FROM crawl_runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise RuntimeError('仍有采集任务排队或运行，请完成后再切换目录')
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='reparse_tasks'").fetchone() and connection.execute("SELECT 1 FROM reparse_tasks WHERE status IN ('queued','running') LIMIT 1").fetchone():
                raise RuntimeError('仍有重新解析任务，请完成后再切换目录')
        try:
            settings.data_dir = target
            settings.ensure_dirs()
            settings.persist_data_dir()
        except Exception:
            settings.data_dir = source
            settings.persist_data_dir()
            raise
        return str(source)
