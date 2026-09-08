import sqlite3
import tempfile
import unittest
from pathlib import Path
from backend.config import Settings
from desktop_launcher import configure_data


class DesktopConfigurationTests(unittest.TestCase):
    def test_chinese_path_is_saved_and_writable(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            settings=Settings(config_dir=base/'config')
            configure_data(settings,str(base/'公告数据 中文'))
            self.assertTrue(settings.config_path.exists())
            self.assertTrue(settings.raw_dir.exists())
            self.assertTrue(settings.load_persisted_data_dir())
            self.assertEqual(settings.data_dir,base/'公告数据 中文')

    def test_unrelated_database_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder); data=base/'data';data.mkdir()
            c=sqlite3.connect(data/'scout.db');c.execute('create table unrelated(id)');c.close()
            settings=Settings(config_dir=base/'config')
            with self.assertRaises(ValueError): configure_data(settings,str(data))
            self.assertFalse(settings.config_path.exists())

    def test_relative_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                configure_data(Settings(config_dir=Path(folder)), 'relative-folder')
