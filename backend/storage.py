from __future__ import annotations

import re
import uuid
from pathlib import Path

from .config import settings


def safe_name(value: str, fallback: str = "file") -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value or "").strip(" .")
    return value[:180] or fallback


def relative_to_data(path: Path) -> str:
    return path.resolve().relative_to(settings.data_dir.resolve()).as_posix()


def absolute_from_relative(relative_path: str) -> Path:
    candidate = (settings.data_dir / relative_path).resolve()
    try:
        candidate.relative_to(settings.data_dir.resolve())
    except ValueError as exc:
        raise ValueError("文件路径超出数据目录") from exc
    return candidate


def notice_directory(notice_id: int) -> Path:
    directory = settings.raw_dir / "notices" / str(notice_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def attachment_directory(notice_id: int) -> Path:
    directory = settings.raw_dir / "notices" / str(notice_id) / "attachments"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def extraction_directory(notice_id: int) -> Path:
    directory = settings.extracted_dir / "notices" / str(notice_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_raw_html(notice_id: int, content: str) -> str:
    path = notice_directory(notice_id) / f"notice-{uuid.uuid4().hex}.html"
    path.write_text(content, encoding="utf-8")
    return relative_to_data(path)


def save_attachment(notice_id: int, filename: str, content: bytes) -> tuple[Path, str]:
    path = attachment_directory(notice_id) / safe_name(filename)
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while path.exists():
        path = path.with_name(f"{stem}-{counter}{suffix}")
        counter += 1
    path.write_bytes(content)
    return path, relative_to_data(path)


def save_extracted_file(notice_id: int, relative_name: str, content: bytes) -> tuple[Path, str]:
    clean_parts = [safe_name(part) for part in relative_name.replace("\\", "/").split("/") if part not in ("", ".", "..")]
    path = extraction_directory(notice_id).joinpath(*clean_parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path, relative_to_data(path)
