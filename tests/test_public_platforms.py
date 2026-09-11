import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.adapters import AdapterError, CdtAdapter, ChngAdapter, NoticeData, make_adapter
from backend.config import settings
from backend.db import get_db, init_db
from backend.public_platforms import ChdtpAdapter, ChnenergyAdapter, CgnAdapter, NeepAdapter, YfbAdapter, notice_type
from backend.service import find_site, ingest_notice_data, reparse_notice


class PublicPlatformTests(unittest.TestCase):
    def adapter(self, cls, handler):
        with patch('backend.adapters.detect_outbound_proxy', return_value=None):
            adapter = cls('https://example.com/')
        adapter.client.close()
        adapter.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(adapter.close)
        return adapter

    def test_yfb_pagination_duplicates_and_excluded_ids(self):
        pages = []
        def handler(request):
            pages.append(request.url.params['pageNo'])
            return httpx.Response(200, text='<table>' + ''.join(
                f'<tr><td><a href="/inviteBid/detail/20260908_{key}.html">设备招标公告</a></td><td>2026-09-08</td></tr>'
                for key in ('1', '1', '2')) + '</table>')
        adapter = self.adapter(YfbAdapter, handler)
        rows = adapter.list_notices(50, 100, exclude_external_ids={'1'})
        self.assertEqual([row.external_id for row in rows], ['2'])
        self.assertEqual(pages, ['1', '2'])
        self.assertEqual(rows[0].published_at, '2026-09-08')

    @patch('backend.manual_verification.browser_request')
    def test_yfb_verified_enterprise_uses_browser_and_new_search_api(self, browser_request):
        browser_request.side_effect = [
            {'code': 200, 'data': {'accountType': 2, 'endTime': '2030-07-11'}},
            {'code': 200, 'data': {'resultList': [
            {
                'contentId': 629767947, 'title': '建设项目招标计划',
                'type': '招标计划', 'updateTime': '2026-09-10 10:47:59',
            },
            {
                'contentId': 629767931, 'areaId': '137',
                'title': '高效永磁同步电机招标公告',
                'type': '招标公告', 'updateTime': '2026-09-10 10:47:57',
            },
        ]}},
        ]
        with patch('backend.adapters.detect_outbound_proxy', return_value=None):
            adapter = YfbAdapter(
                'https://www.yfbzb.com/search/?keywords=设备',
                session_cookie='__scout_browser_session=47; __scout_browser_port=62430',
            )
        adapter.client.close()
        adapter.client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, text='<html>登录</html>')
        ))
        self.addCleanup(adapter.close)

        rows = adapter.list_notices(1, 1)

        requested_url = browser_request.call_args_list[1].args[1]
        self.assertIn('qiye.qianlima.com/new_qd_yfbsite/api/search?', requested_url)
        self.assertIn('pageSize=30', requested_url)
        self.assertEqual(browser_request.call_args_list[1].kwargs['storage_query'], {'openid': 'YFB-OpenId'})
        self.assertIn('/infoCenter/infoDetail/629767931/137/zhaobiao', rows[0].url)

    @patch('backend.manual_verification.browser_page_json')
    def test_yfb_member_detail_uses_enterprise_content_and_attachments(self, browser_page_json):
        browser_page_json.return_value = {'code': 200, 'data': {
            'contentId': 629767931, 'title': '<font>储能</font>设备招标公告', 'type': '招标公告',
            'content': '<p>会员完整正文，投标截止时间：2026-09-20 09:00</p>',
            'updateDate': '2026/09/10', 'downlinkList': [
                {'title': '技术规范.pdf', 'url': 'https://files.example.com/spec.pdf'},
                {'title': '招标文件.docx', 'url': 'http://www.qianlima.com/downloads/agent.jsp?req=signed'},
                {'title': '错误详情链接.pdf', 'url': 'https://qiye.qianlima.com//#/infoCenter/infoDetail/1/1/zhaobiao'},
            ],
        }}
        with patch('backend.adapters.detect_outbound_proxy', return_value=None):
            adapter = YfbAdapter(
                'https://www.yfbzb.com/inviteBid/',
                session_cookie='__scout_browser_session=47; __scout_browser_port=62430',
            )
        self.addCleanup(adapter.close)

        data = adapter.fetch_notice(
            'https://qiye.qianlima.com/new_qd_yfbsite/#/infoCenter/infoDetail/629767931/137/zhaobiao'
        )

        self.assertIn('会员完整正文', data.body_text)
        self.assertEqual(data.title, '储能设备招标公告')
        self.assertIsNone(data.collection_warning)
        self.assertEqual(data.attachments[0].name, '技术规范.pdf')
        self.assertEqual(len(data.attachments), 2)
        self.assertEqual(data.attachments[1].url, 'https://www.qianlima.com/downloads/agent.jsp?req=signed')
        self.assertEqual('/subZhaobiao/zbDetail', browser_page_json.call_args.args[2])

    @patch('backend.manual_verification.browser_page_json')
    def test_yfb_member_detail_never_silently_falls_back_to_public(self, browser_page_json):
        browser_page_json.return_value = {'code': 200, 'data': {'errType': 6}}
        with patch('backend.adapters.detect_outbound_proxy', return_value=None):
            adapter = YfbAdapter(
                'https://www.yfbzb.com/inviteBid/',
                session_cookie='__scout_browser_session=47; __scout_browser_port=62430',
            )
        self.addCleanup(adapter.close)

        with self.assertRaisesRegex(AdapterError, '会员详情未返回完整正文'):
            adapter.fetch_notice(
                'https://qiye.qianlima.com/new_qd_yfbsite/#/infoCenter/infoDetail/'
                '629767931/137/zhaobiao?fromPage=searchPage&published=20260910'
            )

    @patch('backend.manual_verification.browser_request')
    def test_yfb_expired_membership_automatically_uses_public_list(self, browser_request):
        browser_request.return_value = {
            'code': 200, 'data': {'accountType': 2, 'endTime': '2020-01-01'},
        }
        raw = '<tr><td><a href="/inviteBid/detail/20260910_123.html">设备招标公告</a></td><td>2026-09-10</td></tr>'
        with patch('backend.adapters.detect_outbound_proxy', return_value=None):
            adapter = YfbAdapter(
                'https://www.yfbzb.com/inviteBid/',
                session_cookie='__scout_browser_session=47; __scout_browser_port=62430',
            )
        adapter.client.close()
        adapter.client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, text=raw)
        ))
        self.addCleanup(adapter.close)

        rows = adapter.list_notices(1, 1)

        self.assertEqual(rows[0].url, 'https://www.yfbzb.com/inviteBid/detail/20260910_123.html')

    def test_yfb_ignores_sidebar_and_keeps_hidden_warning(self):
        adapter = self.adapter(YfbAdapter, lambda _: httpx.Response(200, text='<h1>设备招标公告</h1><div class="content">公开正文<br/>***<a href="/a.pdf">技术文件.pdf</a></div><aside>储能广告</aside>'))
        data = adapter.fetch_notice('https://www.yfbzb.com/inviteBid/detail/20260908_123.html')
        self.assertNotIn('储能广告', data.body_text)
        self.assertTrue(data.collection_warning)
        self.assertEqual(data.attachments[0].name, '技术文件.pdf')

    def test_yfb_member_page_routes_are_not_downloadable_attachments(self):
        raw = '''<html><h1>设备公开招标公告</h1><div class="content">公开正文
        <a href="https://qiye.qianlima.com//#/infoCenter/infoDetail/1/1/zhaobiao">附件.pdf</a>
        <a href="http://www.qianlima.com/downloads/agent.jsp?req=">招标附件.zip</a></div></html>'''
        adapter = self.adapter(YfbAdapter, lambda _: httpx.Response(200, text=raw))
        data = adapter.fetch_notice('https://www.yfbzb.com/inviteBid/detail/20260910_123.html')
        self.assertEqual(data.attachments, [])

    def test_yfb_does_not_misclassify_opening_records_or_document_publications(self):
        for title, expected in [('工程项目开标记录', '开标记录'), ('工程施工招标文件', '招标文件'), ('设备招标终止公告', '终止公告')]:
            self.assertEqual(notice_type(title), expected)
        self.assertEqual(notice_type('设备招标公告[变更公告]'), '招标公告')

    def test_yfb_skips_document_rows_before_candidate_limit(self):
        def handler(_):
            return httpx.Response(200, text=''.join(f'<tr><td><a href="/inviteBid/detail/20260908_{key}.html">{title}</a></td><td>2026-09-08</td></tr>' for key, title in [('1', '工程开标记录'), ('2', '施工招标文件'), ('3', '设备招标公告')]))
        rows = self.adapter(YfbAdapter, handler).list_notices(1, 1)
        self.assertEqual([row.external_id for row in rows], ['3'])

    def test_yfb_checks_actual_procurement_method_in_body(self):
        adapter = self.adapter(YfbAdapter, lambda _: httpx.Response(200, text='<h1>村部采购监控设备</h1><div class="content"><p>采购方式</p><p>动态报价</p><p>根据招标与网络采购管理办法进行采购</p></div>'))
        data = adapter.fetch_notice('https://www.yfbzb.com/inviteBid/detail/20260908_123.html')
        self.assertEqual(data.notice_type, '采购公告')
        self.assertEqual(notice_type('小学健康项目比选公告'), '采购公告')

    def test_verified_legacy_platforms_stop_repeated_pages(self):
        for cls in (CdtAdapter,):
            with self.subTest(platform=cls.code):
                calls = []
                def handler(request):
                    calls.append(request.url.path)
                    return httpx.Response(200, json={'data': [{'id': str(i), 'message_title': '设备招标公告', 'publish_time': '2026-09-08'} for i in range(3)]})
                rows = self.adapter(cls, handler).list_notices(50, 3, exclude_external_ids={'0', '1', '2'})
                self.assertEqual(rows, [])
                self.assertEqual(len(calls), 2)

    def test_chng_accepts_new_hash_detail_links(self):
        adapter = self.adapter(ChngAdapter, lambda request: httpx.Response(200, json={
            'root': [{'announcementId': 12861658, 'announcementTitle': '设备招标公告', 'createtime': 1788883200000}]
        }))
        rows = adapter.list_notices(1, 10)
        self.assertEqual(rows[0].external_id, '12861658')
        self.assertEqual(rows[0].published_at, '2026-09-09 00:00:00')

    def test_chng_detects_script_challenge_even_with_200_status(self):
        adapter = self.adapter(ChngAdapter, lambda _: httpx.Response(
            200,
            text='<meta id="challenge"><script>window.$_ts={}</script>',
        ))
        with self.assertRaisesRegex(AdapterError, '专用浏览器'):
            adapter.list_notices(1, 10)

    def test_chng_fetches_new_json_detail(self):
        def handler(request):
            return httpx.Response(200, json={'data': {'announcement': {
                'announcementTitle': '华能设备招标公告',
                'announcementHtml': '<p>发布时间：2026-09-09</p><p>投标截止时间：2026-09-20</p>',
            }}})
        data = self.adapter(ChngAdapter, handler).fetch_notice('https://ec.chng.com.cn/channel/home/#/detail?id=12861658')
        self.assertEqual(data.external_id, '12861658')
        self.assertEqual(data.title, '华能设备招标公告')
        self.assertEqual(data.published_at, '2026-09-09')

    def test_neep_duplicate_date_and_missing_attachment(self):
        adapter = self.adapter(NeepAdapter, lambda _: httpx.Response(200, text='<div class="right-main-content"><span>2026-09-08 12:00:00</span><div>项目名称：</div><div>电池询价采购</div><div>发布时间：</div><span>2026-09-08 12:00:00</span><div>报价截止时间：</div><span>2026-09-13 11:00:00</span><div>附件：</div><div class="content">有</div></div>'))
        data = adapter.fetch_notice('https://gd-prod.oss-cn-beijing.aliyuncs.com/upload/cms/article/inquireOne/123.html')
        self.assertEqual(data.published_at, '2026-09-08 12:00:00')
        self.assertEqual(data.opening_at, '2026-09-13 11:00:00')
        self.assertEqual(data.notice_type, '采购公告')
        self.assertTrue(data.collection_warning)

    def test_neep_missing_publication_never_uses_deadline(self):
        adapter = self.adapter(NeepAdapter, lambda _: httpx.Response(200, text='<div class="right-main-content"><p>项目名称：</p><p>询价采购</p><p>发布时间：</p><p>报价截止时间：</p><p>2026-09-13 11:00:00</p></div>'))
        self.assertIsNone(adapter.fetch_notice('https://gd-prod.oss-cn-beijing.aliyuncs.com/upload/cms/article/inquireOne/123.html').published_at)

    def test_neep_jsonp_pages_and_candidate_cap(self):
        calls = []
        def handler(request):
            page = int(request.url.params['pageNo'])
            calls.append(page)
            rows = [{'articleId': page * 10 + n, 'inquireName': '询价采购', 'publishTimeString': '2026-09-08 12:00:00', 'articleUrl': f'https://gd-prod.oss-cn-beijing.aliyuncs.com/upload/cms/article/inquireOne/{page * 10 + n}.html'} for n in range(2)]
            return httpx.Response(200, text='cb(' + json.dumps({'respCode': '0000', 'data': {'rows': rows}}) + ')')
        adapter = self.adapter(NeepAdapter, handler)
        self.assertEqual(len(adapter.list_notices(50, 3)), 3)
        self.assertEqual(calls, [1, 2])

    def test_cgn_dynamic_config_and_page_eleven(self):
        calls = []
        key, store, component = 'a' * 32, 'b' * 32, 'c' * 32
        def handler(request):
            calls.append(request)
            if request.url.path.endswith('zbgg.html'):
                struct = {'children': [{'id': component, 'option': {'partType': 'page', 'data': {'dataId': store, 'pageSize': 1}}}]}
                return httpx.Response(200, text=f"window.struct=data.struct; window.pageId='{key}'; window.struct=" + json.dumps(struct) + ';')
            number = json.loads(request.content)['pageIndex'] if request.method == 'POST' else int(request.url.path.rsplit('/', 1)[1].split('.')[0])
            return httpx.Response(200, json={'total': 100, 'list': [{'Id': f'{number:032x}', 'Title': '服务器招标公告', 'IssueTime': '2026-09-08 12:00:00'}]})
        adapter = self.adapter(CgnAdapter, handler)
        rows = adapter.list_notices(11, 20)
        self.assertEqual(len(rows), 11)
        self.assertIn('Details.html?dataId=', rows[0].url)
        self.assertEqual(calls[-1].method, 'POST')

    def test_cgn_attachment_json_strings_deduplicate(self):
        attachment = {'name': '公告.pdf', 'url': '/ecpmanage/AttachmentController.do?method=downLoadCmsFile&objId=1'}
        adapter = self.adapter(CgnAdapter, lambda _: httpx.Response(200, json={'Title': '设备招标公告', 'Body': '<p>技术要求：在线监测</p>', 'Attachment': json.dumps([attachment]), 'BodyAttachment': json.dumps([attachment]), 'IssueTime': '2026-09-08'}))
        data = adapter.fetch_notice(f'https://ecp.cgnpc.com.cn/Details.html?dataId={"a" * 32}&detailId={"b" * 32}')
        self.assertEqual(len(data.attachments), 1)
        self.assertIn('在线监测', data.body_text)

    def test_restricted_platforms_fail_explicitly(self):
        for code in ('ceb', 'espic'):
            with patch('backend.adapters.detect_outbound_proxy', return_value=None):
                adapter = make_adapter(code, 'https://example.com/')
            try:
                with self.assertRaises(AdapterError):
                    adapter.list_notices()
                with self.assertRaises(AdapterError):
                    adapter.fetch_notice('https://example.com/')
            finally:
                adapter.close()
        self.assertEqual(notice_type('设备招标公告[变更公告]'), '招标公告')
        self.assertEqual(notice_type('设备询价采购公告'), '采购公告')

    def test_partial_unmatched_survives_reparse_and_trash(self):
        original = settings.data_dir
        with tempfile.TemporaryDirectory() as folder:
            settings.data_dir = Path(folder)
            try:
                init_db()
                init_db()
                with get_db() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM sites WHERE code IN ('yfb','chnenergy','cgn')").fetchone()[0], 3)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM sites WHERE code IN ('ceb','espic')").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM sites WHERE code='neep'").fetchone()[0], 0)
                    self.assertEqual(find_site(db, 'https://www.chnenergybidding.com.cn/bidweb/')['code'], 'chnenergy')
                    self.assertIsNone(find_site(db, 'https://other-bucket.aliyuncs.com/1.html'))
                    site = db.execute("SELECT id FROM sites WHERE code='chnenergy'").fetchone()[0]
                    data = NoticeData('1', '普通办公采购', 'https://www.chnenergybidding.com.cn/bidweb/1', '无命中正文', collection_warning='附件未公开')
                    notice_id = ingest_notice_data(db, site, data, source_type='crawl', filter_unmatched=True)
                    self.assertIsNotNone(notice_id)
                    self.assertEqual(db.execute('SELECT ingest_status FROM notices WHERE id=?', (notice_id,)).fetchone()[0], 'partial')
                reparse_notice(notice_id)
                with get_db() as db:
                    self.assertEqual(db.execute('SELECT ingest_status FROM notices WHERE id=?', (notice_id,)).fetchone()[0], 'partial')
                    db.execute("UPDATE notices SET deleted_at='2026-09-08' WHERE id=?", (notice_id,))
                    self.assertIsNone(ingest_notice_data(db, site, data, source_type='crawl', filter_unmatched=True))
                    self.assertIsNotNone(db.execute('SELECT deleted_at FROM notices WHERE id=?', (notice_id,)).fetchone()[0])
            finally:
                settings.data_dir = original


