from __future__ import annotations

import io
import base64
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.adapters import BaseAdapter, CdtAdapter, ChngAdapter, CsgAdapter, EpecAdapter, PageParser, SgccPortalAdapter, clean_text, extract_external_id, first_date
from backend.matching import match_sources
from backend.parsers import parse_document, safe_extract_zip


class AdapterParsingTests(unittest.TestCase):
    class JsonResponse:
        def __init__(self, payload: dict) -> None:
            self.payload = payload
            self.text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self.payload

    def test_saved_browser_user_agent_is_reused_but_not_sent_as_cookie(self) -> None:
        user_agent = "Mozilla/5.0 Edg/140.0"
        encoded = base64.urlsafe_b64encode(user_agent.encode()).decode()
        with unittest.mock.patch('backend.adapters.detect_outbound_proxy', return_value=None):
            adapter = BaseAdapter('https://example.com', f'session=ok; __scout_user_agent={encoded}')
        try:
            self.assertEqual(adapter.client.headers['User-Agent'], user_agent)
            self.assertNotIn('__scout_user_agent', adapter.client.cookies)
        finally:
            adapter.close()

    def test_saved_browser_port_is_routing_metadata_not_target_cookie(self) -> None:
        with unittest.mock.patch('backend.adapters.detect_outbound_proxy', return_value=None):
            adapter = BaseAdapter(
                'https://example.com',
                'session=ok; __scout_browser_session=257; __scout_browser_port=43210',
            )
        try:
            self.assertEqual(adapter.browser_site_id, 257)
            self.assertEqual(adapter.browser_port, 43210)
            self.assertNotIn('__scout_browser_session', adapter.client.cookies)
            self.assertNotIn('__scout_browser_port', adapter.client.cookies)
        finally:
            adapter.close()

    def test_hash_route_external_ids(self) -> None:
        self.assertEqual(
            extract_external_id("https://ecp.sgcc.com.cn/ecp2.0/portal/#/doc/doci-bid/2609030023176732_2018032900295987"),
            "2609030023176732",
        )
        self.assertEqual(extract_external_id("https://www.bidding.csg.cn/zbgg/1200437842.jhtml"), "1200437842")

    def test_heading_wins_and_scripts_are_ignored(self) -> None:
        parser = PageParser()
        parser.feed("<title>站点标题</title><script>noise()</script><h1>真实公告标题</h1><p>正文</p>")
        self.assertEqual(parser.heading_parts, ["真实公告标题"])
        self.assertNotIn("noise()", parser.text_parts)

    def test_date_parser_rejects_incomplete_date_and_prefers_opening_time(self) -> None:
        text = "投标文件递交截止时间\n2026年09月0\n6. 开标时间及地点\n开标时间：\n2026年09月07日09时00分00秒"
        self.assertEqual(first_date(text, ("开标时间", "投标文件递交截止时间")), "2026-09-07 09:00:00")
        self.assertIsNone(first_date("发布时间：2026年09月0", ("发布时间",)))

    def test_content_container_excludes_navigation_and_footer(self) -> None:
        parser = PageParser()
        parser.feed("""<nav>采购公告</nav><h1>服务器项目</h1><div class="Content"><p>采购服务器设备</p><div><p>项目正文</p></div></div><footer>南网党校和培训中心</footer>""")
        self.assertEqual(clean_text(parser.content_parts), "采购服务器设备\n项目正文")
        self.assertNotIn("培训", clean_text(parser.content_parts))

    def test_attachment_extension_in_label_is_detected(self) -> None:
        adapter = BaseAdapter.__new__(BaseAdapter)
        result = adapter._attachments_from_links(
            [("/filesrv/srv/file/download/123/opaque-id", "项目清单.xlsx")],
            "https://www.example.com/zbgg/1.jhtml",
        )
        self.assertEqual([item.name for item in result], ["项目清单.xlsx"])

    def test_csg_skips_existing_ids_and_continues_to_next_page(self) -> None:
        class Response:
            def __init__(self, text: str) -> None:
                self.text = text

            def raise_for_status(self) -> None:
                return None

        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.pages = {
                    "https://www.bidding.csg.cn/zbgg/index.jhtml": '<a href="/zbgg/100.jhtml">历史公告</a>',
                    "https://www.bidding.csg.cn/zbgg/index_2.jhtml": '<a href="/zbgg/101.jhtml">新公告一</a><a href="/zbgg/102.jhtml">新公告二</a>',
                }

            def get(self, url: str) -> Response:
                self.calls.append(url)
                return Response(self.pages[url])

        adapter = CsgAdapter.__new__(CsgAdapter)
        adapter.base_url = "https://www.bidding.csg.cn/"
        adapter.client = Client()
        result = adapter.list_notices(max_pages=2, max_notices=2, exclude_external_ids={"100"})

        self.assertEqual([item.external_id for item in result], ["101", "102"])
        self.assertEqual(len(adapter.client.calls), 2)

    def test_sgcc_portal_skips_existing_ids_and_continues_pages(self) -> None:
        class Response:
            def __init__(self, payload: dict) -> None:
                self.payload = payload

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return self.payload

        class Client:
            def post(self, _url: str, json: dict, headers: dict) -> Response:
                del headers
                rows = ([
                    {"firstPageDocId": "200", "noticeId": "detail-200", "doctype": "doci-bid", "title": "历史公告"},
                    {"firstPageDocId": "201", "noticeId": "detail-201", "doctype": "doci-bid", "title": "新公告一"},
                ] if json["index"] == 1 else [{"firstPageDocId": "202", "noticeId": "detail-202", "doctype": "doci-change", "title": "新公告二"}])
                return Response({"successful": True, "resultValue": {"noteList": rows}})

        adapter = SgccPortalAdapter.__new__(SgccPortalAdapter)
        adapter.portal_root = "https://sgccetp.com.cn/portal/"
        adapter.api_root = "https://sgccetp.com.cn/ecpwcmcore"
        adapter.client = Client()
        result = adapter.list_notices(max_pages=2, max_notices=2, exclude_external_ids={"200"})

        self.assertEqual([item.external_id for item in result], ["201", "202"])
        self.assertEqual([item.detail_id for item in result], ["detail-201", "detail-202"])
        self.assertIn("/detail-201_2018032700291334", result[0].url)

    def test_epec_list_uses_noticefile_detail(self) -> None:
        class Client:
            def post(inner_self, _url: str, json: dict, headers: dict) -> AdapterParsingTests.JsonResponse:
                del headers
                self.assertEqual(json["start"], 0)
                self.assertEqual(json["currentPage"], 1)
                self.assertEqual(json["pageSize"], 50)
                return self.JsonResponse({"status": True, "data": {"root": [{
                    "noticeId": "9001", "noticeTitle": "变压器招标公告", "noticeTypeName": "招标公告",
                    "releaseTime": "2026-09-08 10:00:00", "bidOpeningTime": "2026-09-18 09:00:00",
                    "businessId": "biz-1", "noticeType": "01", "attachUrl": "static/2026_9/a.txt",
                }], "totalCount": 1}})

        adapter = EpecAdapter.__new__(EpecAdapter)
        adapter.base_url = "https://bidding.epec.com/"
        adapter.client = Client()
        rows = adapter.list_notices()
        self.assertEqual(rows[0].external_id, "9001")
        self.assertIn("noticeId=9001", rows[0].url)
        self.assertEqual(rows[0].detail_id, "static/2026_9/a.txt")

    def test_epec_stops_when_backend_repeats_the_same_page(self) -> None:
        class Client:
            def __init__(inner_self) -> None:
                inner_self.requests = []

            def post(inner_self, _url: str, json: dict, headers: dict) -> AdapterParsingTests.JsonResponse:
                del headers
                inner_self.requests.append(json)
                return self.JsonResponse({"status": True, "data": {"root": [{
                    "noticeId": "same", "noticeTitle": "重复页公告", "noticeTypeName": "招标公告",
                    "releaseTime": "2026-09-08 10:00:00", "noticeType": "01", "attachUrl": "a.txt",
                }] * 50, "totalCount": 500}})

        adapter = EpecAdapter.__new__(EpecAdapter)
        adapter.base_url = "https://bidding.epec.com/"
        adapter.client = Client()
        rows = adapter.list_notices(max_pages=50, max_notices=100)
        self.assertEqual([row.external_id for row in rows], ["same"])
        self.assertEqual(len(adapter.client.requests), 2)
        self.assertEqual(adapter.client.requests[1]["currentPage"], 2)

    def test_cdt_list_uses_public_message_type_zero(self) -> None:
        class Client:
            def post(inner_self, _url: str, data: dict, headers: dict) -> AdapterParsingTests.JsonResponse:
                del headers
                self.assertEqual(data["messagetype"], "0")
                return self.JsonResponse({"data": [{"id": "1881919", "message_title": "风机采购公告", "publish_time": "2026-09-04 19:53:20"}], "count": 1})

        adapter = CdtAdapter.__new__(CdtAdapter)
        adapter.base_url = "https://tang.cdt-ec.com/"
        adapter.client = Client()
        rows = adapter.list_notices()
        self.assertEqual(rows[0].external_id, "1881919")
        self.assertTrue(rows[0].url.endswith("moreall?id=1881919"))
        self.assertEqual(rows[0].notice_type, "采购公告")

    def test_cdt_extracts_pdf_url_from_detail_script(self) -> None:
        class Response:
            headers = {"content-type": "text/html;charset=utf-8"}
            text = '''<html><h1>风机招标公告</h1><div class="content">发布时间：2026-09-08</div>
                <script>var pdf="http://bid.cdt-ec.com/dtdzzb/cgUploadController.do?downLoadFileOut&extend=pdf&objId=abc123";</script></html>'''

            def raise_for_status(self) -> None:
                return None

        class Client:
            def get(self, _url: str) -> Response:
                return Response()

        adapter = CdtAdapter.__new__(CdtAdapter)
        adapter.client = Client()
        notice = adapter.fetch_notice("https://tang.cdt-ec.com/notice/moreController/moreall?id=1", "1")
        self.assertEqual(len(notice.attachments), 1)
        self.assertEqual(notice.attachments[0].url, "https://bid.cdt-ec.com/dtdzzb/cgUploadController.do?downLoadFileOut&extend=pdf&objId=abc123")

    def test_chng_rewrites_hash_detail_to_public_html(self) -> None:
        adapter = ChngAdapter.__new__(ChngAdapter)
        self.assertEqual(
            adapter.public_detail_url("https://ec.chng.com.cn/channel/home/#/detail?id=12861658"),
            "https://ec.chng.com.cn/channel/home/#/detail?id=12861658",
        )


