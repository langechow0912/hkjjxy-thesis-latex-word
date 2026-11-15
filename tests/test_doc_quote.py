# -*- coding: utf-8 -*-
"""降 AI（文档改写）支持 .doc：二进制快速解析字数报价，读不懂也不失败、宁可少收，并在报价里说明有误差。离线。"""
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import olefile
from docx import Document

from core import doc_binary, points, service_billing as billing, tabword
from tests.test_points import PointsBase, make_user

FIXTURES = Path(__file__).parent / 'fixtures' / 'doc'
EXPECTED = json.loads((FIXTURES / 'expected.json').read_text(encoding='utf-8'))   # 用 .docx 的数法数出来的标准答案


def fixture(name):
    return (FIXTURES / (name + '.doc')).read_bytes()


def docx_bytes(text='这是一份真正的 docx 文档，用来核对扩展名写错时的处理。'):
    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph(text)
    doc.save(buf)
    return buf.getvalue()


def with_encrypted_flag(data):
    """把 FIB 里的"已加密"位打开（同样大小的流，原地改写），模拟读不了正文的 .doc。"""
    ole = olefile.OleFileIO(io.BytesIO(data), write_mode=True)
    word = bytearray(ole.openstream('WordDocument').read())
    word[0x0A] |= 0x01
    word[0x0B] |= 0x01
    ole.write_stream('WordDocument', bytes(word))
    out = ole.fp.getvalue()
    ole.close()
    return out


