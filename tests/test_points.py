# -*- coding: utf-8 -*-
"""积分：记账幂等、不透支、并发双花、积分支付落订单、充值入账、后台调整。离线。"""
import json
import os
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import accounts, alipay, points, service_billing as billing, tabword
from web import app as web
from web.account_routes import current_user
from tests import signed_in


def make_user(balance=0):
    email = 'p%s@example.com' % uuid.uuid4().hex[:10]
    with closing(accounts._connect()) as conn, conn:
        uid = conn.execute("""INSERT INTO users (username,password_hash,created_at,email,email_key,email_verified_at)
                              VALUES (?,?,?,?,?,?)""",
                           (email, accounts.UNUSABLE_HASH, int(time.time()), email, email, 1)).lastrowid
    if balance:
        points.admin_adjust(email, balance, 'test')
    return {'id': uid, 'username': email, 'email': email}


class PointsBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'ALIPAY_ORDER_DB': os.path.join(self.temp.name, 'orders.db'),
                                      'TABWORD_ENABLE_CALLS': 'true', 'TABWORD_API_KEY': 'offline-only'})
        env.start(); self.addCleanup(env.stop)
        db = patch.object(accounts, '_db_path', lambda: Path(self.temp.name) / 'accounts.sqlite3')
        db.start(); self.addCleanup(db.stop)
        cfg = {'enabled': True, 'amount_cents': 2999, 'mode': 'sandbox', 'app_id': 'test-app',
               'seller_id': 'seller', 'public_path': None, 'notify_url': 'https://x/n', 'return_url': 'https://x/',
               'private_path': None}
        for p in (patch.object(alipay, 'config', return_value=cfg),
                  patch.object(alipay, '_signed_params', return_value={'offline': 'test'}),
                  patch.object(alipay, '_request', return_value={}),
                  patch.object(web, 'AFFILIATE_ADMIN_ON', True),
                  patch.object(web, 'AFFILIATE_ADMIN_USER', 'admin'),
                  patch.object(web, 'AFFILIATE_ADMIN_PASS', 'secret')):
            p.start(); self.addCleanup(p.stop)
        # 建好表再开并发：affiliates 里已有的迁移（ALTER TABLE）在全新库上并发首次访问会撞车，
        # 线上库早已迁移完，这里模拟「服务已启动」的状态。
        for connect in (alipay._connect, points._connect):
            connect().close()
        self.client = TestClient(web.app)
        self.auth = ('admin', 'secret')
        self.addCleanup(web.app.dependency_overrides.__setitem__, current_user, signed_in)

    def as_user(self, user):
        web.app.dependency_overrides[current_user] = lambda: user

    def new_quote(self, user, text='论文正文'):
        quote = billing.quote('detect_text', {'text': text, 'task_platform': 'test'})
        accounts.record_upload(user['id'], 'tabword', quote['quote_id'], '文本', 'detect_text')
        return quote


class Conversion(unittest.TestCase):
    def test_points_price_equals_the_yuan_price_without_rounding(self):
        for cents, pts in ((99, 9.9), (100, 10), (101, 10.1), (1, 0.1), (399, 39.9), (2999, 299.9), (10, 1)):
            self.assertEqual(points.points_for_cents(cents), pts, cents)

    def test_display_drops_the_decimal_point_for_whole_numbers(self):
        for units, shown in ((0, 0), (990, 99), (99, 9.9), (901, 90.1), (-99, -9.9), (-100, -10)):
            self.assertEqual(accounts.points_display(units), shown, units)
        self.assertIsInstance(accounts.points_display(100), int)

    def test_one_decimal_place_only(self):
        for good, units in ((5, 50), (0.1, 1), (9.9, 99), (12.5, 125), (-3.3, -33)):
            self.assertEqual(points._units(good), units, good)
        for bad in (0.05, 1.234, '5', None, True, [1]):
            with self.assertRaises(points.PointsError):
                points._units(bad)


