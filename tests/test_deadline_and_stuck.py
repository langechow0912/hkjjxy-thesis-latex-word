# -*- coding: utf-8 -*-
"""总时长上限、卡死订单清理、耗时记录。全部离线。"""
import io
import json
import time
from contextlib import closing
from unittest.mock import patch

from core import alipay, points, service_billing as billing, tabword
from tests.test_points import PointsBase, make_user


class Base(PointsBase):
    def buy(self, operation='detect_text', values=None):
        values = values or {'text': '论文正文' * 30, 'task_platform': 'p1'}
        files = None
        if operation == 'document_async':            # 文件改写要带 Word 文件
            from docx import Document
            buf = io.BytesIO(); doc = Document(); doc.add_paragraph('文档改写测试正文。' * 40); doc.save(buf)
            files = {'file': ('a.docx', buf.getvalue())}
            values = {}
        quote = self.client.post('/api/tabword/billing/quote', data={
            'operation': operation, 'params': json.dumps(values, ensure_ascii=False)}, files=files).json()
        paid = self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).json()
        return quote, paid, values

    def submit(self, operation, values, order_id):
        return self.client.post('/api/tabword/' + operation, data={
            'params': json.dumps(values, ensure_ascii=False), 'submit': 'true', 'payment_order': order_id})

    def set_quote(self, quote_id, **fields):
        with closing(billing.connect()) as conn, conn:
            for name, value in fields.items():
                conn.execute('UPDATE service_quotes SET %s=? WHERE id=?' % name, (value, quote_id))


class Deadline(Base):
    def setUp(self):
        super().setUp()
        patcher = patch.object(billing, 'GATE', billing.Gate(3))
        patcher.start(); self.addCleanup(patcher.stop)
        self.gate = billing.GATE

    def test_slow_provider_is_abandoned_failed_and_refunded(self):
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy()

        def slow(*args, **kwargs):
            time.sleep(1.6)
            return {'code': 0, 'message': 'success', 'data': {'score': 5}}
        with patch.dict('os.environ', {'TABWORD_DEADLINE_SECONDS': '1'}), patch.object(tabword, 'call', side_effect=slow):
            began = time.time()
            res = self.submit('detect_text', values, paid['order_id'])
            self.assertLess(time.time() - began, 1.5)                 # 没有等满 1.6 秒的服务商
            self.assertEqual(res.status_code, 504)
            self.assertIn('没有返回结果', res.json()['detail'])
            self.assertIn('积分已全额退还', res.json()['detail'])
            self.assertEqual(self.gate.busy(), 1)                     # 服务商那边还占着，名额先不还
            for _ in range(40):
                if not self.gate.busy():
                    break
                time.sleep(0.1)
        self.assertEqual(self.gate.busy(), 0)                         # 工作线程结束后归还
        stored = billing.get_quote(quote['quote_id'])
        self.assertEqual(stored['state'], 'failed')                   # 迟到的结果不会把失败改回完成
        self.assertIsNone(stored['result_json'])
        self.assertEqual(points.balance(user['id']), 500)

    def test_fast_provider_is_unaffected(self):
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy()
        with patch.object(tabword, 'call', return_value={'code': 0, 'message': 'success', 'data': {'score': 7}}):
            res = self.submit('detect_text', values, paid['order_id'])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.gate.busy(), 0)

    def test_stream_over_the_total_limit_is_cut_off_with_error_and_refund(self):
        user = make_user(500); self.as_user(user)
        values = {'text': '论文正文' * 30, 'preset': 'p1'}
        quote, paid, values = self.buy('paraphrase_stream', values)

        def source():
            yield b'event: progress\ndata: {"original":"a","rewritten":"b"}\n\n'
            time.sleep(1.2)
            yield b'event: progress\ndata: {"original":"c","rewritten":"d"}\n\n'
            yield b'event: done\ndata: {}\n\n'
        with patch.dict('os.environ', {'TABWORD_STREAM_DEADLINE_SECONDS': '1'}), \
             patch.object(tabword, 'stream_call', return_value=source()):
            res = self.submit('paraphrase_stream', values, paid['order_id'])
        self.assertIn('event: error', res.text)
        self.assertIn('event: refund', res.text)
        stored = billing.get_quote(quote['quote_id'])
        self.assertEqual(stored['state'], 'failed')
        self.assertIn('超过 1 秒', stored['error'])
        self.assertEqual(points.balance(user['id']), 500)
        self.assertEqual(self.gate.busy(), 0)


