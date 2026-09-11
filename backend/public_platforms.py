"""Public sources verified against the platforms' own pages, not search mirrors."""
from __future__ import annotations

import json
import re
from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from .adapters import (
    AdapterError, AttachmentInfo, BaseAdapter, NoticeData, NoticeSummary,
    PageParser, clean_text, first_date,
)


def plain_html(value: object) -> str:
    parser = PageParser()
    parser.feed(str(value or ""))
    return re.sub(r"\s+", " ", "".join(parser.text_parts)).strip()


def notice_type(title: str, default: str = "招标公告") -> str:
    if "开标记录" in title:
        return "开标记录"
    if any(word in title for word in ("终止公告", "终止招标", "流标公告", "废标公告")):
        return "终止公告"
    if re.search(r"招标文件\s*$", title):
        return "招标文件"
    if any(word in title for word in ("中标", "成交结果", "候选人公示")):
        return "中标公示"
    if "资格预审" in title:
        return "资格预审公告"
    if any(word in title for word in ("询价", "询比", "竞价", "谈判", "直接采购", "单一来源", "比选", "磋商")):
        return "采购公告"
    if "招标" in title:
        return "招标公告"
    return "采购公告" if "采购公告" in title else default


class SectionParser(HTMLParser):
    """Keep only one business-content container, excluding recommendations/nav."""
    def __init__(self, class_name: str):
        super().__init__(convert_charrefs=False)
        self.class_name = class_name
        self.depth = 0
        self.found = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if not self.depth and not self.found:
            if self.class_name in (dict(attrs).get("class") or "").split():
                self.depth = 1
                self.found = True
                return
        if self.depth:
            self.parts.append(self.get_starttag_text())
            if tag not in PageParser.VOID_TAGS:
                self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.depth:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if self.depth and tag not in PageParser.VOID_TAGS:
            self.depth -= 1
            if self.depth:
                self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)

    def handle_entityref(self, name):
        self.handle_data(f"&{name};")

    def handle_charref(self, name):
        self.handle_data(f"&#{name};")


def section(raw: str, class_name: str) -> PageParser:
    selected = SectionParser(class_name)
    selected.feed(raw)
    parser = PageParser()
    parser.feed("".join(selected.parts))
    if not parser.text_parts:
        raise AdapterError(f"公告正文区域 {class_name} 未找到，可能需要验证或页面结构已变化")
    return parser


class PublicAdapter(BaseAdapter):
    def get(self, url, **kwargs):
        try:
            response = self.client.get(url, **kwargs)
            if response.status_code in (401, 403, 405, 412) or "您已被纳入黑名单" in response.text:
                raise AdapterError("平台限制访问，请检查原网站；黑名单限制需联系平台处理，保存会话不代表已解除限制")
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise AdapterError(f"公开公告访问失败：{exc}") from exc

    def health_check(self):
        try:
            rows = self.list_notices(1, 1)
            if not rows:
                return {"ok": False, "message": "列表没有可识别公告，无法确认可采集", "status_code": None}
            detail = self.fetch_notice(rows[0].url, rows[0].external_id)
            return {"ok": True, "message": detail.collection_warning or "公开列表与详情访问正常", "status_code": 200}
        except (AdapterError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "message": str(exc), "status_code": None}


