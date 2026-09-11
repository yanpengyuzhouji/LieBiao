from __future__ import annotations

import json
import gc
import importlib.util
import os
import subprocess
import threading
import sys
import tempfile
from pathlib import Path

from .parsers import DocumentResult


_LOCK = threading.Lock()
_ENGINE = None


def available() -> bool:
    return importlib.util.find_spec("paddle") is not None and importlib.util.find_spec("paddleocr") is not None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from paddleocr import PaddleOCR
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "models"
        model_args = {}
        if bundled.exists():
            model_args = {
                "text_detection_model_dir": str(bundled / "PP-OCRv6_small_det"),
                "text_recognition_model_dir": str(bundled / "PP-OCRv6_small_rec"),
            }
        _ENGINE = PaddleOCR(
            text_detection_model_name="PP-OCRv6_small_det",
            text_recognition_model_name="PP-OCRv6_small_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
            enable_mkldnn=False,
            cpu_threads=4,
            **model_args,
        )
    return _ENGINE


def _texts(result) -> list[str]:
    payload = getattr(result, "json", result)
    if callable(payload):
        payload = payload()
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict):
        payload = payload.get("res", payload)
        values = payload.get("rec_texts", []) if isinstance(payload, dict) else []
        return [str(value).strip() for value in values if str(value).strip()]
    return []


def _parse_in_process(path: Path, max_pages: int = 30, dpi: int = 180) -> DocumentResult:
    global _ENGINE
    if not available():
        return DocumentResult(parser="ppocr-v6-small", status="ocr_pending", error="未安装 CPU OCR 组件")
    import fitz

    pages: list[dict[str, object]] = []
    try:
        with _LOCK, fitz.open(path) as document:
            total_pages = len(document)
            page_count = min(total_pages, max_pages)
            for index in range(page_count):
                pixmap = document[index].get_pixmap(dpi=dpi, alpha=False)
                image_path = path.parent / f".{path.stem}.ocr-{index + 1}.png"
                try:
                    pixmap.save(image_path)
                    lines: list[str] = []
                    for result in _engine().predict(str(image_path)):
                        lines.extend(_texts(result))
                finally:
                    image_path.unlink(missing_ok=True)
                pages.append({"page": index + 1, "text": "\n".join(lines)})
        text = "\n\n".join(str(page["text"]) for page in pages if page["text"])
        if not text.strip():
            return DocumentResult(parser="ppocr-v6-small", status="failed", error="OCR 未识别到文字")
        truncated = total_pages > max_pages
        return DocumentResult(
            text=text,
            structure={"pages": pages, "ocr": {"model": "PP-OCRv6-small", "dpi": dpi, "max_pages": max_pages, "truncated": truncated}},
            parser="ppocr-v6-small", status="parsed",
        )
    except Exception as exc:
        return DocumentResult(parser="ppocr-v6-small", status="failed", error=f"OCR 解析失败：{exc}")
    finally:
        _ENGINE = None
        gc.collect()


def parse_scanned_pdf(path: Path, max_pages: int = 30, dpi: int = 180) -> DocumentResult:
    if not available():
        return DocumentResult(parser="ppocr-v6-small", status="ocr_pending", error="未安装 CPU OCR 组件")
    descriptor, output_name = tempfile.mkstemp(prefix="liebia-ocr-", suffix=".json")
    os.close(descriptor)
    output = Path(output_name)
    try:
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.extend(["-m", "backend.ocr"])
        command.extend(["--ocr-worker", str(path), str(output), str(max_pages), str(dpi)])
        completed = subprocess.run(command, capture_output=True, timeout=1800, check=False)
        if completed.returncode != 0 or not output.exists():
            detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
            return DocumentResult(parser="ppocr-v6-small", status="failed", error=f"OCR 子进程失败：{detail or completed.returncode}")
        payload = json.loads(output.read_text(encoding="utf-8"))
        return DocumentResult(**payload)
    except subprocess.TimeoutExpired:
        return DocumentResult(parser="ppocr-v6-small", status="failed", error="OCR 超时（30 分钟）")
    except Exception as exc:
        return DocumentResult(parser="ppocr-v6-small", status="failed", error=f"OCR 子进程失败：{exc}")
    finally:
        output.unlink(missing_ok=True)


def worker(input_name: str, output_name: str, max_pages: int, dpi: int) -> int:
    result = _parse_in_process(Path(input_name), max_pages, dpi)
    Path(output_name).write_text(json.dumps(result.__dict__, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--ocr-worker":
        raise SystemExit(worker(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])))
    raise SystemExit("仅供猎标系统 OCR 子进程调用")
