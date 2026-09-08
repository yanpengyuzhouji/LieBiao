import json
import unittest

from backend.matching import match_sources, occurrences


class MatchingRegressionTests(unittest.TestCase):
    def test_offsets_survive_whitespace_and_case_expansion(self):
        text = '前文\n\n\n   Straße\t\t服务器采购，后文'
        hits = occurrences(text, '服务器', 'body', '', '', 'include_any')
        self.assertEqual(len(hits), 1)
        self.assertIn('服务器', hits[0].snippet)
        self.assertTrue(hits[0].context_before.endswith('Straße\t\t'))
        self.assertTrue(hits[0].context_after.startswith('采购'))

    def test_required_term_accepts_synonym_alternative(self):
        group = {
            'include_all_json': json.dumps(['储能变流器', '采购']),
            'synonyms_json': json.dumps({'储能变流器': ['PCS']}),
        }
        sources = [{'source_type': 'body', 'text': '本次采购 PCS 设备'}]
        self.assertTrue(match_sources(group, sources))
        self.assertFalse(match_sources(group, [{'source_type': 'body', 'text': 'PCS 设备'}]))

    def test_empty_positive_rule_does_not_match_exclusion_only(self):
        self.assertEqual(match_sources({'exclude_json': '["培训"]'}, [{'source_type': 'body', 'text': '培训'}]), [])

    def test_incidental_attachment_exclusion_does_not_veto_positive_hit(self):
        group = {
            'include_any_json': '["开关柜"]',
            'exclude_json': '["培训"]',
            'scopes_json': '["title", "body", "attachment_body"]',
        }
        sources = [
            {'source_type': 'attachment_body', 'text': '开关柜技术参数以及人员培训要求'},
            {'source_type': 'body', 'text': '设备材料公开招标公告'},
        ]
        hits = match_sources(group, sources)
        self.assertTrue(any(hit.keyword == '开关柜' and not hit.is_negative for hit in hits))
        self.assertFalse(any(hit.is_negative for hit in hits))

    def test_body_exclusion_still_vetoes_notice(self):
        group = {
            'include_any_json': '["开关柜"]',
            'exclude_json': '["培训"]',
            'scopes_json': '["body", "attachment_body"]',
        }
        hits = match_sources(group, [
            {'source_type': 'body', 'text': '开关柜培训项目'},
            {'source_type': 'attachment_body', 'text': '开关柜技术参数'},
        ])
        self.assertTrue(any(hit.is_negative for hit in hits))

    def test_full_width_evidence_keeps_original_text(self):
        hit = occurrences('购置 ＰＣＳ 设备', 'pcs', 'body', '', '', 'include_any')[0]
        self.assertIn('ＰＣＳ', hit.snippet)
        self.assertEqual(hit.context_before, '购置 ')
        self.assertEqual(hit.context_after, ' 设备')