class YfbAdapter(PublicAdapter):
    code = "yfb"
    enterprise_root = "https://qiye.qianlima.com/new_qd_yfbsite/"
    enterprise_list_url = "https://qiye.qianlima.com/new_qd_yfbsite/api/search"
    enterprise_member_url = "https://qiye.qianlima.com/new_qd_yfbsite/api/enterprise/selectUserVipInfo"
    enterprise_detail_url = "https://qiye.qianlima.com/new_qd_yfbsite/api/subZhaobiao/zbDetail"
    public_detail_url = "https://www.yfbzb.com/inviteBid/detail/{date}_{external_id}.html"

    def health_check(self):
        try:
            member = bool(self.browser_site_id and self._is_active_member())
            rows = (self._list_verified_enterprise(1, 1) if member else self._list_public(1, 1))
            if not rows:
                return {"ok": False, "message": "列表没有可识别公告，无法确认可采集", "status_code": None}
            detail = self.fetch_notice(rows[0].url, rows[0].external_id)
            mode = "member" if member else "public"
            message = ("会员采集正常" if member else "公开采集正常，正文可能不完整")
            return {"ok": True, "mode": mode, "message": detail.collection_warning or message, "status_code": 200}
        except (AdapterError, ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "message": str(exc), "status_code": None}

    def list_notices(self, max_pages=1, max_notices=100, list_url=None, exclude_external_ids=None):
        if getattr(self, "browser_site_id", None) and self._is_active_member():
            return self._list_verified_enterprise(max_pages, max_notices, exclude_external_ids)
        return self._list_public(max_pages, max_notices, list_url, exclude_external_ids)

    def _browser_json(self, url):
        from .manual_verification import ManualVerificationError, browser_request
        try:
            payload = browser_request(
                self.browser_site_id, url, port=self.browser_port,
                storage_query={"openid": "YFB-OpenId"}, authorization_cookie="Admin-Token",
            )
        except ManualVerificationError as exc:
            raise AdapterError(f"乙方宝企业接口访问失败：{exc}") from exc
        if not isinstance(payload, dict) or payload.get("code") != 200:
            message = payload.get("msg") if isinstance(payload, dict) else "响应格式异常"
            raise AdapterError(f"乙方宝企业接口访问失败：{message or '登录会话无效'}")
        return payload

    def _is_active_member(self):
        if hasattr(self, "_member_mode"):
            return self._member_mode
        data = (self._browser_json(self.enterprise_member_url).get("data") or {})
        end_time = str(data.get("endTime") or "")[:10]
        self._member_mode = bool(data.get("accountType") and end_time >= date.today().isoformat())
        return self._member_mode

    def _list_public(self, max_pages=1, max_notices=100, list_url=None, exclude_external_ids=None):
        result, seen = [], set()
        excluded = exclude_external_ids or set()
        for page in range(1, max_pages + 1):
            raw = self.get(list_url or self.base_url, params={"defaultSearch": "true", "pageNo": page}).text
            progress = False
            recognized = False
            for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", raw, re.S | re.I):
                parser = PageParser()
                parser.feed(row)
                for href, title in parser.links:
                    match = re.search(r"/inviteBid/detail/(\d{8})_(\d+)\.html", href)
                    if not match:
                        continue
                    recognized = True
                    key = match[2]
                    if key in seen:
                        continue
                    seen.add(key)
                    progress = True
                    if key in excluded or not title:
                        continue
                    kind = notice_type(title)
                    if kind != "招标公告":
                        continue
                    dates = re.findall(r"(?m)^\s*(20\d{2}-\d{2}-\d{2})\s*$", clean_text(parser.text_parts))
                    date = dates[-1] if dates else None
                    result.append(NoticeSummary(key, title, urljoin(self.base_url, href), date, kind))
                    if len(result) >= max_notices:
                        return result
            if not recognized and page == 1:
                raise AdapterError("乙方宝列表未识别到公告，请检查登录限制或页面变化")
            if not progress:
                break
        return result

    def _list_verified_enterprise(self, max_pages, max_notices, exclude_external_ids=None):
        result, seen = [], set()
        excluded = exclude_external_ids or set()
        for page in range(1, max_pages + 1):
            query = urlencode({
                "pageSize": 30, "pageNum": page, "pageFrom": "zhaobiao",
                "keyword": "", "filterCondition": 1, "searchType": "1", "timeOption": "2",
                "viewMonitor": "false", "defTimeFlag": "0",
            })
            payload = self._browser_json(f"{self.enterprise_list_url}?{query}")
            rows = (payload.get("data") or {}).get("resultList") or []
            progress = False
            for row in rows:
                external_id = str(row.get("contentId") or "").strip()
                title = plain_html(row.get("title"))
                if not external_id or not title:
                    continue
                if external_id in seen:
                    continue
                seen.add(external_id)
                progress = True
                if external_id in excluded or row.get("type") != "招标公告":
                    continue
                dates = re.findall(r"20\d{2}-\d{2}-\d{2}", str(row.get("updateTime") or ""))
                if not dates:
                    continue
                published_at = dates[0]
                area_id = str(row.get("areaId") or "2703")
                result.append(NoticeSummary(
                    external_id, title,
                    f"{self.enterprise_root}#/infoCenter/infoDetail/{external_id}/{area_id}/zhaobiao"
                    f"?fromPage=searchPage&published={published_at.replace('-', '')}",
                    published_at, "招标公告",
                ))
                if len(result) >= max_notices:
                    return result
            if not rows and page == 1:
                raise AdapterError("乙方宝企业公告列表未识别到招标公告，请检查登录限制或页面变化")
            if not progress:
                break
        return result

    def fetch_notice(self, url, external_id=None, detail_id=None):
        parsed = urlparse(url)
        enterprise_match = re.search(r"/infoCenter/infoDetail/(\d+)/([^/?]+)/zhaobiao", parsed.fragment)
        if enterprise_match and self.browser_site_id:
            return self._fetch_enterprise_notice(url, enterprise_match[1], enterprise_match[2])
        match = re.search(r"/inviteBid/detail/(\d{8})_(\d+)\.html", urlparse(url).path)
        if not match:
            raise AdapterError("请输入乙方宝公告详情链接")
        raw = self.get(url).text
        full = PageParser()
        full.feed(raw)
        title = clean_text(full.heading_parts)
        if not title:
            raise AdapterError("乙方宝未返回公告标题，可能需要登录验证")
        parser = section(raw, "content")
        body = clean_text(parser.text_parts)
        warning = "乙方宝部分正文或联系方式被会员权限隐藏，当前仅解析公开内容" if re.search(r"\*{3,}|点击登录查看|会员.*查看", body) else None
        kind = notice_type(title, default="其他公告")
        method = re.search(r"(?:采购|招标)方式\s*[：:]?\s*([^\n]{1,40})", body)
        if method:
            if "公开招标" in method[1]:
                kind = "招标公告"
            elif any(word in method[1] for word in ("动态报价", "网络采购", "询价", "询比", "竞价", "比选", "磋商", "谈判", "单一来源", "直接采购")):
                kind = "采购公告"
        elif kind == "其他公告" and re.search(r"招标公告|(?:采用|进行|通过|现对|采购方式[：:\s]*).{0,30}公开招标", body):
            kind = "招标公告"
        day = match[1]
        attachments = self._attachments_from_links(parser.links, url)
        attachments = [item for item in attachments if not (
            ((urlparse(item.url).hostname or "").lower() == "qiye.qianlima.com" and urlparse(item.url).fragment.startswith("/infoCenter/"))
            or (urlparse(item.url).path.endswith("/downloads/agent.jsp") and not parse_qs(urlparse(item.url).query, keep_blank_values=False).get("req"))
        )]
        return NoticeData(
            external_id or match[2], title, url, body, raw,
            published_at=f"{day[:4]}-{day[4:6]}-{day[6:]}",
            opening_at=first_date(body, ("开标时间", "投标截止时间")),
            notice_type=kind, attachments=attachments,
            collection_warning=warning,
        )

    def _fetch_enterprise_notice(self, url, external_id, area_id):
        from .manual_verification import ManualVerificationError, browser_page_json
        try:
            payload = browser_page_json(
                self.browser_site_id, url, "/subZhaobiao/zbDetail",
                port=self.browser_port,
            )
        except ManualVerificationError as exc:
            raise AdapterError(f"乙方宝会员详情访问失败：{exc}") from exc
        data = payload.get("data") or {}
        if data.get("errType") or not data.get("content"):
            reason = data.get("errMsg") or data.get("msg") or data.get("errType") or "响应正文为空"
            raise AdapterError(f"乙方宝会员详情未返回完整正文（{reason}），未降级采集公开页面")
        raw = str(data.get("content") or "")
        parser = PageParser()
        parser.feed(raw)
        body = clean_text(parser.text_parts)
        published = str(data.get("updateDate") or "").replace("/", "-")[:10] or None
        attachments = []
        for item in data.get("downlinkList") or []:
            attachment_url = str(item.get("url") or "").strip()
            parsed_attachment = urlparse(attachment_url)
            if not attachment_url or parsed_attachment.fragment.startswith("/infoCenter/"):
                continue
            if parsed_attachment.hostname in {"qianlima.com", "www.qianlima.com"} and parsed_attachment.scheme == "http":
                attachment_url = parsed_attachment._replace(scheme="https").geturl()
            attachments.append(AttachmentInfo(plain_html(item.get("title") or "附件"), attachment_url))
        return NoticeData(
            external_id, plain_html(data.get("title") or external_id), url, body, raw,
            published_at=published,
            opening_at=str(data.get("openBidTime") or "") or first_date(body, ("开标时间", "投标截止时间")),
            notice_type=notice_type(str(data.get("title") or "")), attachments=attachments,
        )

    def _fetch_public_notice(self, url, external_id=None):
        return self.fetch_notice(url, external_id)