class ChnenergyTests(unittest.TestCase):
    adapter = PublicPlatformTests.adapter

    def test_tender_only_pagination_and_trash_exclusions(self):
        calls = []
        def handler(request):
            calls.append(request.url.path)
            # Two links per row: the project code must never become its title.
            rows = ''.join(f'<li><a href="/bidweb/001/{category}/{category}001/20260908/{key}.html">CEZB260000001</a><a href="/bidweb/001/{category}/{category}001/20260908/{key}.html">设备公告</a><span>2026-09-08</span></li>' for category, key in [('001002', 'a' * 36), ('001002', 'b' * 36), ('001003', 'c' * 36), ('001006', 'd' * 36)])
            return httpx.Response(200, text=rows + '<a href="/bidweb/001/001002/2.html">下页 &gt;</a>')
        adapter = self.adapter(ChnenergyAdapter, handler)
        rows = adapter.list_notices(50, 100, exclude_external_ids={'a' * 36})
        self.assertEqual([row.external_id for row in rows], ['b' * 36])
        self.assertEqual(rows[0].title, '设备公告')
        self.assertEqual(rows[0].notice_type, '招标公告')
        self.assertEqual(len(calls), 2)

    def test_detail_dates_and_public_attachment_sibling(self):
        raw = '''<aside>推荐储能公告</aside><div class="article"><div class="article-info"><h1>设备招标公告</h1><p class="info-sources">【发布时间：2026-09-08 10:00:00】</p><div class="con"><p>技术要求：在线监测</p><p>开标时间：2026年9月20日09时00分</p></div></div><div class="con attach"><a href="/bidweb/uploadfile/spec.pdf">技术要求.pdf</a></div></div>'''
        adapter = self.adapter(ChnenergyAdapter, lambda _: httpx.Response(200, text=raw))
        data = adapter.fetch_notice(ChnenergyAdapter.ROOT + '/bidweb/001/001002/001002001/20260908/' + 'a' * 36 + '.html')
        self.assertEqual(data.published_at, '2026-09-08 10:00:00')
        self.assertEqual(data.opening_at, '2026-09-20 09:00:00')
        self.assertNotIn('推荐储能', data.body_text)
        self.assertEqual(len(data.attachments), 1)
        self.assertEqual(data.attachments[0].name, '技术要求.pdf')

    def test_non_tender_import_is_rejected_without_request(self):
        def handler(_):
            self.fail('non-tender URLs must not be fetched')
        adapter = self.adapter(ChnenergyAdapter, handler)
        for category in ('001001', '001003', '001005', '001006', '001007'):
            with self.assertRaises(AdapterError):
                adapter.fetch_notice(ChnenergyAdapter.ROOT + f'/bidweb/001/{category}/{category}001/20260908/' + 'a' * 36 + '.html')