class Settings(PointsBase):
    def test_defaults_and_validation(self):
        self.assertEqual(points.get_settings(), {'enabled': True, 'signup_bonus': 0, 'signup_bonus_daily_cap': 100,
                                                  'topup_packages': [5, 10, 20, 50, 100],
                                                  'ai_points_per_1k_tokens': 0.2, 'ai_min_charge': 0.5,
                                                  'ai_max_output_tokens': 3000})
        good = {'enabled': True, 'signup_bonus': 20, 'signup_bonus_daily_cap': 50, 'topup_packages': [20, 5]}
        self.assertEqual(points.save_settings(good)['topup_packages'], [5, 20])
        for bad in ({**good, 'topup_packages': []}, {**good, 'topup_packages': [5, 5]},
                    {**good, 'topup_packages': [0]}, {**good, 'topup_packages': [True]},
                    {**good, 'topup_packages': list(range(1, 10))}, {**good, 'signup_bonus': -1},
                    {**good, 'signup_bonus': 1.55}, {**good, 'signup_bonus': '5'}, {**good, 'enabled': 1}, {'bogus': 1}, {}, 'x',
                    {**good, 'ai_points_per_1k_tokens': 0}, {**good, 'ai_points_per_1k_tokens': 0.05},
                    {**good, 'ai_min_charge': -1}, {**good, 'ai_max_output_tokens': 50},
                    {**good, 'ai_max_output_tokens': 9000}, {**good, 'ai_max_output_tokens': 3000.5}):
            with self.assertRaises(points.PointsError):
                points.save_settings(bad)
        self.assertEqual(points.get_settings()['signup_bonus'], 20)
        self.assertEqual(points.save_settings({**good, 'signup_bonus': 12.5})['signup_bonus'], 12.5)

    def test_admin_endpoints_need_admin_and_work(self):
        body = {'enabled': True, 'signup_bonus': 15, 'signup_bonus_daily_cap': 10, 'topup_packages': [10]}
        self.assertEqual(self.client.get('/api/admin/points-settings').status_code, 401)
        self.assertEqual(self.client.put('/api/admin/points-settings', json=body).status_code, 401)
        self.assertEqual(self.client.put('/api/admin/points-settings', json=body, auth=self.auth).status_code, 200)
        self.assertEqual(self.client.get('/api/admin/points-settings', auth=self.auth).json()['signup_bonus'], 15)
        self.assertEqual(self.client.put('/api/admin/points-settings', json={**body, 'topup_packages': []},
                                         auth=self.auth).status_code, 422)


class Ledger(PointsBase):
    def test_admin_adjust_records_ledger_and_cannot_overdraw(self):
        user = make_user()
        res = self.client.post('/api/admin/points/adjust', json={'email': user['email'], 'delta': 50, 'note': '活动'},
                               auth=self.auth)
        self.assertEqual(res.json()['balance'], 50)
        self.assertEqual(self.client.post('/api/admin/points/adjust', json={'email': user['email'], 'delta': -80},
                                          auth=self.auth).status_code, 402)
        self.assertEqual(points.balance(user['id']), 50)
        self.assertEqual(self.client.post('/api/admin/points/adjust', json={'email': user['email'], 'delta': 0},
                                          auth=self.auth).status_code, 422)
        self.assertEqual(self.client.post('/api/admin/points/adjust', json={'email': 'nobody@example.com', 'delta': 5},
                                          auth=self.auth).status_code, 404)
        rows = self.client.get('/api/admin/points/ledger', params={'email': user['email']}, auth=self.auth).json()['ledger']
        self.assertEqual([r['delta'] for r in rows], [50])
        self.assertEqual(self.client.post('/api/admin/points/adjust', json={'email': user['email'], 'delta': 5}).status_code, 401)

    def test_ledger_balance_after_matches(self):
        user = make_user(30)
        points.admin_adjust(user['email'], -12)
        rows = points.ledger(user['id'])
        self.assertEqual([(r['delta'], r['balance_after']) for r in rows], [(-12, 18), (30, 30)])


