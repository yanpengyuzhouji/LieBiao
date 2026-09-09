"""Verify the no-cookie completion path against public collection endpoints."""
import tempfile
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import settings
from backend.db import get_db, init_db
from backend.main import complete_site_verification
from backend.manual_verification import ManualVerificationError

old = settings.data_dir
with tempfile.TemporaryDirectory(prefix="liebiao-public-verification-") as folder:
    settings.data_dir = Path(folder)
    try:
        init_db()
        with get_db() as db:
            sites = list(db.execute("SELECT id,code FROM sites WHERE code IN (?,?) ORDER BY code", ("csg", "epec")))
        with patch("backend.main.complete_verification", side_effect=ManualVerificationError("未读取到平台会话")), patch("backend.main.close_verification"):
            for site in sites:
                result = complete_site_verification(site["id"])
                assert result == {"ok": True, "mode": "public", "message": "公开采集正常，无需人工验证"}, result
                print(site["code"], result)
    finally:
        settings.data_dir = old