class NeepAdapter(PublicAdapter):
    code = "neep"
    ARTICLE_HOSTS = {"gd-prod.oss-cn-beijing.aliyuncs.com", "gd-prod.cn-beijing.oss.aliyuncs.com"}
    LIST_API = "https://www.neep.shop/rest/service/routing/nouser/inquiry/quote/searchCmsArticleList"

    def list_notices(self, max_pages=1, max_notices=100, list_url=None, exclude_external_ids=None):
        result, seen = [], set()
        excluded = exclude_external_ids or set()
        # This verified public column is inquiry procurement, not tender notices.
        for page in range(1, max_pages + 1):
            raw = self.get(self.LIST_API, params={"noticeType": 1, "pageNo": page, "callback": "cb", "quotDeadline": "", "inquireName": "", "publishArea": "", "inquireCode": ""}).text.strip()
            match = re.fullmatch(r"cb\((.*)\);?", raw, re.S)
            payload = json.loads(match[1] if match else raw)
            if str(payload.get("respCode")) != "0000":
                raise AdapterError("国家能源 e购公告接口返回失败：" + str(payload.get("respDesc", "未知响应")))
            rows = payload.get("data", {}).get("rows")
            if not isinstance(rows, list):
                raise AdapterError("国家能源 e购列表结构发生变化")
            progress = False
            for row in rows:
                key, title, url = str(row.get("articleId") or ""), row.get("inquireName"), row.get("articleUrl") or ""
                if not key or not title or urlparse(url).hostname not in self.ARTICLE_HOSTS:
                    raise AdapterError("国家能源 e购返回无法识别的公告或非公开公告地址")
                if key in seen:
                    continue
                seen.add(key)
                progress = True
                if key in excluded:
                    continue
                result.append(NoticeSummary(key, title, url, row.get("publishTimeString"), "采购公告"))
                if len(result) >= max_notices:
                    return result
            if not progress:
                break
        return result

    def fetch_notice(self, url, external_id=None, detail_id=None):
        parsed = urlparse(url)
        match = re.fullmatch(r"/upload/cms/article/[^/]+/(\d+)\.html", parsed.path)
        if parsed.hostname not in self.ARTICLE_HOSTS or not match:
            raise AdapterError("请输入国家能源 e购公开公告详情链接")
        raw = self.get(url).text
        parser = section(raw, "right-main-content")
        # Repeated dates are meaningful field values; clean_text deduplicates
        # them and can leave 发布时间 pointing at the later quotation deadline.
        body = "\n".join(parser.text_parts)
        title = re.search(r"项目名称\s*[：:]\s*([^\n]+)", body)
        if not title:
            raise AdapterError("国家能源 e购详情没有项目名称")
        attachments = self._attachments_from_links(parser.links, url)
        warning = "原公告注明有附件，但公开页面没有提供下载地址，附件内容尚未匹配" if re.search(r"附件\s*[：:]\s*有", body) and not attachments else None
        published = re.search(r"发布时间\s*[：:]\s*(20\d{2}-\d{2}-\d{2}(?:[ \t]+\d{2}:\d{2}:\d{2})?)", body)
        return NoticeData(
            external_id or match[1], title[1].strip(), url, body, raw,
            published_at=published[1] if published else None, opening_at=first_date(body, ("报价截止时间",)),
            notice_type="采购公告", attachments=attachments, collection_warning=warning,
        )