class Decimals(PointsBase):
    def test_repeated_decimal_spends_have_no_float_drift(self):
        user = make_user(100)
        for _ in range(10):
            points.pay(user['id'], 'TW-' + uuid.uuid4().hex, 99)        # 10 x 9.9 = 99
        self.assertEqual(points.balance(user['id']), 1)
        self.assertEqual([r['balance_after'] for r in points.ledger(user['id'], 2)], [1, 10.9])

    def test_cannot_spend_a_tenth_more_than_the_balance(self):
        user = make_user(9.8)
        with self.assertRaises(points.PointsError) as caught:
            points.pay(user['id'], 'TW-' + uuid.uuid4().hex, 99)        # 要 9.9
        self.assertEqual(caught.exception.status, 402)
        self.assertEqual(points.balance(user['id']), 9.8)
        points.admin_adjust(user['email'], 0.1)
        points.pay(user['id'], 'TW-' + uuid.uuid4().hex, 99)
        self.assertEqual(points.balance(user['id']), 0)

    def test_admin_adjust_accepts_one_decimal_only(self):
        user = make_user()
        self.assertEqual(points.admin_adjust(user['email'], 0.5)['balance'], 0.5)
        self.assertEqual(self.client.post('/api/admin/points/adjust', json={'email': user['email'], 'delta': 0.25},
                                          auth=self.auth).status_code, 422)
        self.assertEqual(self.client.post('/api/admin/points/adjust', json={'email': user['email'], 'delta': 1.5},
                                          auth=self.auth).json()['balance'], 2)

    def test_me_and_summary_report_decimal_points(self):
        user = make_user(10); self.as_user(user)
        points.pay(user['id'], 'TW-' + uuid.uuid4().hex, 99)
        data = self.client.get('/api/points').json()
        self.assertEqual(data['balance'], 0.1)
        self.assertEqual(data['prices']['detect_text'], 9.9)
        self.assertEqual(data['prices']['format_export'], 299.9)
        self.assertEqual(data['prices_cents']['detect_text'], 99)
        self.assertEqual(data['ledger'][0]['delta'], -9.9)

    def test_old_integer_points_are_converted_once(self):
        user = make_user()
        with closing(points._connect()) as conn, conn:        # 还原成旧版本的数据：整数积分、没有迁移标记
            conn.execute("UPDATE users SET points=5 WHERE id=?", (user['id'],))
            conn.execute("""INSERT INTO points_ledger (user_id,delta,balance_after,kind,ref,note,created_at)
                            VALUES (?,5,5,'admin','old-1','旧数据',1)""", (user['id'],))
            conn.execute("INSERT INTO point_topups VALUES ('LPOLD0001',?,100,1000,1)", (user['id'],))
            conn.execute("DELETE FROM points_settings WHERE name='_unit'")
        self.assertEqual(points.balance(user['id']), 5)          # 5 积分，迁移后仍是 5 积分
        self.assertEqual(points.ledger(user['id'])[0]['delta'], 5)
        with closing(points._connect()) as conn:
            self.assertEqual(conn.execute("SELECT points FROM point_topups WHERE order_no='LPOLD0001'").fetchone()[0], 1000)
            raw = conn.execute("SELECT points FROM users WHERE id=?", (user['id'],)).fetchone()[0]
        self.assertEqual(raw, 50)
        points.balance(user['id'])                                # 再连一次不会再乘
        with closing(points._connect()) as conn:
            self.assertEqual(conn.execute("SELECT points FROM users WHERE id=?", (user['id'],)).fetchone()[0], 50)


