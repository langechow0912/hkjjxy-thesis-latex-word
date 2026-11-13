# -*- coding: utf-8 -*-
"""失败自动退积分、记录失败原因、文本改写同一段原文首次全价后 3 次半价。全部离线。"""
import json
from unittest.mock import patch

from core import points, service_billing as billing, tabword
from tests.test_points import PointsBase, make_user

TEXT = '这是一段需要降低 AI 率的论文正文，用来测试半价重改。' * 40


class RewriteBase(PointsBase):
    def quote(self, text=TEXT, preset='p1', operation='paraphrase_stream'):
        values = {'text': text, 'preset': preset}
        res = self.client.post('/api/tabword/billing/quote',
                               data={'operation': operation, 'params': json.dumps(values, ensure_ascii=False)})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json(), values

    def pay(self, quote):
        res = self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def submit(self, operation, values, order_id, upstream):
        target = 'stream_call' if tabword.OPERATIONS[operation].stream else 'call'
        with patch.object(tabword, target, **upstream):
            return self.client.post('/api/tabword/' + operation, data={
                'params': json.dumps(values, ensure_ascii=False), 'submit': 'true', 'payment_order': order_id})


def ok_stream():
    # 迭代器只能读一次，每个用例都要现造一个
    return {'return_value': iter([b'event: progress\ndata: {"original":"a","rewritten":"b"}\n\n',
                                  b'event: done\ndata: {"paraphrased_text":"b"}\n\n'])}


class FailureRefund(RewriteBase):
    def test_stream_error_refunds_points_and_records_reason(self):
        user = make_user(500); self.as_user(user)
        quote, values = self.quote()
        paid = self.pay(quote)
        before = points.balance(user['id'])
        broken = {'return_value': iter([b'event: progress\ndata: {"original":"a","rewritten":"b"}\n\n',
                                        b'event: error\ndata: {"message":"upstream overloaded"}\n\n'])}
        res = self.submit('paraphrase_stream', values, paid['order_id'], broken)
        self.assertEqual(res.status_code, 200)
        self.assertIn('event: refund', res.text)
        self.assertIn('积分已全额退还', res.text)
        self.assertEqual(points.balance(user['id']), before + quote['amount_cents'] / 10)
        stored = billing.get_quote(quote['quote_id'])
        self.assertEqual(stored['state'], 'failed')
        self.assertIn('upstream overloaded', stored['error'])
        self.assertIn('已收到 1 段', stored['error'])

    def test_stream_without_done_is_refunded_with_reason(self):
        user = make_user(500); self.as_user(user)
        quote, values = self.quote()
        paid = self.pay(quote)
        res = self.submit('paraphrase_stream', values, paid['order_id'],
                       {'return_value': iter([b'event: progress\ndata: {}\n\n'])})
        self.assertIn('event: refund', res.text)
        self.assertIn('没有 done', billing.get_quote(quote['quote_id'])['error'])
        self.assertEqual(points.balance(user['id']), 500)

    def test_connection_failure_refunds_and_says_so(self):
        user = make_user(500); self.as_user(user)
        quote, values = self.quote()
        paid = self.pay(quote)
        res = self.submit('paraphrase_stream', values, paid['order_id'],
                       {'side_effect': tabword.TabwordError('服务商连接中断，提交状态可能未确认；未自动重试', 502)})
        self.assertEqual(res.status_code, 502)
        self.assertIn('积分已全额退还', res.json()['detail'])
        self.assertIn('服务商连接中断', billing.get_quote(quote['quote_id'])['error'])
        self.assertEqual(points.balance(user['id']), 500)

    def test_refund_happens_only_once(self):
        user = make_user(500); self.as_user(user)
        quote, _ = self.quote()
        self.pay(quote)
        self.assertIsNotNone(points.refund_spend(quote['quote_id']))
        self.assertIsNone(points.refund_spend(quote['quote_id']))
        self.assertEqual(points.balance(user['id']), 500)

    def test_success_is_not_refunded(self):
        user = make_user(500); self.as_user(user)
        quote, values = self.quote()
        paid = self.pay(quote)
        res = self.submit('paraphrase_stream', values, paid['order_id'], ok_stream())
        self.assertNotIn('event: refund', res.text)
        self.assertEqual(billing.get_quote(quote['quote_id'])['state'], 'complete')
        self.assertEqual(points.balance(user['id']), 500 - quote['amount_cents'] / 10)

    def test_same_payment_still_cannot_be_submitted_twice(self):
        user = make_user(500); self.as_user(user)
        quote, values = self.quote()
        paid = self.pay(quote)
        self.submit('paraphrase_stream', values, paid['order_id'], {'side_effect': tabword.TabwordError('x', 502)})
        again = self.submit('paraphrase_stream', values, paid['order_id'], ok_stream())
        self.assertEqual(again.status_code, 409)


