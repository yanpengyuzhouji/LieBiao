"""Opt-in real Edge launch through the API, with an isolated database/profile."""
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from backend.config import settings
from backend.db import get_db, init_db
from backend.main import app
from backend.manual_verification import close_verification


if __name__ == '__main__':
    root = Path(tempfile.mkdtemp(prefix='verification-window-smoke-'))
    with patch.object(settings, 'data_dir', root):
        init_db()
        with get_db() as db:
            site_id = db.execute("SELECT id FROM sites WHERE code='chdtp'").fetchone()[0]
        try:
            response = TestClient(app).post(f'/api/sites/{site_id}/manual-verification/open')
            assert response.status_code == 200, response.text
            assert response.json().get('opened') is True, response.text
            print('PASS: Huadian verification API opened a real Edge window (HTTP 200)')
        finally:
            close_verification(site_id)