class BinaryParser(PointsBase):
    def test_counts_match_the_docx_method_exactly(self):
        for name in ('plain', 'long'):
            count, basis = doc_binary.count(fixture(name))
            self.assertEqual((count, basis), (EXPECTED[name], 'doc'), name)

    def test_field_instructions_and_control_characters_are_not_counted(self):
        raw = doc_binary.main_text(fixture('plain'))
        self.assertIn('PAGE', raw)                           # 域指令确实在正文流里…
        self.assertNotIn('PAGE', doc_binary.clean(raw))      # …但不算字数
        self.assertFalse(any(ord(c) < 0x20 for c in doc_binary.clean(raw)))

    def test_unreadable_documents_are_estimated_low_and_never_raise(self):
        data = fixture('long')
        for label, broken in (('encrypted', with_encrypted_flag(data)),
                              ('truncated', data[:len(data) // 2]),
                              ('not a doc at all', os.urandom(5000)),
                              ('rtf text', b'{\\rtf1\\ansi ' + b'x' * 3000 + b'}'),
                              ('empty', b'')):
            count, basis = doc_binary.count(broken)
            self.assertEqual(basis, 'estimate', label)
            self.assertGreaterEqual(count, doc_binary.FALLBACK_MIN, label)
            self.assertLessEqual(count, doc_binary.FALLBACK_MAX, label)

    def test_the_estimate_for_a_real_doc_never_exceeds_the_true_count(self):
        # 用户体验第一：估算只能偏低，宁可少收
        for name in ('plain', 'long'):
            count, basis = doc_binary.count(with_encrypted_flag(fixture(name)))
            self.assertEqual(basis, 'estimate')
            self.assertLessEqual(count, EXPECTED[name], name)

    def test_the_estimate_is_capped_for_huge_unreadable_files(self):
        self.assertEqual(doc_binary.estimate(b'\0' * (20 * 1024 * 1024)), doc_binary.FALLBACK_MAX)


class Pricing(PointsBase):
    def quote(self, name, data, filename=None):
        return self.client.post('/api/tabword/billing/quote', data={'operation': 'document_async', 'params': '{}'},
                                files={'file': (filename or name + '.doc', data)})

    def setUp(self):
        super().setUp()
        self.as_user(make_user(1000))

    def test_doc_quote_reports_the_count_the_basis_and_a_price(self):
        res = self.quote('plain', fixture('plain'))
        self.assertEqual(res.status_code, 200, res.text)
        quote = res.json()
        self.assertEqual((quote['characters'], quote['basis'], quote['approximate']), (EXPECTED['plain'], 'doc', True))
        self.assertEqual(quote['amount_cents'], -(-EXPECTED['plain'] * 100 // 1000))      # 每千字 1 元，向上取整到分
        self.assertEqual(quote['adjust_percent'], 0)

    def test_docx_quote_is_exact_and_not_marked_approximate(self):
        quote = self.quote('x', docx_bytes(), 'x.docx').json()
        self.assertEqual((quote['approximate'], quote['basis'], quote['adjust_percent']), (False, None, 0))

    def test_a_docx_saved_with_a_doc_extension_is_counted_as_docx(self):
        quote = self.quote('x', docx_bytes(), 'misnamed.doc').json()
        self.assertEqual((quote['approximate'], quote['basis']), (False, None))
        self.assertEqual(quote['characters'], billing.character_count({}, ('a.docx', docx_bytes())))

    def test_adjust_percent_changes_only_doc_quotes_and_follows_the_environment(self):
        base = EXPECTED['long']
        for percent, expected in ((0, -(-base * 100 // 1000)), (-10, -(-base * 100 * 90 // 100000)),
                                  (50, -(-base * 100 * 150 // 100000))):
            with patch.dict(os.environ, {'THESIS_DOC_PRICE_PERCENT': str(percent)}):
                quote = self.quote('long', fixture('long')).json()
                self.assertEqual((quote['amount_cents'], quote['adjust_percent']), (expected, percent), percent)
                docx = self.quote('x', docx_bytes(), 'x.docx').json()
                self.assertEqual(docx['adjust_percent'], 0)

    def test_percent_setting_is_clamped_and_bad_values_mean_zero(self):
        for raw, expected in (('', 0), ('abc', 0), ('-5', -5), ('-500', -90), ('900', 200), ('2.6', 3)):
            with patch.dict(os.environ, {'THESIS_DOC_PRICE_PERCENT': raw}):
                self.assertEqual(billing.doc_price_percent(), expected, raw)

    def test_an_unreadable_doc_still_gets_a_quote_instead_of_an_error(self):
        res = self.quote('x', with_encrypted_flag(fixture('long')))
        self.assertEqual(res.status_code, 200, res.text)
        quote = res.json()
        self.assertEqual((quote['basis'], quote['approximate']), ('estimate', True))
        self.assertGreaterEqual(quote['amount_cents'], 1)
        self.assertLessEqual(quote['characters'], EXPECTED['long'])                    # 偏低，不会多收

    def test_empty_and_unsupported_files_are_still_rejected_clearly(self):
        empty = self.quote('x', b'', 'x.doc')
        self.assertEqual(empty.status_code, 422)
        self.assertIn('空', empty.json()['detail'])
        self.assertEqual(self.quote('x', b'hello', 'x.txt').status_code, 422)      # 文档改写不收 .txt


class IndependentOfTheFormatWorker(PointsBase):
    """阿里云上降 AI 的计费（报价、付款、提交、退款）必须自己完成，不能依赖 AWS 排版服务器。"""

    def test_the_billing_modules_never_import_the_aws_client(self):
        import subprocess
        import sys
        code = ("import sys, core.service_billing, core.doc_binary, core.points, core.alipay, core.tabword, "
                "web.tabword_routes, web.points_routes\n"
                "sys.exit(1 if 'core.format_remote' in sys.modules else 0)")
        done = subprocess.run([sys.executable, '-c', code], cwd=str(Path(__file__).resolve().parents[1]),
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])

    def test_quote_pay_submit_and_refund_work_while_any_call_to_aws_would_blow_up(self):
        from core import format_remote
        user = make_user(1000); self.as_user(user)
        boom = AssertionError('降 AI 的计费流程不能调用 AWS 排版服务器')
        with patch.dict(os.environ, {'FORMAT_WORKER_URL': 'https://aws.invalid', 'FORMAT_WORKER_TOKEN': 'x'}), \
             patch.object(format_remote, '_call', side_effect=boom), patch.object(format_remote, 'healthy', side_effect=boom):
            data = fixture('plain')
            ok_quote = self.client.post('/api/tabword/billing/quote', data={'operation': 'document_async', 'params': '{}'},
                                        files={'file': ('a.doc', data)}).json()
            paid = self.client.post('/api/points/pay', json={'quote_id': ok_quote['quote_id']}).json()
            with patch.object(tabword, 'call', return_value={'task_id': 't1'}):
                res = self.client.post('/api/tabword/document_async', data={'params': '{}', 'submit': 'true', 'payment_order': paid['order_id']},
                                       files={'file': ('a.doc', data)})
            self.assertEqual(res.status_code, 200, res.text)
            # 提交失败也要在本机自己退款
            bad_quote = self.client.post('/api/tabword/billing/quote', data={'operation': 'document_async', 'params': '{}'},
                                         files={'file': ('b.doc', fixture('long'))}).json()
            bad_paid = self.client.post('/api/points/pay', json={'quote_id': bad_quote['quote_id']}).json()
            with patch.object(tabword, 'call', side_effect=tabword.TabwordError('服务商失败', 502)):
                res = self.client.post('/api/tabword/document_async', data={'params': '{}', 'submit': 'true', 'payment_order': bad_paid['order_id']},
                                       files={'file': ('b.doc', fixture('long'))})
            self.assertEqual(res.status_code, 502)
            self.assertIn('积分已全额退还', res.json()['detail'])
        self.assertEqual(points.balance(user['id']), 1000 - ok_quote['amount_cents'] / 10)


class PaidFlow(PointsBase):
    def test_the_original_doc_is_what_gets_submitted_after_paying(self):
        user = make_user(1000); self.as_user(user)
        data = fixture('plain')
        quote = self.client.post('/api/tabword/billing/quote', data={'operation': 'document_async', 'params': '{}'},
                                 files={'file': ('论文.doc', data)}).json()
        paid = self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).json()
        seen = {}

        def provider(name, params, file, **kwargs):
            seen.update(name=name, filename=file[0], same_bytes=file[1] == data)
            return {'task_id': 't-doc-1'}
        with patch.object(tabword, 'call', side_effect=provider):
            res = self.client.post('/api/tabword/document_async', data={'params': '{}', 'submit': 'true', 'payment_order': paid['order_id']},
                                   files={'file': ('论文.doc', data)})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(seen, {'name': 'document_async', 'filename': '论文.doc', 'same_bytes': True})
        self.assertEqual(billing.get_quote(quote['quote_id'])['task_id'], 't-doc-1')
        self.assertEqual(points.balance(user['id']), 1000 - quote['amount_cents'] / 10)

    def test_a_provider_failure_on_a_doc_is_refunded_as_usual(self):
        user = make_user(1000); self.as_user(user)
        data = fixture('long')
        quote = self.client.post('/api/tabword/billing/quote', data={'operation': 'document_async', 'params': '{}'},
                                 files={'file': ('a.doc', data)}).json()
        paid = self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).json()
        with patch.object(tabword, 'call', side_effect=tabword.TabwordError('服务商不支持该文档', 422)):
            res = self.client.post('/api/tabword/document_async', data={'params': '{}', 'submit': 'true', 'payment_order': paid['order_id']},
                                   files={'file': ('a.doc', data)})
        self.assertEqual(res.status_code, 422)
        self.assertIn('积分已全额退还', res.json()['detail'])
        self.assertEqual(points.balance(user['id']), 1000)
