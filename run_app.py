from __future__ import annotations

import threading
import time
import webbrowser

import uvicorn

from backend.config import settings
from backend.db import init_db


def open_ui() -> None:
    time.sleep(1.2)
    webbrowser.open(f"http://127.0.0.1:{settings.port}")


if __name__ == "__main__":
    settings.load_persisted_data_dir()
    settings.ensure_dirs()
    init_db()
    threading.Thread(target=open_ui, daemon=True).start()
    uvicorn.run("backend.main:app", host=settings.host, port=settings.port, reload=False, log_level="info")