class Topup(PointsBase):
    def paid_topup(self, user, yuan=10):
        job = 'PT-' + uuid.uuid4().hex
        order_no = 'LP' + uuid.uuid4().hex[:16].upper()
        now = int(time.time())
        with closing(alipay._connect()) as conn, conn:
            conn.execute("""INSERT INTO payment_orders (out_trade_no,job_id,amount_cents,status,qr_code,created_at,
                            payment_url,payment_product) VALUES (?,?,?,?,?,?,?,?)""",
                         (order_no, job, yuan * 100, 'WAIT_BUYER_PAY', '', now, 'u', 'page_pay'))
        with closing(points._connect()) as conn, conn:
            conn.execute('INSERT INTO point_topups VALUES (?,?,?,?,?)', (order_no, user['id'], yuan * 100, yuan * 100, now))
        return order_no

    def test_only_configured_packages_can_be_bought(self):
        user = make_user()
        for bad in (0, 7, -5, 100000):
            with self.assertRaises(points.PointsError):
                points.create_topup(user['id'], bad)
        self.as_user(user)
        self.assertEqual(self.client.post('/api/points/topup', json={'yuan': 7}).status_code, 422)

    def test_topup_creates_checkout_and_credits_once_when_paid(self):
        user = make_user(); self.as_user(user)
        res = self.client.post('/api/points/topup', json={'yuan': 10})
        self.assertEqual(res.status_code, 200, res.text)
        order_no = res.json()['order_id']
        self.assertEqual(res.json()['points'], 100)
        self.assertEqual(points.balance(user['id']), 0)             # 没付款前不入账
        with patch.object(alipay, 'verify', return_value=True):
            notice = {'out_trade_no': order_no, 'app_id': 'test-app', 'seller_id': 'seller',
                      'total_amount': '10.00', 'trade_status': 'TRADE_SUCCESS', 'trade_no': 't1'}
            self.assertTrue(alipay.apply_notification(notice))
            self.assertTrue(alipay.apply_notification(notice))      # 支付宝会重复通知
        self.assertEqual(points.balance(user['id']), 100)
        self.assertEqual([r['kind'] for r in points.ledger(user['id'])], ['topup'])

    def test_credit_is_idempotent_and_reconcile_recovers_a_missed_credit(self):
        user = make_user()
        order_no = self.paid_topup(user)
        self.assertFalse(points.credit_topup(order_no))             # 还没付款
        with closing(alipay._connect()) as conn, conn:              # 模拟：付款标记成功了，但入账那步没跑
            conn.execute("UPDATE payment_orders SET status='TRADE_SUCCESS', paid_at=? WHERE out_trade_no=?",
                         (int(time.time()), order_no))
        self.assertEqual(points.balance(user['id']), 0)
        self.assertEqual(points.summary(user['id'])['balance'], 100)   # 读余额时补记
        self.assertFalse(points.credit_topup(order_no))
        self.assertEqual(points.balance(user['id']), 100)

    def test_reading_balance_confirms_a_paid_topup_without_notification(self):
        # 本地收不到异步通知、付款页回跳到新标签或关掉弹窗后没人轮询：读余额时要主动向支付宝确认
        user = make_user()
        order_no = self.paid_topup(user)
        alipay._LAST_QUERY.pop(order_no, None)
        paid = {'code': '10000', 'out_trade_no': order_no, 'trade_status': 'TRADE_SUCCESS',
                'total_amount': '10.00', 'trade_no': 't-sync'}
        with patch.object(alipay, '_request', return_value=paid):
            self.assertEqual(points.summary(user['id'])['balance'], 100)
        self.assertEqual([r['kind'] for r in points.ledger(user['id'])], ['topup'])

    def test_old_unpaid_topups_are_not_queried(self):
        user = make_user()
        order_no = self.paid_topup(user)
        with closing(points._connect()) as conn, conn:
            conn.execute("UPDATE point_topups SET created_at=0 WHERE order_no=?", (order_no,))
        with patch.object(alipay, 'sync_order') as sync:
            points.summary(user['id'])
        sync.assert_not_called()

    def test_amount_mismatch_is_not_credited(self):
        user = make_user()
        order_no = self.paid_topup(user, 10)
        with closing(alipay._connect()) as conn, conn:
            conn.execute("UPDATE payment_orders SET status='TRADE_SUCCESS', amount_cents=100 WHERE out_trade_no=?", (order_no,))
        self.assertFalse(points.credit_topup(order_no))
        self.assertEqual(points.balance(user['id']), 0)

    def test_only_the_buyer_can_see_or_open_a_topup(self):
        buyer, other = make_user(), make_user()
        self.as_user(buyer)
        order_no = self.client.post('/api/points/topup', json={'yuan': 5}).json()['order_id']
        self.as_user(other)
        self.assertEqual(self.client.get('/api/points/topup/' + order_no).status_code, 404)
        self.assertEqual(self.client.get('/api/points/topup/%s/checkout' % order_no, follow_redirects=False).status_code, 404)

    def test_disabled_points_refuse_topup(self):
        points.save_settings({**points.get_settings(), 'enabled': False})
        with self.assertRaises(points.PointsError):
            points.create_topup(make_user()['id'], 10)


