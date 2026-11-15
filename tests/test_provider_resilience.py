# -*- coding: utf-8 -*-
"""服务商请求的韧性：连接阶段限时并安全重试、总时长硬截止、阶段进度、
支付宝付款失败自动退成积分、文件没送达时恢复订单。全部离线（本机假服务端）。"""
import json
import os
import socket
import threading
import time
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

from core import alipay, points, service_billing as billing, tabword
from tests.test_points import PointsBase, make_user

OK_BODY = json.dumps({'success': True, 'presets': []}).encode()


def ok(conn, stop):
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s'
                 % (len(OK_BODY), OK_BODY))


def silent(conn, stop):
    """收下请求然后什么都不回：服务商处理中卡死的样子。"""
    stop.wait(30)


def trickle(conn, stop):
    """先回响应头，再一个字节一个字节地涓滴：每次读都不超时，只有总时长截止能拦住它。"""
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000000\r\n\r\n')
    while not stop.is_set():
        conn.sendall(b'x')
        stop.wait(0.2)


class RawServer:
    def __init__(self, behaviour, port=0):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', port))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.behaviour, self.stop = behaviour, threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            conn.recv(65536)
            self.behaviour(conn, self.stop)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.stop.set()
        self.sock.close()


def free_port():
    with closing(socket.socket()) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class NetworkLayer(unittest.TestCase):
    def setUp(self):
        self.servers = []
        env = patch.dict(os.environ, {'TABWORD_API_KEY': 'sk-offline-only', 'TABWORD_ENABLE_CALLS': 'true'})
        env.start(); self.addCleanup(env.stop)
        self.addCleanup(lambda: [s.close() for s in self.servers])
        pause = patch.object(tabword, '_pause', lambda seconds: None)
        pause.start(); self.addCleanup(pause.stop)

    def serve(self, behaviour, port=0):
        server = RawServer(behaviour, port)
        self.servers.append(server)
        return server

    def point_at(self, port):
        env = patch.dict(os.environ, {'TABWORD_BASE_URL': 'http://127.0.0.1:%d' % port})
        env.start(); self.addCleanup(env.stop)

    def test_phases_run_in_order_on_a_healthy_provider(self):
        self.point_at(self.serve(ok).port)
        phases = []
        result = tabword.call('presets', {}, on_phase=lambda phase, **info: phases.append(phase))
        self.assertTrue(result['success'])
        self.assertEqual(phases, ['connecting', 'sending', 'waiting'])

    def test_connect_failure_is_retried_once_then_reported_as_not_delivered(self):
        self.point_at(free_port())                                  # 没人监听：连接被拒
        phases = []
        with self.assertRaises(tabword.NotDelivered) as caught:
            tabword.call('presets', {}, on_phase=lambda phase, **info: phases.append(phase))
        self.assertEqual(phases, ['connecting', 'retrying', 'connecting'])
        self.assertIn('请求没有送达', str(caught.exception))
        self.assertEqual(caught.exception.status, 503)

    def test_retry_succeeds_when_the_provider_comes_back(self):
        port = free_port()
        self.point_at(port)

        def provider_recovers(seconds):
            self.serve(ok, port)
        phases = []
        with patch.object(tabword, '_pause', provider_recovers):
            result = tabword.call('presets', {}, on_phase=lambda phase, **info: phases.append(phase))
        self.assertTrue(result['success'])
        self.assertEqual(phases, ['connecting', 'retrying', 'connecting', 'sending', 'waiting'])

    def test_no_retry_when_the_deadline_is_too_close(self):
        self.point_at(free_port())
        phases = []
        with self.assertRaises(tabword.NotDelivered):
            tabword.call('presets', {}, deadline=10, on_phase=lambda phase, **info: phases.append(phase))
        self.assertNotIn('retrying', phases)

    def test_silent_provider_is_cut_at_the_deadline_and_says_the_file_was_delivered(self):
        self.point_at(self.serve(silent).port)
        began = time.time()
        with self.assertRaises(tabword.TabwordError) as caught:
            tabword.call('presets', {}, deadline=1)
        self.assertLess(time.time() - began, 5)
        self.assertEqual(caught.exception.status, 504)
        self.assertIn('文件已送达服务商', str(caught.exception))
        self.assertIn('1 秒内没有返回结果', str(caught.exception))
        self.assertNotIsInstance(caught.exception, tabword.NotDelivered)   # 已送达就不能当作没提交

    def test_provider_trickling_bytes_cannot_outlive_the_deadline(self):
        # 线上 18 分钟的那次：每次读都没超时，但永远读不完
        self.point_at(self.serve(trickle).port)
        began = time.time()
        with self.assertRaises(tabword.TabwordError) as caught:
            tabword.call('presets', {}, deadline=1)
        self.assertLess(time.time() - began, 5)
        self.assertEqual(caught.exception.status, 504)
        self.assertIn('1 秒内没有返回结果', str(caught.exception))

    def test_a_finished_call_leaves_no_watchdog_behind(self):
        self.point_at(self.serve(ok).port)
        before = threading.active_count()
        for _ in range(5):
            tabword.call('presets', {}, deadline=30)
        time.sleep(0.3)
        self.assertLessEqual(threading.active_count(), before + 1)