class ChnenergyAdapter(PublicAdapter):
    """国能e招：只读取招标公告栏目（货物、工程、服务）。"""
    code = "chnenergy"
    ROOT = "https://www.chnenergybidding.com.cn"
    LIST_PATH = "/bidweb/001/001002/"
    DETAIL_PATH = re.compile(r"/bidweb/001/001002/00100200[123]/(\d{8})/([a-fA-F0-9-]{36})\.html")

    @classmethod
    def is_source(cls, url):
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.hostname == "www.chnenergybidding.com.cn"

    def list_notices(self, max_pages=1, max_notices=100, list_url=None, exclude_external_ids=None):
        url = list_url or self.ROOT + self.LIST_PATH + "moreinfo.html"
        if not self.is_source(url) or not re.fullmatch(self.LIST_PATH + r"(?:moreinfo|\d+)\.html", urlparse(url).path):
            raise AdapterError("国能e招仅支持招标公告列表入口")
        result, seen, visited = [], set(), set()
        excluded = exclude_external_ids or set()
        for _ in range(max_pages):
            if url in visited:
                break
            visited.add(url)
            raw = self.get(url).text
            progress = False
            recognized = False
            for row in re.findall(r"<li\b[^>]*>(.*?)</li>", raw, re.S | re.I):
                parser = PageParser()
                parser.feed(row)
                for href, title in parser.links:
                    link = urljoin(url, href)
                    match = self.DETAIL_PATH.fullmatch(urlparse(link).path)
                    # The same row links both the project number and title.
                    if not self.is_source(link) or not match or not re.search(r"[\u4e00-\u9fff]", title):
                        continue
                    recognized = True
                    key = match[2].lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    progress = True
                    if key in excluded:
                        continue
                    dates = re.findall(r"(?m)^\s*(20\d{2}-\d{2}-\d{2})\s*$", "\n".join(parser.text_parts))
                    result.append(NoticeSummary(key, title, link, dates[-1] if dates else None, "招标公告"))
                    if len(result) >= max_notices:
                        return result
            if not recognized:
                raise AdapterError("国能e招列表未识别到招标公告，可能触发验证或页面结构变化")
            if not progress:
                break
            page = PageParser()
            page.feed(raw)
            next_url = next((urljoin(url, href) for href, label in page.links if label.startswith("下页")), None)
            if not next_url:
                break
            if not self.is_source(next_url) or not re.fullmatch(self.LIST_PATH + r"\d+\.html", urlparse(next_url).path):
                raise AdapterError("国能e招下一页链接不属于招标公告栏目")
            url = next_url
        return result

    def fetch_notice(self, url, external_id=None, detail_id=None):
        match = self.DETAIL_PATH.fullmatch(urlparse(url).path)
        if not self.is_source(url) or not match:
            raise AdapterError("请输入国能e招招标公告详情链接；不采集询价、资格预审、中标和终止公告")
        raw = self.get(url).text
        article = section(raw, "article-info")
        title = clean_text(article.heading_parts)
        if not title:
            raise AdapterError("国能e招详情缺少公告标题")
        body = "\n".join(section(raw, "con").text_parts)
        metadata = "\n".join(section(raw, "info-sources").text_parts)
        # Attachment area is a sibling of article-info, inside article.
        links = section(raw, "article").links
        attachments = self._attachments_from_links(links, url)
        return NoticeData(
            external_id or match[2].lower(), title, url, body, raw,
            published_at=first_date(metadata, ("发布时间",)),
            opening_at=first_date(body, ("开标时间", "投标截止时间", "投标文件递交的截止时间")),
            notice_type="招标公告", attachments=attachments,
        )