class Spend(PointsBase):
    def test_pay_deducts_once_and_creates_a_paid_points_order(self):
        user = make_user(100)
        job = 'TW-' + uuid.uuid4().hex
        first = points.pay(user['id'], job, 99)
        again = points.pay(user['id'], job, 99)
        self.assertEqual(first['out_trade_no'], again['out_trade_no'])
        self.assertEqual((first['status'], first['payment_product'], first['amount_cents']), ('TRADE_SUCCESS', 'points', 99))
        self.assertEqual(points.balance(user['id']), 90.1)
        self.assertEqual([(r['kind'], r['delta']) for r in points.ledger(user['id'])], [('spend', -9.9), ('admin', 100)])
        self.assertTrue(alipay.job_paid(job))

    def test_insufficient_balance_creates_nothing(self):
        user = make_user(5)
        job = 'TW-' + uuid.uuid4().hex
        with self.assertRaises(points.PointsError) as caught:
            points.pay(user['id'], job, 99)
        self.assertEqual(caught.exception.status, 402)
        self.assertEqual(points.balance(user['id']), 5)
        self.assertFalse(alipay.job_paid(job))

    def test_concurrent_payments_of_the_same_job_charge_once(self):
        user = make_user(100)
        job = 'TW-' + uuid.uuid4().hex
        with ThreadPoolExecutor(12) as pool:
            orders = list(pool.map(lambda _: points.pay(user['id'], job, 99)['out_trade_no'], range(12)))
        self.assertEqual(len(set(orders)), 1)
        self.assertEqual(points.balance(user['id']), 90.1)
        with closing(alipay._connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM payment_orders WHERE job_id=?", (job,)).fetchone()[0], 1)

    def test_concurrent_payments_never_overdraw(self):
        user = make_user(30)                       # 30 积分 = 300 个单位，每笔 9.9 积分，够付 3 笔
        jobs = ['TW-' + uuid.uuid4().hex for _ in range(10)]

        def attempt(job):
            try:
                points.pay(user['id'], job, 99)
                return True
            except points.PointsError:
                return False
        with ThreadPoolExecutor(10) as pool:
            results = list(pool.map(attempt, jobs))
        self.assertEqual(sum(results), 3)
        self.assertEqual(points.balance(user['id']), 0.3)
        paid = sum(alipay.job_paid(j) for j in jobs)
        self.assertEqual(paid, 3)

    def test_crash_between_deduct_and_order_is_completed_without_a_second_charge(self):
        user = make_user(100)
        job = 'TW-' + uuid.uuid4().hex
        with patch.object(points, '_write_points_order', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                points.pay(user['id'], job, 99)
        self.assertEqual(points.balance(user['id']), 90.1)
        self.assertFalse(alipay.job_paid(job))
        order = points.pay(user['id'], job, 99)              # 用户重试
        self.assertEqual(points.balance(user['id']), 90.1)
        self.assertEqual(order['amount_cents'], 99)

    def test_reconcile_completes_an_order_that_was_never_written(self):
        user = make_user(100)
        job = 'TW-' + uuid.uuid4().hex
        with patch.object(points, '_write_points_order', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                points.pay(user['id'], job, 99)
        points.summary(user['id'])
        self.assertTrue(alipay.job_paid(job))
        self.assertEqual(alipay.get_order(points._order_no(job))['amount_cents'], 99)

    def test_someone_elses_spend_cannot_be_hijacked(self):
        a, b = make_user(100), make_user(100)
        job = 'TW-' + uuid.uuid4().hex
        with patch.object(points, '_write_points_order', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                points.pay(a['id'], job, 99)
        with self.assertRaises(points.PointsError) as caught:
            points.pay(b['id'], job, 99)
        self.assertEqual(caught.exception.status, 403)

    def test_paying_with_points_closes_a_pending_alipay_order_for_the_same_job(self):
        user = make_user(100)
        job = 'TW-' + uuid.uuid4().hex
        with closing(alipay._connect()) as conn, conn:
            conn.execute("""INSERT INTO payment_orders (out_trade_no,job_id,amount_cents,status,qr_code,created_at,
                            payment_url,payment_product) VALUES ('LPPENDING0001',?,99,'WAIT_BUYER_PAY','',?,'u','page_pay')""",
                         (job, int(time.time())))
        points.pay(user['id'], job, 99)
        self.assertEqual(alipay.get_order('LPPENDING0001')['status'], 'TRADE_CLOSED')

    def test_points_spend_earns_no_affiliate_commission(self):
        user = make_user(100)
        job = 'TW-' + uuid.uuid4().hex
        order = points.pay(user['id'], job, 99)
        self.assertIsNone(order['affiliate_id'])


class PayRoutes(PointsBase):
    def test_pay_quote_then_provider_call_is_authorised(self):
        user = make_user(100); self.as_user(user)
        quote = self.new_quote(user)
        res = self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual((body['points'], body['balance'], body['paid']), (9.9, 90.1, True))
        values = {'text': '论文正文', 'task_platform': 'test'}
        self.assertEqual(billing.authorize('detect_text', values, None, body['order_id']), quote['quote_id'])
        with self.assertRaises(tabword.TabwordError) as caught:      # 一笔付款只能提交一次
            billing.authorize('detect_text', values, None, body['order_id'])
        self.assertEqual(caught.exception.status, 409)

    def test_not_your_quote_or_unknown_quote(self):
        owner, other = make_user(100), make_user(100)
        quote = self.new_quote(owner)
        self.as_user(other)
        self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).status_code, 404)
        self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': 'TW-' + 'a' * 32}).status_code, 404)
        self.assertEqual(points.balance(other['id']), 100)

    def test_insufficient_points_returns_402_and_keeps_quote_buyable(self):
        user = make_user(3); self.as_user(user)
        quote = self.new_quote(user)
        self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).status_code, 402)
        points.admin_adjust(user['email'], 20)
        self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).status_code, 200)

    def test_expired_or_already_submitted_quote_is_refused(self):
        user = make_user(100); self.as_user(user)
        old = self.new_quote(user)
        with closing(billing.connect()) as conn, conn:
            conn.execute('UPDATE service_quotes SET created_at=created_at-3600 WHERE id=?', (old['quote_id'],))
        self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': old['quote_id']}).status_code, 409)
        sent = self.new_quote(user, '另一段')
        with closing(billing.connect()) as conn, conn:
            conn.execute("UPDATE service_quotes SET state='processing' WHERE id=?", (sent['quote_id'],))
        self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': sent['quote_id']}).status_code, 409)
        self.assertEqual(points.balance(user['id']), 100)

    def test_provider_disabled_refuses_before_charging(self):
        user = make_user(100); self.as_user(user)
        quote = self.new_quote(user)
        with patch.dict(os.environ, {'TABWORD_ENABLE_CALLS': 'false'}):
            self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']}).status_code, 503)
        self.assertEqual(points.balance(user['id']), 100)

    def test_request_must_name_exactly_one_target(self):
        user = make_user(100); self.as_user(user)
        self.assertEqual(self.client.post('/api/points/pay', json={}).status_code, 422)
        self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': 'TW-x', 'job_id': 'j1'}).status_code, 422)

    def test_requires_login(self):
        web.app.dependency_overrides.pop(current_user, None)
        self.assertEqual(self.client.post('/api/points/pay', json={'quote_id': 'TW-x'}).status_code, 401)
        self.assertEqual(self.client.get('/api/points').status_code, 401)

    def test_format_export_pays_with_points_and_downloads_without_alipay(self):
        user = make_user(400); self.as_user(user)
        jid = 'j' + uuid.uuid4().hex
        docx = Path(self.temp.name) / 'f.docx'
        docx.write_bytes(b'word')
        web.JOBS[jid] = {'id': jid, 'kind': 'school', 'state': 'done', 'name': '论文', 'tmp': self.temp.name,
                         'files': {'docx': str(docx)}, 'log': [], 'school_key': 'school:hkjjxy'}
        self.addCleanup(web.JOBS.pop, jid, None)
        accounts.record_upload(user['id'], 'format', jid, '论文', 'x')
        res = self.client.post('/api/points/pay', json={'job_id': jid})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual((res.json()['points'], res.json()['balance']), (299.9, 100.1))
        with patch.object(alipay, 'public_status', return_value={'enabled': False, 'amount_cents': None}):
            ok = self.client.get('/api/format/jobs/%s/download' % jid, params={'payment_order': res.json()['order_id']})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.content, b'word')
            self.assertEqual(self.client.get('/api/format/jobs/%s/download' % jid).status_code, 503)

    def test_summary_lists_packages_and_balance(self):
        user = make_user(40); self.as_user(user)
        data = self.client.get('/api/points').json()
        self.assertEqual(data['balance'], 40)
        self.assertEqual(data['packages'][0], {'yuan': 5, 'points': 50})
        self.assertEqual(data['rate'], 10)


if __name__ == '__main__':
    unittest.main()