class DeadlinesForTheCountdown(Base):
    def test_page_config_exposes_the_waiting_limits(self):
        config = self.client.get('/api/tabword').json()
        self.assertEqual(config['deadlines']['detect_text'], 180)
        self.assertEqual(config['deadlines']['detect_file'], 180)
        self.assertEqual(config['deadlines']['paraphrase_stream'], 300)

    def test_limits_follow_the_environment_so_the_countdown_matches_the_server(self):
        with patch.dict('os.environ', {'TABWORD_DEADLINE_SECONDS': '90', 'TABWORD_STREAM_DEADLINE_SECONDS': '240'}):
            deadlines = self.client.get('/api/tabword').json()['deadlines']
        self.assertEqual((deadlines['detect_text'], deadlines['paraphrase_stream']), (90, 240))

    def test_every_billable_operation_has_a_limit(self):
        self.assertTrue(set(billing.SUBMISSIONS) <= set(billing.deadlines()) | {'document_paraphrase'})


class Timing(Base):
    def test_start_time_and_elapsed_are_recorded(self):
        user = make_user(500); self.as_user(user)
        quote, paid, values = self.buy()
        before = billing.get_quote(quote['quote_id'])
        self.assertIsNone(before['started_at'])
        with patch.object(tabword, 'call', return_value={'code': 0, 'message': 'success', 'data': {}}):
            self.submit('detect_text', values, paid['order_id'])
        after = billing.get_quote(quote['quote_id'])
        self.assertIsNotNone(after['started_at'])
        self.assertGreaterEqual(after['elapsed'], 0)
        self.assertLess(after['elapsed'], 5)


class StuckOrders(Base):
    def stuck(self, user, started_ago, state='processing', operation='detect_text', values=None):
        quote, paid, values = self.buy(operation, values)
        self.set_quote(quote['quote_id'], state=state,
                       started_at=(time.time() - started_ago) if started_ago is not None else None)
        return quote

    def test_old_processing_order_is_failed_and_refunded(self):
        user = make_user(500); self.as_user(user)
        quote = self.stuck(user, 11 * 60)
        self.assertEqual(billing.fail_stuck(), [quote['quote_id']])
        stored = billing.get_quote(quote['quote_id'])
        self.assertEqual(stored['state'], 'failed')
        self.assertIn('判定为中断', stored['error'])
        self.assertEqual(points.balance(user['id']), 500)

    def test_recent_processing_order_is_left_alone(self):
        user = make_user(500); self.as_user(user)
        quote = self.stuck(user, 30)
        self.assertEqual(billing.fail_stuck(), [])
        self.assertEqual(billing.get_quote(quote['quote_id'])['state'], 'processing')
        self.assertLess(points.balance(user['id']), 500)

    def test_legacy_order_without_start_time_uses_payment_time_and_a_longer_limit(self):
        user = make_user(500); self.as_user(user)
        quote = self.stuck(user, None)
        self.assertEqual(billing.fail_stuck(), [])                    # 刚付款，不误伤
        with closing(alipay._connect()) as conn, conn:
            conn.execute('UPDATE payment_orders SET paid_at=? WHERE job_id=?', (int(time.time()) - 2 * 3600, quote['quote_id']))
        self.assertEqual(billing.fail_stuck(), [quote['quote_id']])
        self.assertEqual(points.balance(user['id']), 500)

    def test_dangling_stream_order_is_cleaned_but_running_document_task_is_not(self):
        user = make_user(900); self.as_user(user)
        stream = self.stuck(user, 11 * 60, state='submitted', operation='paraphrase_stream',
                            values={'text': '论文正文' * 30, 'preset': 'p1'})
        task = self.stuck(user, 11 * 60, state='submitted', operation='document_async', values={'text': '正文' * 40})
        self.assertEqual(billing.fail_stuck(), [stream['quote_id']])
        self.assertEqual(billing.get_quote(task['quote_id'])['state'], 'submitted')

    def test_finished_orders_are_never_touched_and_refund_is_once(self):
        user = make_user(500); self.as_user(user)
        quote = self.stuck(user, 11 * 60, state='complete')
        self.assertEqual(billing.fail_stuck(), [])
        failed = self.stuck(user, 11 * 60)
        billing.fail_stuck(); billing.fail_stuck()
        self.assertEqual(points.balance(user['id']), 500 - quote['amount_cents'] / 10)   # 完成的那单不退，失败的只退一次

    def test_purge_expired_runs_the_stuck_cleanup(self):
        user = make_user(500); self.as_user(user)
        quote = self.stuck(user, 11 * 60)
        billing.purge_expired()
        self.assertEqual(billing.get_quote(quote['quote_id'])['state'], 'failed')


if __name__ == '__main__':
    import unittest
    unittest.main()