class WorkbookCompatibilityTests(unittest.TestCase):
    def test_mime_html_disguised_as_xlsx_is_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fake.xlsx"
            path.write_bytes(b"MIME-Version: 1.0\nContent-Transfer-Encoding: quoted-printable\nContent-Type: text/html\n\n<html><body><table><tr><td>server=20price</td></tr></table></body></html>")
            result = parse_document(path)
            self.assertEqual(result.status, "parsed")
            self.assertEqual(result.parser, "mime-html")
            self.assertIn("server price", result.text)


class ArchiveSafetyTests(unittest.TestCase):
    def test_zip_slip_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "blocked")
            with self.assertRaises(ValueError):
                safe_extract_zip(archive_path, Path(folder) / "out", 1024 * 1024, 1024 * 1024, 10, 3)

    def test_normal_zip_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            archive_path = Path(folder) / "ok.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("附件/清单.txt", "储能变流器")
            files = safe_extract_zip(archive_path, Path(folder) / "out", 1024 * 1024, 1024 * 1024, 10, 3)
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8"), "储能变流器")


class KeywordEvidenceTests(unittest.TestCase):
    def test_hit_keeps_source_and_snippet(self) -> None:
        group = {
            "id": 1, "include_any_json": '["储能"]', "include_all_json": '["采购"]',
            "phrases_json": "[]", "exclude_json": '["招聘"]', "synonyms_json": "{}",
            "scopes_json": '["body"]',
        }
        hits = match_sources(group, [{"source_type": "body", "source_file": "公告.html", "location": "正文", "text": "本项目采购储能设备。"}])
        self.assertTrue(hits)
        self.assertEqual(hits[0].source_file, "公告.html")
        self.assertIn("储能", hits[0].snippet)


if __name__ == "__main__":
    unittest.main()
