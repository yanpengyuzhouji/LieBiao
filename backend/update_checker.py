from __future__ import annotations

import re
import json
import threading
from urllib.parse import urlparse

import httpx


class UpdateCheckError(RuntimeError):
    pass


def version_key(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(value).strip(), re.IGNORECASE)
    if not match:
        raise UpdateCheckError("更新源返回了无效版本号")
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def check_update(current_version: str, manifest_url: str) -> dict:
    if urlparse(manifest_url).scheme != "https":
        raise UpdateCheckError("更新地址必须使用 HTTPS")
    try:
        with httpx.stream(
            "GET",
            manifest_url,
            headers={"Accept": "application/vnd.github+json, application/json", "User-Agent": "LieBiao-Update-Checker"},
            follow_redirects=True,
            timeout=5.0,
        ) as response:
            response.raise_for_status()
            declared_size = int(response.headers.get("Content-Length", "0") or 0)
            if declared_size > 1024 * 1024:
                raise UpdateCheckError("更新清单超过 1 MB 限制")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise UpdateCheckError("更新清单超过 1 MB 限制")
    except httpx.HTTPError as exc:
        raise UpdateCheckError("无法访问更新源") from exc
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateCheckError("更新源没有返回有效 JSON") from exc
    latest = str(payload.get("tag_name") or payload.get("version") or "").lstrip("vV")
    release_page_url = str(payload.get("html_url") or payload.get("release_url") or "").strip()
    assets = payload.get("assets") if isinstance(payload.get("assets"), list) else []
    asset_urls = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_name = str(asset.get("name") or "").lower()
        asset_url = str(asset.get("browser_download_url") or "").strip()
        if asset_url and (asset_name.endswith(".exe") or "installer" in asset_name):
            asset_urls.append(asset_url)
    release_url = asset_urls[0] if asset_urls else str(payload.get("download_url") or release_page_url).strip()
    if urlparse(release_url).scheme != "https":
        raise UpdateCheckError("更新下载页必须使用 HTTPS")
    available = version_key(latest) > version_key(current_version)
    return {
        "available": available,
        "current_version": current_version,
        "latest_version": latest,
        "release_url": release_url,
        "release_page_url": release_page_url,
        "notes": str(payload.get("body") or payload.get("notes") or "")[:4000],
        "published_at": payload.get("published_at"),
    }


class UpdateMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status: dict = {"enabled": False, "available": False}

    def start(self, current_version: str, manifest_url: str, enabled: bool, interval_seconds: float = 3600) -> None:
        self.stop()
        self._stop.clear()
        self._status = {"enabled": enabled, "available": False, "current_version": current_version}
        if not enabled:
            return

        def run() -> None:
            while not self._stop.is_set():
                try:
                    result = {"enabled": True, **check_update(current_version, manifest_url)}
                except UpdateCheckError:
                    result = {"enabled": True, "available": False, "current_version": current_version, "error": "更新检查暂时不可用"}
                with self._lock:
                    self._status = result
                self._stop.wait(interval_seconds)

        self._thread = threading.Thread(target=run, name="update-checker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        self._thread = None

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)


update_monitor = UpdateMonitor()
