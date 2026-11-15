# -*- coding: utf-8 -*-
"""检测接口响应体里 code != 0 当失败（退积分）；同时提交数有上限，满了不动订单。全部离线。"""
import io
import json
from unittest.mock import patch

from core import points, service_billing as billing, tabword
from tests.test_points import PointsBase, make_user
from tests.test_rewrite_refund import ok_stream


class FakeResponse(io.BytesIO):
    headers = {'Content-Type': 'application/json'}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def provider_reply(body):
    operation = tabword.OPERATIONS['detect_text']
    return patch.object(tabword, 'open_call',
                        return_value=(operation, FakeResponse(json.dumps(body, ensure_ascii=False).encode()), 'offline-only'))


class DetectBase(PointsBase):
    def buy(self, user):
        values = {'text': '论文正文' * 30, 'task_platform': 'p1'}
        quote = self.client.post('/api/tabword/billing/quote', data={
            'operation': 'detect_text', 'params': json.dumps(values, ensure_ascii=False)}).json()
        paid = self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).json()
        return quote, paid, values

    def submit(self, values, order_id, operation='detect_text'):
        return self.client.post('/api/tabword/' + operation, data={
            'params': json.dumps(values, ensure_ascii=False), 'submit': 'true', 'payment_order': order_id})


class ProviderBodyErrors(DetectBase):
    def test_nonzero_code_is_a_failure_with_refund_and_provider_reason(self):
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy(user)
        with provider_reply({'code': 500, 'message': '检测预设不存在: gzd_0510', 'data': {'score': 0}}):
            res = self.submit(values, paid['order_id'])
        self.assertEqual(res.status_code, 502)
        self.assertIn('检测预设不存在', res.json()['detail'])
        self.assertIn('积分已全额退还', res.json()['detail'])
        stored = billing.get_quote(quote['quote_id'])
        self.assertEqual(stored['state'], 'failed')
        self.assertIsNone(stored['result_json'])
        self.assertIn('检测预设不存在', stored['error'])
        self.assertEqual(points.balance(user['id']), 500)

    def test_code_zero_is_a_success(self):
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy(user)
        with provider_reply({'code': 0, 'message': 'success', 'data': {'score': 12.5}}):
            res = self.submit(values, paid['order_id'])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['data']['score'], 12.5)
        self.assertEqual(billing.get_quote(quote['quote_id'])['state'], 'complete')

    def test_reply_without_a_code_field_is_still_accepted(self):
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy(user)
        with provider_reply({'score': 3}):
            self.assertEqual(self.submit(values, paid['order_id']).status_code, 200)


class FailureContext(DetectBase):
    def test_failure_reason_names_the_platform_but_never_the_text(self):
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy(user)
        with provider_reply({'code': 500, 'message': '检测预设不存在: gzd_0510'}):
            self.submit(values, paid['order_id'])
        stored = billing.get_quote(quote['quote_id'])
        self.assertIn('平台 p1', stored['error'])
        self.assertIn('检测预设不存在', stored['error'])
        self.assertNotIn('论文正文', stored['error'])

    def test_timeout_reason_also_carries_the_context(self):
        import time
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy(user)
        with patch.dict('os.environ', {'TABWORD_DEADLINE_SECONDS': '1'}), \
             patch.object(tabword, 'call', side_effect=lambda *a, **k: time.sleep(1.5)):
            self.submit(values, paid['order_id'])
        self.assertIn('平台 p1', billing.get_quote(quote['quote_id'])['error'])

    def test_context_formats_platform_preset_and_file_size(self):
        from web.tabword_routes import _context
        self.assertEqual(_context({'task_platform': '格子达_zh'}, ('a.docx', b'x' * 5120)), '（平台 格子达_zh，文件 5 KB）')
        self.assertEqual(_context({'preset': 'zh_jc_1_0325', 'text': '不应出现的正文'}, None), '（预设 zh_jc_1_0325）')
        self.assertEqual(_context({}, None), '')


class ConcurrencyGate(DetectBase):
    def setUp(self):
        super().setUp()
        gate = billing.Gate(2)
        patcher = patch.object(billing, 'GATE', gate)
        patcher.start(); self.addCleanup(patcher.stop)
        self.gate = gate

    def test_gate_hands_out_limit_slots_then_refuses_and_recovers(self):
        a, b = self.gate.acquire(), self.gate.acquire()
        self.assertIsNone(self.gate.acquire())
        self.gate.release(a)
        self.assertIsNotNone(self.gate.acquire())
        self.assertEqual(self.gate.busy(), 2)
        self.gate.release(b)

    def test_stale_slots_expire(self):
        self.gate.lease = -1
        self.gate.acquire(); self.gate.acquire()
        self.assertIsNotNone(self.gate.acquire())

    def test_full_gate_rejects_fast_and_leaves_order_and_points_untouched(self):
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy(user)
        balance = points.balance(user['id'])
        self.gate.acquire(); self.gate.acquire()
        with provider_reply({'code': 0, 'message': 'success', 'data': {}}) as upstream:
            res = self.submit(values, paid['order_id'])
            self.assertEqual(res.status_code, 429)
            self.assertIn('不会重复扣费', res.json()['detail'])
            upstream.assert_not_called()
        self.assertEqual(billing.get_quote(quote['quote_id'])['state'], 'ready')
        self.assertEqual(points.balance(user['id']), balance)
        self.gate = billing.GATE
        for token in list(self.gate._held):
            self.gate.release(token)
        with provider_reply({'code': 0, 'message': 'success', 'data': {'score': 1}}):
            self.assertEqual(self.submit(values, paid['order_id']).status_code, 200)

    def test_slot_is_released_after_success_failure_and_stream(self):
        user = make_user(900); self.as_user(user)
        for body, status in (({'code': 0, 'message': 'success', 'data': {}}, 200),
                             ({'code': 500, 'message': 'boom'}, 502)):
            _, paid, values = self.buy(user)
            with provider_reply(body):
                self.assertEqual(self.submit(values, paid['order_id']).status_code, status)
            self.assertEqual(self.gate.busy(), 0)
        values = {'text': '论文正文' * 30, 'preset': 'p1'}
        quote = self.client.post('/api/tabword/billing/quote', data={
            'operation': 'paraphrase_stream', 'params': json.dumps(values, ensure_ascii=False)}).json()
        paid = self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).json()
        with patch.object(tabword, 'stream_call', **ok_stream()):
            res = self.submit(values, paid['order_id'], 'paraphrase_stream')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.gate.busy(), 0)

    def test_polling_an_async_task_does_not_use_a_slot(self):
        self.gate.acquire(); self.gate.acquire()
        self.assertNotIn('document_task', billing.SUBMISSIONS)


if __name__ == '__main__':
    import unittest
    unittest.main()
