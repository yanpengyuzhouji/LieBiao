from __future__ import annotations

import base64
import json
import os
import socket
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import getproxies

import httpx


class AdapterError(RuntimeError):
    pass


@dataclass
class AttachmentInfo:
    name: str
    url: str


@dataclass
class NoticeSummary:
    external_id: str
    title: str
    url: str
    published_at: str | None = None
    notice_type: str = "招标公告"
    # The public route uses firstPageDocId, while detail APIs use noticeId.
    detail_id: str | None = None


@dataclass
class NoticeData:
    external_id: str
    title: str
    url: str
    body_text: str
    raw_html: str = ""
    published_at: str | None = None
    opening_at: str | None = None
    notice_type: str = "招标公告"
    attachments: list[AttachmentInfo] = field(default_factory=list)
    collection_warning: str | None = None


class PageParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.content_parts: list[str] = []
        self._content_depth = 0
        self._href: str | None = None
        self._link_parts: list[str] = []
        self._in_title = False
        self._in_heading = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = dict(attrs)
        classes = {item.lower() for item in (attr_map.get("class") or "").split()}
        if self._content_depth:
            if tag not in self.VOID_TAGS:
                self._content_depth += 1
        elif tag == "div" and "content" in classes:
            self._content_depth = 1
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if tag == "a":
            self._href = attr_map.get("href")
            self._link_parts = []
        if tag == "title":
            self._in_title = True
        if tag == "h1":
            self._in_heading = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join(self._link_parts).strip()))
            self._href = None
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_heading = False
        if self._content_depth:
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        self.text_parts.append(value)
        if self._content_depth:
            self.content_parts.append(value)
        if self._href is not None:
            self._link_parts.append(value)
        if self._in_title:
            self.title_parts.append(value)
        if self._in_heading:
            self.heading_parts.append(value)


def clean_text(parts: list[str]) -> str:
    return "\n".join(dict.fromkeys(part for part in parts if part))


def first_date(text: str, labels: tuple[str, ...]) -> str | None:
    date_pattern = re.compile(
        r"(?P<year>20\d{2})(?:\s*年\s*(?P<month_cn>\d{1,2})\s*月\s*(?P<day_cn>\d{1,2})\s*日|[-\/.](?P<month_num>\d{1,2})[-\/.](?P<day_num>\d{1,2}))"
        r"(?:\s*(?:(?P<hour_cn>\d{1,2})\s*时\s*(?P<minute_cn>\d{1,2})\s*分(?:\s*(?P<second_cn>\d{1,2})\s*秒)?|(?P<hour_num>\d{1,2})[:：](?P<minute_num>\d{1,2})(?:[:：](?P<second_num>\d{1,2}))?))?"
    )
    # Labels are ordered by semantic preference. For example, an explicit
    # opening time is preferred over an earlier bid-submission deadline.
    for label in labels:
        for label_match in re.finditer(re.escape(label), text, re.I):
            match = date_pattern.search(text[label_match.end():label_match.end() + 300])
            if not match:
                continue
            month = int(match.group("month_cn") or match.group("month_num"))
            day = int(match.group("day_cn") or match.group("day_num"))
            hour_text = match.group("hour_cn") or match.group("hour_num")
            minute_text = match.group("minute_cn") or match.group("minute_num")
            second_text = match.group("second_cn") or match.group("second_num")
            try:
                value = f"{int(match.group('year')):04d}-{month:02d}-{day:02d}"
                if hour_text is not None and minute_text is not None:
                    value += f" {int(hour_text):02d}:{int(minute_text):02d}:{int(second_text or 0):02d}"
                # Validate impossible calendar values before accepting.
                from datetime import datetime
                datetime.fromisoformat(value)
                return value
            except (TypeError, ValueError):
                continue
    return None