class BillingBase(PointsBase):
    def setUp(self):
        super().setUp()
        billing._PLATFORM_FAILS.clear()          # 平台失败计数是全局的，免得前一个用例把 p1 暂停了

    def quote_for(self, user, text='论文正文' * 30):
        values = {'text': text, 'task_platform': 'p1'}
        quote = self.client.post('/api/tabword/billing/quote', data={
            'operation': 'detect_text', 'params': json.dumps(values, ensure_ascii=False)}).json()
        return quote, values

    def pay_points(self, user):
        quote, values = self.quote_for(user)
        paid = self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).json()
        return quote, paid['order_id'], values

    def pay_alipay(self, user):
        quote, values = self.quote_for(user)
        order_no = 'AL' + uuid.uuid4().hex[:16].upper()
        now = int(time.time())
        with closing(alipay._connect()) as conn, conn:
            conn.execute("""INSERT INTO payment_orders (out_trade_no,job_id,amount_cents,status,qr_code,trade_no,
                            created_at,paid_at,payment_url,payment_product) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (order_no, quote['quote_id'], quote['amount_cents'], 'TRADE_SUCCESS', '', 'T-' + order_no,
                          now, now, 'u', 'page_pay'))
        return quote, order_no, values

    def submit(self, values, order_id):
        return self.client.post('/api/tabword/detect_text', data={
            'params': json.dumps(values, ensure_ascii=False), 'submit': 'true', 'payment_order': order_id})

    def state(self, quote):
        return billing.get_quote(quote['quote_id'])


GOOD = {'code': 0, 'message': 'success', 'data': {'score': 9}}
TIMEOUT = tabword.TabwordError('文件已送达服务商，但服务商在 178 秒内没有返回结果，已放弃等待', 504)


class AlipayFailureBecomesPoints(BillingBase):
    def test_failed_alipay_order_is_refunded_as_points_once(self):
        user = make_user(0); self.as_user(user)
        quote, order, values = self.pay_alipay(user)
        with patch.object(tabword, 'call', side_effect=TIMEOUT):
            res = self.submit(values, order)
        self.assertEqual(res.status_code, 504)
        self.assertIn('支付宝付款已按原额退还为积分', res.json()['detail'])
        self.assertAlmostEqual(points.balance(user['id']), quote['amount_cents'] / 10)
        stored = self.state(quote)
        self.assertEqual(stored['state'], 'failed')
        self.assertTrue(stored['error'].endswith('（支付宝付款已退成积分）'))
        # 再记一次失败（例如重复回调）不会再发积分
        self.assertEqual(billing.record(quote['quote_id'], 'detect_text', error=True, reason='again'), billing.FAILED_MANUAL)
        self.assertAlmostEqual(points.balance(user['id']), quote['amount_cents'] / 10)

    def test_the_refunded_points_can_pay_for_a_retry_straight_away(self):
        user = make_user(0); self.as_user(user)
        quote, order, values = self.pay_alipay(user)
        with patch.object(tabword, 'call', side_effect=TIMEOUT):
            self.submit(values, order)
        again, paid, _ = self.pay_points(user)
        with patch.object(tabword, 'call', return_value=GOOD):
            self.assertEqual(self.submit(values, paid).status_code, 200)
        self.assertEqual(self.state(again)['state'], 'complete')
        self.assertEqual(points.balance(user['id']), 0)

    def test_points_paid_order_still_gets_its_own_points_back(self):
        user = make_user(500); self.as_user(user)
        quote, order, values = self.pay_points(user)
        with patch.object(tabword, 'call', side_effect=TIMEOUT):
            res = self.submit(values, order)
        self.assertIn('积分已全额退还', res.json()['detail'])
        self.assertEqual(points.balance(user['id']), 500)
        self.assertTrue(self.state(quote)['error'].endswith('（积分已退）'))

    def test_anonymous_alipay_order_cannot_be_credited_and_asks_for_manual_refund(self):
        user = make_user(0); self.as_user(user)
        quote, order, values = self.pay_alipay(user)
        with closing(billing.connect()) as conn, conn:
            conn.execute('UPDATE service_quotes SET user_id=NULL WHERE id=?', (quote['quote_id'],))
        with patch.object(tabword, 'call', side_effect=TIMEOUT):
            res = self.submit(values, order)
        self.assertIn('联系客服退款', res.json()['detail'])
        self.assertEqual(points.balance(user['id']), 0)

    def test_stuck_alipay_order_is_refunded_by_the_sweeper(self):
        user = make_user(0); self.as_user(user)
        quote, order, values = self.pay_alipay(user)
        billing.authorize('detect_text', values, None, order)
        with closing(billing.connect()) as conn, conn:
            conn.execute('UPDATE service_quotes SET started_at=? WHERE id=?', (time.time() - 3600, quote['quote_id']))
        self.assertEqual(billing.fail_stuck(), [quote['quote_id']])
        self.assertEqual(self.state(quote)['state'], 'failed')
        self.assertAlmostEqual(points.balance(user['id']), quote['amount_cents'] / 10)
        self.assertEqual(billing.fail_stuck(), [])

    def test_refund_failed_reports_how_it_refunded(self):
        user = make_user(500); self.as_user(user)
        q1, o1, _ = self.pay_points(user)
        self.assertEqual(points.refund_failed(q1['quote_id'], user['id'], '检测')[1], 'spend')
        q2, o2, _ = self.pay_alipay(user)
        self.assertEqual(points.refund_failed(q2['quote_id'], user['id'], '检测')[1], 'alipay')
        self.assertEqual(points.refund_failed(q2['quote_id'], user['id'], '检测'), (None, None))
        self.assertEqual(points.refund_failed('TW-' + uuid.uuid4().hex, user['id'], '检测'), (None, None))


class NotDeliveredKeepsThePayment(BillingBase):
    def check(self, pay):
        user = make_user(500 if pay == 'points' else 0); self.as_user(user)
        quote, order, values = (self.pay_points if pay == 'points' else self.pay_alipay)(user)
        balance_after_paying = points.balance(user['id'])
        with patch.object(tabword, 'call', side_effect=tabword.NotDelivered('无法连接服务商', 503)):
            res = self.submit(values, order)
        self.assertEqual(res.status_code, 503)
        self.assertIn('付款仍然有效', res.json()['detail'])
        self.assertEqual(self.state(quote)['state'], 'ready')                 # 恢复成可重新提交，不是 failed
        self.assertEqual(points.balance(user['id']), balance_after_paying)    # 不退款：付款还在
        self.assertIsNone(billing.get_progress(quote['quote_id']))
        with patch.object(tabword, 'call', return_value=GOOD):                # 同一笔付款重新提交
            self.assertEqual(self.submit(values, order).status_code, 200)
        self.assertEqual(self.state(quote)['state'], 'complete')

    def test_points_paid(self):
        self.check('points')

    def test_alipay_paid(self):
        self.check('alipay')

    def test_not_delivered_does_not_pause_the_platform(self):
        user = make_user(500); self.as_user(user)
        billing._PLATFORM_FAILS.clear()
        for _ in range(3):
            quote, order, values = self.pay_points(user)
            with patch.object(tabword, 'call', side_effect=tabword.NotDelivered('无法连接服务商', 503)):
                self.submit(values, order)
        self.assertEqual(billing.unavailable_platforms(), {})


class Progress(BillingBase):
    def test_route_passes_a_hard_deadline_and_a_phase_callback(self):
        user = make_user(500); self.as_user(user)
        quote, order, values = self.pay_points(user)
        seen = {}

        def fake(name, params, file, **kwargs):
            seen.update(kwargs)
            kwargs['on_phase']('sending')
            seen['during'] = billing.get_progress(quote['quote_id'])
            return GOOD
        with patch.object(tabword, 'call', side_effect=fake):
            self.assertEqual(self.submit(values, order).status_code, 200)
        self.assertEqual(seen['deadline'], billing.deadline() - 2)          # 比外层等待早 2 秒，原因由线程如实报告
        self.assertEqual(seen['during']['phase'], 'sending')
        self.assertIsNone(billing.get_progress(quote['quote_id']))          # 结束后清掉

    def test_status_endpoint_shows_progress_only_while_processing(self):
        user = make_user(500); self.as_user(user)
        quote, order, values = self.pay_points(user)
        url = '/api/tabword/billing/%s/%s' % (quote['quote_id'], order)
        self.assertIsNone(self.client.get(url).json()['progress'])          # 还没开始
        billing.authorize('detect_text', values, None, order)                # → processing
        note = billing.progress_for(quote['quote_id'])
        note('waiting')
        progress = self.client.get(url).json()['progress']
        self.assertEqual(progress['phase'], 'waiting')
        self.assertIsInstance(progress['elapsed'], int)
        billing.clear_progress(quote['quote_id'])
        note('connecting')                                                   # 迟到的回调不会把它写回来
        self.assertIsNone(billing.get_progress(quote['quote_id']))
        self.assertIsNone(self.client.get(url).json()['progress'])

    def test_retry_attempt_is_reported(self):
        note = billing.progress_for('TW-x')
        note('retrying', attempt=1)
        self.assertEqual(billing.get_progress('TW-x')['phase'], 'retrying')
        billing.clear_progress('TW-x')


if __name__ == '__main__':
    unittest.main()
