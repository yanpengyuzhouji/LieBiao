from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


def default_config_dir() -> Path:
    configured = os.getenv('LIEBIAO_CONFIG_DIR')
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform.startswith("win"):
        base = os.getenv("PROGRAMDATA") or os.getenv("LOCALAPPDATA")
        if base:
            return Path(base) / "LieBiao"
    return Path(__file__).resolve().parent.parent / ".runtime"


def normalize_data_dir(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value).strip()
    if not raw:
        raise ValueError("文件目录不能为空")
    if sys.platform.startswith("win"):
        path = Path(raw).expanduser()
    else:
        # Allow a Windows absolute path while developing/running under WSL.
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
        if match:
            drive = match.group(1).lower()
            mount = Path("/mnt") / drive
            if not mount.exists():
                raise ValueError(f"当前环境找不到 Windows 磁盘 {match.group(1)}:")
            path = mount / match.group(2).replace("\\", "/")
        else:
            path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("数据根目录必须填写绝对路径，例如 D:\\LieBiaoData")
    return path.resolve()


def default_data_dir() -> Path:
    configured = os.getenv("LIEBIAO_DATA_DIR")
    if configured:
        return normalize_data_dir(configured)
    if sys.platform.startswith("win"):
        program_data = os.getenv("PROGRAMDATA") or os.getenv("LOCALAPPDATA")
        if program_data:
            return Path(program_data) / "LieBiao" / "data"
    return Path(__file__).resolve().parent.parent / "data"


@dataclass
class Settings:
    project_dir: Path = Path(__file__).resolve().parent.parent
    config_dir: Path = default_config_dir()
    data_dir: Path = default_data_dir()
    host: str = os.getenv("LIEBIAO_HOST", "127.0.0.1")
    port: int = int(os.getenv("LIEBIAO_PORT", "8090"))
    max_attachment_mb: int = int(os.getenv("LIEBIAO_MAX_ATTACHMENT_MB", "500"))
    max_archive_mb: int = int(os.getenv("LIEBIAO_MAX_ARCHIVE_MB", "500"))
    max_expanded_mb: int = int(os.getenv("LIEBIAO_MAX_EXPANDED_MB", "2048"))
    max_archive_files: int = int(os.getenv("LIEBIAO_MAX_ARCHIVE_FILES", "3000"))
    max_archive_depth: int = int(os.getenv("LIEBIAO_MAX_ARCHIVE_DEPTH", "3"))

    @property
    def config_path(self) -> Path:
        return self.config_dir / "storage.json"

    def load_persisted_data_dir(self) -> bool:
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            root = payload.get("root")
            if root:
                self.data_dir = normalize_data_dir(root)
                return True
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            pass
        return False

    def persist_data_dir(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"root": str(self.data_dir)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.config_path)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scout.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def extracted_dir(self) -> Path:
        return self.data_dir / "extracted"

    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "preview"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "temp"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        for directory in (self.data_dir, self.raw_dir, self.extracted_dir, self.preview_dir, self.temp_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
