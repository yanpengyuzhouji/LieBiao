"""Coordinate local filesystem migration with active requests and workers."""
import threading
from contextlib import contextmanager
from functools import wraps

_lock = threading.RLock()
_active = 0
_owner = None


@contextmanager
def activity():
    global _active
    with _lock:
        if _owner is not None and _owner != threading.get_ident():
            raise RuntimeError('数据目录迁移中，请稍后重试')
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active -= 1


@contextmanager
def exclusive():
    global _owner
    with _lock:
        if _active or _owner is not None:
            raise RuntimeError('系统正在处理请求或任务，请等待完成后迁移目录')
        _owner = threading.get_ident()
    try:
        yield
    finally:
        with _lock:
            _owner = None


def tracked(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        with activity():
            return function(*args, **kwargs)
    return wrapper