class ChdtpTests(unittest.TestCase):
    adapter = PublicPlatformTests.adapter

    def test_verified_session_uses_browser_and_current_tender_page(self):
        blocked = "<script src='/45i5xfip730ih5uh/sgodapt2y.js'></script>"
        adapter = self.adapter(ChdtpAdapter, lambda _: httpx.Response(412, text=blocked))
        adapter.browser_site_id = 55
        adapter.browser_port = 43210
        list_html = '''<a href="javascript:toGetContent('zhaobiaogg/2026/09/10/zhaobiaogg_3914671_39484.html')"
            title="新能源设备招标公告">新能源设备招标...</a><span>[2026-09-10]</span>'''
        detail_html = '<html><h1>新能源设备招标公告</h1><p>发布时间：2026-09-10</p></html>'
        with patch('backend.manual_verification.browser_request', side_effect=[list_html, detail_html]) as request:
            rows = adapter.list_notices(1, 1)
            data = adapter.fetch_notice(rows[0].url, rows[0].external_id)
        self.assertEqual(data.title, '新能源设备招标公告')
        self.assertEqual(
            request.call_args_list[0].args[:2],
            (55, 'https://www.chdtp.com/webs/queryWebZbgg.action?zbggType=1'),
        )
        self.assertEqual(request.call_args_list[0].kwargs['port'], 43210)
        self.assertFalse(request.call_args_list[0].kwargs['parse_json'])

    def test_security_challenge_is_never_parsed_as_notice(self):
        blocked = "<script src='/45i5xfip730ih5uh/sgodapt2y.js'></script><script l='d'>challenge</script>"
        adapter = self.adapter(ChdtpAdapter, lambda _: httpx.Response(412, text=blocked))
        with self.assertRaisesRegex(AdapterError, '安全验证'):
            adapter.list_notices()
        with self.assertRaisesRegex(AdapterError, '安全验证'):
            adapter.fetch_notice('https://www.chdtp.com/pages/detail.jsp?id=abc')

    def test_tender_list_paginates_deduplicates_and_respects_trash(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text='''
              <a href="/pages/detail.jsp?id=old">旧项目招标公告</a><span>2026-09-09</span>
              <a href="/pages/detail.jsp?id=new">新能源设备招标公告</a><span>2026-09-09</span>
              <a href="/pages/detail.jsp?id=buy">谈判采购公告</a>
              <a href="/staticPage/zxzbggJT_2.html">下一页</a>''')
        adapter = self.adapter(ChdtpAdapter, handler)
        rows = adapter.list_notices(50, 10, exclude_external_ids={'old'})
        self.assertEqual([row.external_id for row in rows], ['new'])
        self.assertEqual(rows[0].notice_type, '招标公告')
        self.assertEqual(len(calls), 2)

    def test_detail_reads_dates_and_attachments(self):
        raw = '''<html><head><title>中国华电</title></head><body><h1>新能源设备招标公告</h1>
        <p>发布时间：2026年9月9日 08:30</p><p>投标截止时间：2026-09-20 09:00</p>
        <p>项目内容：在线监测设备</p><a href="/files/spec.pdf">技术规范.pdf</a></body></html>'''
        adapter = self.adapter(ChdtpAdapter, lambda _: httpx.Response(200, text=raw))
        data = adapter.fetch_notice('https://www.chdtp.com/pages/detail.jsp?id=abc')
        self.assertEqual(data.external_id, 'abc')
        self.assertEqual(data.published_at, '2026-09-09 08:30:00')
        self.assertEqual(data.opening_at, '2026-09-20 09:00:00')
        self.assertEqual(data.attachments[0].name, '技术规范.pdf')

    def test_non_official_detail_is_rejected_before_request(self):
        adapter = self.adapter(ChdtpAdapter, lambda _: self.fail('must not request foreign host'))
        with self.assertRaisesRegex(AdapterError, '官方'):
            adapter.fetch_notice('https://example.com/detail.jsp?id=abc')


if __name__ == '__main__':
    unittest.main()