def extract_external_id(url: str) -> str:
    fragment = urlparse(url).fragment
    route_match = re.search(r"/(?:doc/)?doci-bid/([^/?]+)", fragment)
    if not route_match:
        route_match = re.search(r"/doc/([^/?]+)", fragment)
    if route_match:
        return route_match.group(1).split("_")[0]
    path = urlparse(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    return re.sub(r"\.jhtml?$", "", tail, flags=re.I) or url


def detect_outbound_proxy() -> str | None:
    explicit = os.getenv("LIEBIAO_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if explicit:
        return explicit
    proxies = getproxies()
    system_proxy = proxies.get("https") or proxies.get("http")
    if system_proxy:
        return system_proxy
    # WSL does not read the Windows WinINET proxy registry.  Localhost
    # forwarding is available in the desktop deployment, so probe common
    # local HTTP proxy ports without making an external request.
    for port in (7890, 7897):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return None


class BaseAdapter:
    code = "base"

    def __init__(self, base_url: str, session_cookie: str | None = None, timeout: float = 25.0) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._request_interval_seconds = 0.0
        self._last_request_started: float | None = None
        self._request_lock = threading.Lock()
        self.browser_site_id: int | None = None
        self.browser_port: int | None = None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        cookies = {}
        if session_cookie:
            for item in session_cookie.split(";"):
                if "=" in item:
                    key, value = item.strip().split("=", 1)
                    if key == "__scout_user_agent":
                        try:
                            headers["User-Agent"] = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
                        except (ValueError, UnicodeDecodeError):
                            pass
                    elif key == "__scout_browser_session":
                        try:
                            self.browser_site_id = int(value)
                        except ValueError:
                            pass
                    elif key == "__scout_browser_port":
                        try:
                            port = int(value)
                            if 1 <= port <= 65535:
                                self.browser_port = port
                        except ValueError:
                            pass
                    else:
                        cookies[key] = value
        self.proxy_url = detect_outbound_proxy()
        self.client = httpx.Client(
            timeout=timeout,
            proxy=self.proxy_url,
            follow_redirects=True,
            headers=headers,
            cookies=cookies,
            event_hooks={"request": [self._throttle_request]},
        )

    def set_request_interval(self, interval_ms: int) -> None:
        self._request_interval_seconds = max(0, int(interval_ms)) / 1000

    def _throttle_request(self, _request: httpx.Request) -> None:
        with self._request_lock:
            if self._last_request_started is not None and self._request_interval_seconds:
                remaining = self._request_interval_seconds - (time.monotonic() - self._last_request_started)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request_started = time.monotonic()

    def close(self) -> None:
        self.client.close()

    def authenticate(self) -> dict[str, Any]:
        return {"status": "public", "message": "使用公开页面，不需要登录"}

    def health_check(self) -> dict[str, Any]:
        try:
            response = self.client.get(self.base_url)
            return {"ok": response.status_code < 400, "status_code": response.status_code, "message": f"HTTP {response.status_code}"}
        except httpx.HTTPError as exc:
            return {"ok": False, "status_code": None, "message": f"连接失败：{exc}"}

    def build_source_url(self, external_id: str, url: str | None = None) -> str:
        return url or urljoin(self.base_url, external_id)

    def list_notices(self, max_pages: int = 1, max_notices: int = 100, list_url: str | None = None, exclude_external_ids: set[str] | None = None) -> list[NoticeSummary]:
        raise AdapterError("该平台的列表接口尚未完成步骤 1 勘探，请使用 URL 导入或配置公开列表入口")

    def fetch_notice(self, url: str, external_id: str | None = None, detail_id: str | None = None) -> NoticeData:
        try:
            response = self.client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AdapterError(f"详情页访问失败：{exc}") from exc
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and not response.text.lstrip().startswith("<"):
            raise AdapterError("详情链接不是可解析的公开 HTML 页面")
        parser = PageParser()
        parser.feed(response.text)
        full_text = clean_text(parser.text_parts)
        body = clean_text(parser.content_parts or parser.text_parts)
        title = clean_text(parser.heading_parts).strip() or clean_text(parser.title_parts).strip(" -_|") or extract_external_id(url)
        attachments = self._attachments_from_links(parser.links, url)
        return NoticeData(
            external_id=external_id or extract_external_id(url), title=title, url=url, body_text=body,
            raw_html=response.text,
            published_at=first_date(full_text, ("发布时间", "发布日期", "公告时间", "发布于")),
            opening_at=first_date(body, ("开标时间", "投标截止时间", "截标时间", "递交截止时间")) or first_date(full_text, ("开标时间", "投标截止时间", "截标时间", "递交截止时间")),
            attachments=attachments,
        )

    def _attachments_from_links(self, links: list[tuple[str, str]], page_url: str) -> list[AttachmentInfo]:
        allowed = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip", ".rar", ".7z", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
        result: list[AttachmentInfo] = []
        seen: set[str] = set()
        for href, label in links:
            if not href or href.startswith(("javascript:", "#", "mailto:")):
                continue
            absolute = urljoin(page_url, href)
            parsed = urlparse(absolute)
            suffix = parsed.path.lower().rsplit(".", 1)[-1] if "." in parsed.path else ""
            label_lower = label.strip().lower()
            is_attachment = (
                any(parsed.path.lower().endswith(ext) for ext in allowed)
                or any(label_lower.endswith(ext) for ext in allowed)
                or any(word in f"{label_lower}{href.lower()}" for word in ("附件下载", "点击下载", "download"))
            )
            if is_attachment and absolute not in seen:
                seen.add(absolute)
                name = label.strip() or parsed.path.rsplit("/", 1)[-1] or "未命名附件"
                result.append(AttachmentInfo(name=name, url=absolute))
        return result


class CsgAdapter(BaseAdapter):
    code = "csg"

    def list_notices(self, max_pages: int = 1, max_notices: int = 100, list_url: str | None = None, exclude_external_ids: set[str] | None = None) -> list[NoticeSummary]:
        entry = list_url or urljoin(self.base_url, "zbgg/index.jhtml")
        result: list[NoticeSummary] = []
        seen: set[str] = set()
        excluded = exclude_external_ids or set()
        recognized_any = False
        for page in range(1, max_pages + 1):
            page_url = entry if page == 1 else urljoin(entry, f"index_{page}.jhtml")
            try:
                response = self.client.get(page_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise AdapterError(f"公开公告列表访问失败：{exc}") from exc
            parser = PageParser()
            parser.feed(response.text)
            page_found = 0
            for href, label in parser.links:
                absolute = urljoin(page_url, href)
                path = urlparse(absolute).path.lower()
                if not re.fullmatch(r"/zbgg/\d+\.jhtml?", path):
                    continue
                external_id = extract_external_id(absolute)
                if external_id in seen or not label.strip():
                    continue
                seen.add(external_id)
                page_found += 1
                recognized_any = True
                if external_id in excluded:
                    continue
                result.append(NoticeSummary(external_id=external_id, title=label.strip(), url=absolute))
                if len(result) >= max_notices:
                    return result
            # A page containing only historical IDs must not stop the scan;
            # the next page may contain enough new notices.
            if not page_found:
                break
        if not result and not recognized_any:
            raise AdapterError("列表页面未发现可识别的公告链接，可能需要重新勘探页面结构")
        return result


class SgccPortalAdapter(BaseAdapter):
    code = "sgcc"

    api_root = "https://sgccetp.com.cn/ecpwcmcore"
    portal_root = "https://sgccetp.com.cn/portal/"
    menu_ids = ("2018032700291334", "2018032900295987")
    supported_doctypes = {"doci-bid", "doci-change"}

    def list_notices(self, max_pages: int = 1, max_notices: int = 100, list_url: str | None = None, exclude_external_ids: set[str] | None = None) -> list[NoticeSummary]:
        result: list[NoticeSummary] = []
        seen: set[str] = set()
        excluded = exclude_external_ids or set()
        recognized_any = False
        for menu_id in self.menu_ids:
            for page in range(1, max_pages + 1):
                request = {
                    "index": page, "size": min(20, max_notices), "firstPageMenuId": menu_id,
                    "purOrgStatus": "", "purOrgCode": "", "purType": "", "noticeType": "",
                    "orgId": "", "key": "", "homePageType": "1",
                }
                try:
                    response = self.client.post(
                        f"{self.api_root}/index/noteList", json=request,
                        headers={"Content-Type": "application/json", "Referer": self.portal_root},
                    )
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError) as exc:
                    raise AdapterError(f"公告列表接口访问失败：{exc}") from exc
                rows = ((payload.get("resultValue") or {}).get("noteList") or []) if payload.get("successful") else []
                for row in rows:
                    doctype = str(row.get("doctype") or "").strip().lower()
                    if doctype not in self.supported_doctypes:
                        # ECP also returns doc-spec rows in the public list;
                        # those are specifications, not detail API notices.
                        continue
                    doc_id = str(row.get("firstPageDocId") or row.get("noticeId") or row.get("id") or "")
                    if not doc_id:
                        continue
                    recognized_any = True
                    if doc_id in seen:
                        continue
                    seen.add(doc_id)
                    if doc_id in excluded:
                        continue
                    detail_id = str(row.get("noticeId") or row.get("id") or doc_id)
                    source_url = f"{self.portal_root}#/doc/{doctype}/{detail_id}_{menu_id}"
                    result.append(NoticeSummary(
                        external_id=doc_id, title=str(row.get("title") or doc_id), url=source_url,
                        published_at=row.get("noticePublishTime"),
                        notice_type="招标公告" if menu_id == "2018032700291334" else "采购公告",
                        detail_id=detail_id,
                    ))
                    if len(result) >= max_notices:
                        return result
                if len(rows) < request["size"]:
                    break
        if not result and not recognized_any:
            raise AdapterError("公开公告列表未返回数据")
        return result

    def fetch_notice(self, url: str, external_id: str | None = None, detail_id: str | None = None) -> NoticeData:
        return self._fetch_public_notice(url, external_id, detail_id)

    def _fetch_public_notice(self, url: str, external_id: str | None = None, detail_id: str | None = None) -> NoticeData:
        # firstPageDocId identifies the public route; getNoticeBid expects noticeId.
        notice_id = detail_id or external_id or extract_external_id(url)
        endpoint = f"{self.api_root}/index/getNoticeBid"
        try:
            portal_root = self.api_root.split("/ecpwcmcore", 1)[0] + "/portal/"
            response = self.client.post(
                endpoint, content=json.dumps(str(notice_id)),
                headers={"Content-Type": "application/json", "Referer": portal_root},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AdapterError(f"公开公告接口访问失败：{exc}") from exc
        result = payload.get("resultValue") or {}
        notice = result.get("notice") or result.get("noticeOld")
        if not payload.get("successful") or not isinstance(notice, dict):
            raise AdapterError(payload.get("resultHint") or "公开公告接口未返回公告数据")
        title = str(notice.get("TITLE") or notice.get("PURPRJ_NAME") or notice_id)
        body = self._notice_body(notice)
        attachments: list[AttachmentInfo] = []
        if str(result.get("fileFlag") or notice.get("fileFlag") or "") == "1":
            detail_id = notice.get("PURPRJ_NOTICE_DET_ID") or ""
            download_url = f"{self.api_root}/index/downLoadBid?noticeId={notice_id}&noticeDetId={detail_id}"
            attachments.append(AttachmentInfo(name=f"{notice.get('NOTICE_TYPE_NAME') or '公告附件'}.zip", url=download_url))
        return NoticeData(
            external_id=str(external_id or notice_id), title=title, url=url, body_text=body,
            raw_html=json.dumps(payload, ensure_ascii=False, indent=2),
            published_at=notice.get("PUB_TIME"), opening_at=notice.get("OPENBID_TIME"),
            notice_type=notice.get("NOTICE_TYPE_NAME") or "招标公告", attachments=attachments,
        )

    @staticmethod
    def _notice_body(notice: dict[str, Any]) -> str:
        fields = (
            ("项目名称", "PURPRJ_NAME"), ("项目编号", "PURPRJ_CODE"), ("公告类型", "NOTICE_TYPE_NAME"),
            ("采购人", "BID_ORG"), ("发布单位", "PUBLISH_ORG_NAME"), ("招标代理机构", "BID_AGT"),
            ("代理机构地址", "BID_AGT_ADDR"), ("联系人", "CONTACT"), ("联系电话", "TEL"),
            ("邮箱", "E_MAIL"), ("发布时间", "PUB_TIME"), ("报名开始时间", "BIDBOOK_SELL_BEGIN_TIME"),
            ("报名截止时间", "BIDBOOK_BUY_END_TIME"), ("开标时间", "OPENBID_TIME"), ("开标地点", "OPENBID_ADDR"),
            ("项目简介", "PRJ_INTRODUCE"), ("公告内容", "CHG_NOTICE_CONT"),
        )
        return "\n".join(f"{label}：{notice.get(key)}" for label, key in fields if notice.get(key) not in (None, "", " "))


class EcpAdapter(SgccPortalAdapter):
    code = "ecp"
    api_root = "https://ecp.sgcc.com.cn/ecp2.0/ecpwcmcore"
    portal_root = "https://ecp.sgcc.com.cn/ecp2.0/portal/"


class EpecAdapter(BaseAdapter):
    code = "epec"
    api_url = "https://bidding.epec.com/gateway/obs/business/ubm/notice/queryNoticePageList"

    def list_notices(self, max_pages: int = 1, max_notices: int = 100, list_url: str | None = None, exclude_external_ids: set[str] | None = None) -> list[NoticeSummary]:
        del list_url
        result: list[NoticeSummary] = []
        seen: set[str] = set()
        excluded = exclude_external_ids or set()
        page_size = min(50, max_notices)
        previous_page_ids: tuple[str, ...] | None = None
        for page in range(max_pages):
            request = {
                "model": {"noticeTypeList": ["01", "11"]},
                "currentPage": page + 1, "pageSize": page_size,
                "start": page * page_size, "limit": page_size,
            }
            try:
                response = self.client.post(self.api_url, json=request, headers={"Content-Type": "application/json", "Referer": self.base_url})
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise AdapterError(f"中国石化公告列表访问失败：{exc}") from exc
            rows = ((payload.get("data") or {}).get("root") or []) if payload.get("status") else []
            page_ids = tuple(str(row.get("noticeId") or "") for row in rows)
            if page > 0 and page_ids and page_ids == previous_page_ids:
                break
            previous_page_ids = page_ids
            for row in rows:
                notice_id = str(row.get("noticeId") or "")
                title = str(row.get("noticeTitle") or "").strip()
                if not notice_id or not title or notice_id in excluded or notice_id in seen:
                    continue
                seen.add(notice_id)
                query = urlencode({
                    "noticeId": notice_id, "type": row.get("noticeType") or "01",
                    "businessId": row.get("businessId") or "", "attachUrl": row.get("attachUrl") or "",
                })
                result.append(NoticeSummary(
                    external_id=notice_id, title=title,
                    url=f"https://bidding.epec.com/noticeDetail?{query}",
                    published_at=row.get("releaseTime"), notice_type=row.get("noticeTypeName") or "招标公告",
                    detail_id=row.get("attachUrl") or None,
                ))
                if len(result) >= max_notices:
                    return result
            if len(rows) < page_size:
                break
        if not result:
            raise AdapterError("中国石化公开公告列表未返回可识别数据")
        return result

    def fetch_notice(self, url: str, external_id: str | None = None, detail_id: str | None = None) -> NoticeData:
        query = parse_qs(urlparse(url).query)
        notice_id = external_id or (query.get("noticeId") or [""])[0] or extract_external_id(url)
        attach_path = detail_id or (query.get("attachUrl") or [""])[0]
        if not attach_path:
            raise AdapterError("中国石化公告缺少正文地址")
        text_url = urljoin("https://bidding.epec.com/noticefile/", attach_path.lstrip("/"))
        try:
            response = self.client.get(text_url, headers={"Referer": url})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AdapterError(f"中国石化公告正文访问失败：{exc}") from exc
        raw_html = response.content.decode("utf-8", errors="replace")
        if raw_html.count("�") > 5:
            raw_html = response.content.decode("gb18030", errors="replace")
        parser = PageParser()
        parser.feed(raw_html)
        body = clean_text(parser.text_parts)
        title = clean_text(parser.heading_parts).strip() or body.split("\n", 1)[0] or str(notice_id)
        return NoticeData(
            external_id=str(notice_id), title=title, url=url, body_text=body, raw_html=raw_html,
            published_at=first_date(body, ("发布时间", "发布日期")),
            opening_at=first_date(body, ("开标时间", "投标截止时间", "递交截止时间")),
            notice_type="招标公告", attachments=self._attachments_from_links(parser.links, text_url),
        )


class ChngAdapter(BaseAdapter):
    code = "chng"
    api_root = "https://ec.chng.com.cn/scm-uiaoauth-web/s/business/uiaouth/"

    def health_check(self) -> dict[str, Any]:
        try:
            self.list_notices(max_pages=1, max_notices=1)
            return {"ok": True, "status_code": 200, "message": "公开公告列表正常"}
        except (AdapterError, httpx.HTTPError) as exc:
            return {"ok": False, "status_code": None, "message": str(exc)}

    @staticmethod
    def public_detail_url(url: str) -> str:
        query = parse_qs(urlparse(url).query)
        if not query and "?" in urlparse(url).fragment:
            query = parse_qs(urlparse(url).fragment.split("?", 1)[1])
        notice_id = (query.get("id") or query.get("announcementId") or [""])[0]
        return f"https://ec.chng.com.cn/channel/home/#/detail?id={notice_id}" if notice_id else url

    def _json(self, path: str, method: str = "GET", payload: dict | None = None) -> dict[str, Any]:
        url = urljoin(self.api_root, path)
        if self.browser_site_id:
            from .manual_verification import ManualVerificationError, browser_request
            try:
                return browser_request(self.browser_site_id, url, method, payload, port=self.browser_port)
            except ManualVerificationError as exc:
                raise AdapterError(str(exc)) from exc
        response = self.client.request(method, url, json=payload, headers={"Referer": self.base_url})
        if response.status_code == 412 or "$_ts" in response.text:
            raise AdapterError("中国华能要求使用专用浏览器采集，请打开人工验证窗口并保持窗口运行")
        response.raise_for_status()
        return response.json()

    def list_notices(self, max_pages: int = 1, max_notices: int = 100, list_url: str | None = None, exclude_external_ids: set[str] | None = None) -> list[NoticeSummary]:
        del list_url
        result: list[NoticeSummary] = []
        excluded = exclude_external_ids or set()
        seen: set[str] = set()
        recognized = False
        for page in range(1, max_pages + 1):
            payload = self._json("queryAnnouncementByTitle", "POST", {"type": "103", "start": (page - 1) * 10, "limit": 10})
            found = 0
            rows = payload.get("root") or []
            for row in rows:
                notice_id = str(row.get("announcementId") or "")
                title = str(row.get("announcementTitle") or "").strip()
                if not notice_id or not title:
                    continue
                recognized = True
                if notice_id in seen:
                    continue
                seen.add(notice_id)
                found += 1
                if notice_id in excluded:
                    continue
                created = row.get("createtime")
                published = datetime.fromtimestamp(created / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S") if isinstance(created, (int, float)) else None
                result.append(NoticeSummary(notice_id, title, f"https://ec.chng.com.cn/channel/home/#/detail?id={notice_id}", published_at=published))
                if len(result) >= max_notices:
                    return result
            if not found or len(rows) < 10:
                break
        if not result and not recognized:
            raise AdapterError("中国华能公开公告列表未返回可识别数据")
        return result

    def fetch_notice(self, url: str, external_id: str | None = None, detail_id: str | None = None) -> NoticeData:
        del detail_id
        public_url = self.public_detail_url(url)
        notice_id = external_id or (parse_qs(urlparse(public_url).fragment.split("?", 1)[1]).get("id") or [""])[0]
        payload = self._json("announcementDetail?" + urlencode({"announcementId": notice_id}))
        announcement = ((payload.get("data") or {}).get("announcement") or {})
        raw = str(announcement.get("announcementHtml") or "")
        title = str(announcement.get("announcementTitle") or "").strip()
        if not raw or not title:
            raise AdapterError("中国华能公告详情接口未返回完整正文")
        parser = PageParser()
        parser.feed(raw)
        body = clean_text(parser.text_parts)
        created = announcement.get("createtime")
        published = datetime.fromtimestamp(created / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S") if isinstance(created, (int, float)) else first_date(body, ("发布时间", "发布日期"))
        return NoticeData(str(notice_id), title, public_url, body, raw, published_at=published,
                          opening_at=first_date(body, ("开标时间", "投标截止时间", "递交截止时间")),
                          notice_type="招标公告", attachments=self._attachments_from_links(parser.links, public_url))


class CdtAdapter(BaseAdapter):
    code = "cdt"
    list_api = "https://tang.cdt-ec.com/notice/moreController/getList"

    def health_check(self) -> dict[str, Any]:
        try:
            self.list_notices(max_pages=1, max_notices=1)
            return {"ok": True, "status_code": 200, "message": "公开公告列表正常"}
        except (AdapterError, httpx.HTTPError) as exc:
            return {"ok": False, "status_code": None, "message": str(exc)}

    def list_notices(self, max_pages: int = 1, max_notices: int = 100, list_url: str | None = None, exclude_external_ids: set[str] | None = None) -> list[NoticeSummary]:
        del list_url
        result: list[NoticeSummary] = []
        excluded = exclude_external_ids or set()
        seen: set[str] = set()
        recognized = False
        page_size = min(50, max_notices)
        for page in range(1, max_pages + 1):
            data = {"page": page, "limit": page_size, "messagetype": "0", "startDate": "", "endDate": ""}
            try:
                if getattr(self, "browser_site_id", None):
                    from .manual_verification import ManualVerificationError, browser_request
                    try:
                        payload = browser_request(
                            self.browser_site_id, self.list_api, "POST", data,
                            port=self.browser_port, form_encoded=True,
                        )
                    except ManualVerificationError as exc:
                        raise AdapterError(str(exc)) from exc
                else:
                    response = self.client.post(self.list_api, data=data, headers={"Referer": self.base_url})
                    response.raise_for_status()
                    if response.text.lstrip().startswith("<"):
                        raise AdapterError("大唐集团列表触发平台安全验证，未将验证页作为公告入库")
                    payload = response.json()
            except AdapterError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                raise AdapterError(f"大唐集团公告列表访问失败：{exc}") from exc
            rows = payload.get("data") or []
            page_found = 0
            for row in rows:
                notice_id = str(row.get("id") or "")
                title = str(row.get("message_title") or "").strip()
                if not notice_id or not title:
                    continue
                recognized = True
                if notice_id in seen:
                    continue
                seen.add(notice_id)
                page_found += 1
                if notice_id in excluded:
                    continue
                result.append(NoticeSummary(
                    notice_id, title,
                    f"https://tang.cdt-ec.com/notice/moreController/moreall?id={notice_id}",
                    row.get("publish_time"), self._notice_type_from_title(title),
                ))
                if len(result) >= max_notices:
                    return result
            if not page_found or len(rows) < page_size:
                break
        if not result and not recognized:
            raise AdapterError("大唐集团公开公告列表未返回可识别数据")
        return result

    @staticmethod
    def _notice_type_from_title(title: str) -> str:
        if "资格预审" in title or "资审公告" in title:
            return "资格预审公告"
        if "采购公告" in title:
            return "采购公告"
        return "招标公告"

    def fetch_notice(self, url: str, external_id: str | None = None, detail_id: str | None = None) -> NoticeData:
        del detail_id
        try:
            if getattr(self, "browser_site_id", None):
                from .manual_verification import ManualVerificationError, browser_request
                try:
                    raw_html = browser_request(
                        self.browser_site_id, url, port=self.browser_port, parse_json=False,
                    )
                except ManualVerificationError as exc:
                    raise AdapterError(str(exc)) from exc
            else:
                response = self.client.get(url)
                response.raise_for_status()
                raw_html = response.text
        except httpx.HTTPError as exc:
            raise AdapterError(f"大唐集团公告详情访问失败：{exc}") from exc
        lowered = raw_html.lower()
        if "aliyunwaf" in lowered or re.search(r"\barg1\s*=", raw_html):
            raise AdapterError("大唐集团详情页触发平台安全验证，请重新完成人工验证")
        parser = PageParser()
        parser.feed(raw_html)
        full_text = clean_text(parser.text_parts)
        body = clean_text(parser.content_parts or parser.text_parts)
        title = clean_text(parser.heading_parts).strip() or clean_text(parser.title_parts).strip(" -_|") or str(external_id or extract_external_id(url))
        attachments = self._attachments_from_links(parser.links, url)
        seen_urls = {item.url for item in attachments}
        pdf_pattern = re.compile(r"https?://bid\.cdt-ec\.com/dtdzzb/cgUploadController\.do\?downLoadFileOut&extend=pdf&objId=[^\"'<>\s]+", re.I)
        for match in pdf_pattern.findall(raw_html):
            pdf_url = re.sub(r"^http://", "https://", match, flags=re.I)
            if pdf_url not in seen_urls:
                seen_urls.add(pdf_url)
                attachments.append(AttachmentInfo(name=f"大唐公告-{external_id or extract_external_id(url)}.pdf", url=pdf_url))
        return NoticeData(
            external_id=str(external_id or extract_external_id(url)), title=title, url=url,
            body_text=body, raw_html=raw_html,
            published_at=first_date(full_text, ("发布时间", "发布日期", "公告时间")),
            opening_at=first_date(body, ("开标时间", "投标截止时间", "递交截止时间")),
            notice_type=self._notice_type_from_title(title), attachments=attachments,
        )


ADAPTERS = {
    "csg": CsgAdapter, "ecp": EcpAdapter, "sgcc": SgccPortalAdapter,
    "epec": EpecAdapter, "chng": ChngAdapter, "cdt": CdtAdapter,
}


def make_adapter(code: str, base_url: str, session_cookie: str | None = None) -> BaseAdapter:
    from .public_platforms import PUBLIC_ADAPTERS

    adapter_class = ADAPTERS.get(code) or PUBLIC_ADAPTERS.get(code, BaseAdapter)
    return adapter_class(base_url, session_cookie=session_cookie)
