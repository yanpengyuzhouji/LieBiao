from __future__ import annotations

import re
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
        response = httpx.get(
            manifest_url,
            headers={"Accept": "application/vnd.github+json, application/json", "User-Agent": "LieBiao-Update-Checker"},
            follow_redirects=True,
            timeout=5.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UpdateCheckError(f"无法访问更新源：{exc}") from exc
    if len(response.content) > 1024 * 1024:
        raise UpdateCheckError("更新清单超过 1 MB 限制")
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpdateCheckError("更新源没有返回有效 JSON") from exc
    latest = str(payload.get("tag_name") or payload.get("version") or "").lstrip("vV")
    release_url = str(payload.get("html_url") or payload.get("url") or "").strip()
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