class HalfPriceRewrites(RewriteBase):
    def test_first_full_then_three_half_price_then_full(self):
        user = make_user(5000); self.as_user(user)
        first, _ = self.quote()
        self.assertFalse(first['discounted'])
        self.assertEqual(first['rewrite_discounts'], 3)
        self.pay(first)
        full = first['amount_cents']
        for left in (2, 1, 0):
            q, _ = self.quote()
            self.assertTrue(q['discounted'])
            self.assertEqual(q['amount_cents'], (full + 1) // 2)
            self.assertEqual(q['discounts_left'], left)
            self.pay(q)
        fifth, _ = self.quote()
        self.assertFalse(fifth['discounted'])
        self.assertEqual(fifth['amount_cents'], full)

    def test_discount_needs_a_paid_first_rewrite(self):
        user = make_user(5000); self.as_user(user)
        self.quote()                          # 只报价没付款
        second, _ = self.quote()
        self.assertFalse(second['discounted'])

    def test_unpaid_discounted_quotes_do_not_use_up_the_discount(self):
        user = make_user(5000); self.as_user(user)
        self.pay(self.quote()[0])
        for _ in range(5):                    # 反复报价但不付款
            self.assertTrue(self.quote()[0]['discounted'])

    def test_failed_first_rewrite_still_grants_half_price(self):
        user = make_user(5000); self.as_user(user)
        first, values = self.quote()
        paid = self.pay(first)
        self.submit('paraphrase_stream', values, paid['order_id'], {'side_effect': tabword.TabwordError('x', 502)})
        self.assertTrue(self.quote()[0]['discounted'])

    def test_different_preset_is_the_same_text_but_different_text_is_full_price(self):
        user = make_user(5000); self.as_user(user)
        self.pay(self.quote()[0])
        self.assertTrue(self.quote(preset='another')[0]['discounted'])
        self.assertTrue(self.quote(text=TEXT.replace('。', '。\n  '))[0]['discounted'])   # 只差空白
        self.assertFalse(self.quote(text=TEXT + '新的一句。')[0]['discounted'])

    def test_discount_is_per_user(self):
        alice, bob = make_user(5000), make_user(5000)
        self.as_user(alice); self.pay(self.quote()[0])
        self.as_user(bob)
        self.assertFalse(self.quote()[0]['discounted'])

    def test_file_rewrite_and_detection_get_no_discount(self):
        user = make_user(5000); self.as_user(user)
        self.pay(self.quote()[0])
        res = self.client.post('/api/tabword/billing/quote', data={
            'operation': 'detect_text', 'params': json.dumps({'text': TEXT, 'task_platform': 'test'}, ensure_ascii=False)})
        self.assertEqual(res.status_code, 200, res.text)
        detect = res.json()
        self.assertFalse(detect['discounted'])
        self.assertEqual(detect['rewrite_discounts'], 0)

    def test_discount_expires_after_the_window(self):
        user = make_user(5000); self.as_user(user)
        self.pay(self.quote()[0])
        import time
        later = time.time() + billing.REWRITE_DISCOUNT_WINDOW + 60
        with patch('core.service_billing.time.time', return_value=later):
            self.assertFalse(billing.rewrite_offer(user['id'], {'text': TEXT})['discounts_left'])


if __name__ == '__main__':
    import unittest
    unittest.main()
