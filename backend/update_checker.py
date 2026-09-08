from __future__ import annotations

import re
import json
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
    release_url = str(payload.get("html_url") or payload.get("release_url") or payload.get("download_url") or "").strip()
    if urlparse(release_url).scheme != "https":
        raise UpdateCheckError("更新下载页必须使用 HTTPS")
    available = version_key(latest) > version_key(current_version)
    return {
        "available": available,
        "current_version": current_version,
        "latest_version": latest,
        "release_url": release_url,
        "notes": str(payload.get("body") or payload.get("notes") or "")[:4000],
        "published_at": payload.get("published_at"),
    }
