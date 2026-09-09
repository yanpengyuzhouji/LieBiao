import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from backend.config import Settings
from backend import tray as tray_module
from desktop_launcher import configure_data
from backend.tray import WindowsTray


class DesktopConfigurationTests(unittest.TestCase):
    def test_tray_wrapper_is_available_without_extra_dependency(self):
        self.assertTrue(callable(WindowsTray))

    def test_tray_degrades_cleanly_when_native_area_is_unavailable(self):
        class Window:
            def winfo_id(self):
                return 1

        with mock.patch.object(tray_module.os, 'name', 'posix'):
            self.assertFalse(WindowsTray(Window(), Path('.'), lambda: None).start())

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
