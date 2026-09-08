import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from backend.main import app
from backend.parsers import safe_extract_zip


class AuditRegressionTests(unittest.TestCase):
    def test_private_files_are_not_static_assets(self):
        # No lifespan: do not start the scheduler or touch the real database.
        client = TestClient(app)
        for path in ('/backend/main.py', '/data/scout.db', '/.runtime/storage.json', '/requirements.txt'):
            self.assertEqual(client.get(path).status_code, 404, path)
        for path in ('/', '/app.js', '/styles.css'):
            self.assertEqual(client.get(path).status_code, 200, path)

    def test_equal_size_changed_member_is_updated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'sample.zip'
            for content in ('AAAA', 'BBBB', 'BBBB'):
                with zipfile.ZipFile(archive, 'w') as writer:
                    writer.writestr('list.txt', content)
                    writer.writestr('~$list.xlsx', 'lock')
                files = safe_extract_zip(archive, root / 'out', 10000, 10000, 10, 3)
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].read_text(), content)
            self.assertFalse(list((root / 'out').glob('.extract-*')))