class CgnAdapter(PublicAdapter):
    code = "cgn"
    ROOT = "https://ecp.cgnpc.com.cn/"

    def list_notices(self, max_pages=1, max_notices=100, list_url=None, exclude_external_ids=None):
        raw = self.get(list_url or urljoin(self.ROOT, "zbgg.html")).text
        page = re.search(r"window\.pageId\s*=\s*['\"]([a-f0-9]{32})['\"]", raw)
        struct = re.search(r"window\.struct\s*=\s*(?=\{)", raw)
        if not page or not struct:
            raise AdapterError("中广核公告列表配置未找到")
        config, _ = json.JSONDecoder().raw_decode(raw[struct.end():])
        def components(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from components(child)
            elif isinstance(value, list):
                for child in value:
                    yield from components(child)
        part = next((item for item in components(config) if item.get("option", {}).get("partType") == "page" and item.get("option", {}).get("data", {}).get("dataId")), None)
        if not part or not part.get("id"):
            raise AdapterError("中广核分页组件未找到")
        store = part["option"]["data"]["dataId"]
        result, seen = [], set()
        excluded = exclude_external_ids or set()
        for number in range(1, max_pages + 1):
            if number <= 10:
                payload = self.get(urljoin(self.ROOT, f"content/{page[1]}/{part['id']}/{number}.json")).json()
            else:
                response = self.client.post(urljoin(self.ROOT, "portalApi/content/queryPage"), json={"siteId": "3f5692fa17a24c86b80a08e6669a4df5", "lang": "zh-cn", "pageId": page[1], "partId": part["id"], "pageIndex": number, "SiteType": 0})
                response.raise_for_status()
                payload = response.json()
            rows = payload.get("list")
            if not isinstance(rows, list):
                raise AdapterError("中广核分页数据结构发生变化")
            progress = False
            for row in rows:
                key = str(row.get("Id") or "")
                if not re.fullmatch(r"[a-fA-F0-9]{32}", key) or not row.get("Title"):
                    raise AdapterError("中广核列表公告标识或标题缺失")
                if key in seen:
                    continue
                seen.add(key)
                progress = True
                if key in excluded:
                    continue
                url = urljoin(self.ROOT, "Details.html") + "?" + urlencode({"dataId": store, "detailId": key})
                result.append(NoticeSummary(key, row["Title"], url, row.get("IssueTime"), notice_type(row["Title"])))
                if len(result) >= max_notices:
                    return result
            if not progress or number * int(part["option"]["data"].get("pageSize", 15)) >= int(payload.get("total", 0)):
                break
        return result

    def fetch_notice(self, url, external_id=None, detail_id=None):
        query = parse_qs(urlparse(url).query)
        store, key = query.get("dataId", [""])[0], query.get("detailId", [""])[0]
        if not all(re.fullmatch(r"[a-fA-F0-9]{32}", value) for value in (store, key)):
            raise AdapterError("中广核详情链接缺少有效的 dataId 或 detailId")
        payload = self.get(urljoin(self.ROOT, f"detail/{store}/{key}.json")).json()
        raw, title = payload.get("Body") or "", payload.get("Title")
        parser = PageParser()
        parser.feed(raw)
        if not title or not parser.text_parts:
            raise AdapterError("中广核没有返回完整公告正文")
        attachments = self._attachments_from_links(parser.links, self.ROOT)
        seen = {item.url for item in attachments}
        for name in ("Attachment", "BodyAttachment"):
            items = payload.get(name) or []
            if isinstance(items, str):
                items = json.loads(items)
            for item in items:
                link = urljoin(self.ROOT, item.get("url") or "")
                if item.get("url") and item.get("name") and link not in seen:
                    seen.add(link)
                    attachments.append(AttachmentInfo(item["name"], link))
        return NoticeData(external_id or key, title, url, clean_text(parser.text_parts), raw, published_at=payload.get("IssueTime"), opening_at=payload.get("BidEndTime"), notice_type=notice_type(title), attachments=attachments)


class CebAdapter(PublicAdapter):
    code = "ceb"
    LIMITATION = "中国招标投标公共服务平台公告列表可直接读取，但详情跳转 ctbpsp.com 后要求每次提供交互验证码令牌；当前不能进行无人值守的完整采集"

    def health_check(self):
        return {"ok": False, "status_code": None, "message": self.LIMITATION}

    def list_notices(self, max_pages=1, max_notices=100, list_url=None, exclude_external_ids=None):
        # Stop before issuing thousands of detail requests that cannot succeed.
        raise AdapterError(self.LIMITATION)

    def fetch_notice(self, url, external_id=None, detail_id=None):
        raise AdapterError(self.LIMITATION)


class EspicAdapter(PublicAdapter):
    code = "espic"
    LIMITATION = "中国电力设备信息网采集适配尚未完成，暂不能采集；打开人工验证可检查网站访问情况，但验证不能代替公告列表与详情适配"

    def health_check(self):
        try:
            response = self.get(self.base_url)
            if "WEB 应用防火墙" in response.text or "remote-shield/start" in response.text:
                return {"ok": False, "status_code": response.status_code, "message": "中国电力设备信息网招标列表仍停留在 WEB 应用防火墙验证页，未返回公告数据"}
        except AdapterError as exc:
            return {"ok": False, "status_code": None, "message": str(exc)}
        return {"ok": False, "status_code": None, "message": self.LIMITATION}

    def list_notices(self, max_pages=1, max_notices=100, list_url=None, exclude_external_ids=None):
        raise AdapterError(self.LIMITATION)

    def fetch_notice(self, url, external_id=None, detail_id=None):
        raise AdapterError(self.LIMITATION)


class ChdtpAdapter(PublicAdapter):
    code = "chdtp"
    ROOT = "https://www.chdtp.com/"
    LIST_URL = urljoin(ROOT, "pages/wzglS/cgxx/caigou.jsp?cgtype=4")
    DATA_URL = urljoin(ROOT, "webs/queryWebZbgg.action?zbggType=1")

    @staticmethod
    def _blocked(response):
        text = response.text
        return response.status_code == 412 or "/sgodapt2y.js" in text or "l='d'" in text[:2000]

    def _page(self, url, method="GET", payload=None):
        if getattr(self, "browser_site_id", None):
            try:
                from .manual_verification import ManualVerificationError, browser_request
                return browser_request(
                    self.browser_site_id, url, method, payload, port=self.browser_port,
                    form_encoded=payload is not None, parse_json=False,
                )
            except ManualVerificationError as exc:
                raise AdapterError(f"中国华电专用浏览器访问失败：{exc}") from exc
        response = self.client.request(
            method, url, data=payload, headers={"Referer": self.LIST_URL},
        )
        if self._blocked(response):
            raise AdapterError("中国华电平台触发安全验证，请在“平台与账号”完成人工验证后再采集")
        response.raise_for_status()
        return response.text

    def health_check(self):
        try:
            rows = self.list_notices(1, 1)
            return {"ok": bool(rows), "status_code": 200, "message": "人工验证有效，招标公告列表正常"}
        except (AdapterError, httpx.HTTPError) as exc:
            return {"ok": False, "status_code": None, "message": str(exc)}

    def list_notices(self, max_pages=1, max_notices=100, list_url=None, exclude_external_ids=None):
        entry = list_url or self.DATA_URL
        if (urlparse(entry).hostname or "").lower() != "www.chdtp.com":
            raise AdapterError("中国华电列表地址不属于官方平台")
        result, seen = [], set()
        excluded = exclude_external_ids or set()
        for page in range(1, max_pages + 1):
            payload = None if page == 1 else {"zbggType": "1", "page.currentpage": page}
            raw = self._page(entry, "POST" if payload else "GET", payload)
            progress = False
            recognized = False
            for attrs, inner in re.findall(r"<a\b([^>]*)>(.*?)</a>", raw, re.S | re.I):
                href_match = re.search(r"\bhref\s*=\s*([\"'])(.*?)\1", attrs, re.S | re.I)
                if not href_match:
                    continue
                href = unescape(href_match.group(2)).strip()
                script_path = re.search(r"toGetContent\(\s*['\"]([^'\"]+)['\"]\s*\)", href, re.I)
                if script_path:
                    absolute = urljoin(self.ROOT, "staticPage/" + script_path.group(1).lstrip("/"))
                else:
                    absolute = urljoin(entry, href)
                parsed = urlparse(absolute)
                title_match = re.search(r"\btitle\s*=\s*([\"'])(.*?)\1", attrs, re.S | re.I)
                label = unescape(title_match.group(2) if title_match else re.sub(r"<[^>]+>", "", inner)).strip()
                if parsed.hostname != "www.chdtp.com" or not label or "招标" not in label:
                    continue
                recognized = True
                key = extract_chdtp_id(absolute)
                if not key or key in seen:
                    continue
                seen.add(key)
                progress = True
                if key in excluded:
                    continue
                context = raw[max(0, raw.find(href) - 300):raw.find(href) + len(href) + 500]
                date = first_date("发布时间：" + clean_text(re.findall(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?", context)), ("发布时间",))
                result.append(NoticeSummary(key, label.strip(), absolute, date, "招标公告"))
                if len(result) >= max_notices:
                    return result
            if not recognized or not progress:
                break
        if not result and not seen:
            raise AdapterError("中国华电招标公告列表未返回可识别数据，可能需要重新人工验证或页面结构已变化")
        return result

    def fetch_notice(self, url, external_id=None, detail_id=None):
        if (urlparse(url).hostname or "").lower() != "www.chdtp.com":
            raise AdapterError("请输入中国华电官方招标公告详情链接")
        raw = self._page(url)
        parser = PageParser()
        parser.feed(raw)
        text = clean_text(parser.text_parts)
        title = clean_text(parser.heading_parts).strip() or next((part for part in parser.title_parts if "招标公告" in part), "")
        if not title or "招标公告" not in title:
            raise AdapterError("中国华电详情未识别到招标公告标题，未将当前页面入库")
        return NoticeData(
            external_id or extract_chdtp_id(url), title, url, text, raw,
            published_at=first_date(text, ("发布时间", "发布日期", "公告时间")),
            opening_at=first_date(text, ("开标时间", "投标截止时间", "递交截止时间")),
            notice_type="招标公告", attachments=self._attachments_from_links(parser.links, url),
        )


def extract_chdtp_id(url):
    query = parse_qs(urlparse(url).query)
    for name in ("id", "noticeId", "noticeid", "guid", "objectId", "objId"):
        if query.get(name) and query[name][0]:
            return query[name][0]
    tail = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    value = re.sub(r"\.(?:jsp|html?)$", "", tail, flags=re.I)
    return value if value and value.lower() not in {"index", "detail", "show", "view"} else None


PUBLIC_ADAPTERS = {"yfb": YfbAdapter, "neep": NeepAdapter, "chnenergy": ChnenergyAdapter, "cgn": CgnAdapter, "ceb": CebAdapter, "espic": EspicAdapter, "chdtp": ChdtpAdapter}
