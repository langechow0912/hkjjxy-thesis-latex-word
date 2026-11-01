import os
import shutil
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import accounts, alipay
from web import app as web
from web.account_routes import current_user
from tests import signed_in


def name():
    return 'u' + uuid.uuid4().hex[:10]


class AccountTests(unittest.TestCase):
    def setUp(self):
        # 这组用例走真实登录，不用默认的「已登录」替身。
        web.app.dependency_overrides.pop(current_user, None)
        self.addCleanup(web.app.dependency_overrides.__setitem__, current_user, signed_in)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'ALIPAY_ORDER_DB': os.path.join(self.temp.name, 'orders.db')})
        env.start(); self.addCleanup(env.stop)
        accounts._FAILS.clear()
        self.client = TestClient(web.app)

    def register(self, username=None, password='correct-horse'):
        return self.client.post('/api/account/register', json={'username': username or name(), 'password': password})

    def test_register_signs_in_with_httponly_cookie(self):
        response = self.register('学生_01')
        self.assertEqual(response.status_code, 200)
        self.assertIn('httponly', response.headers['set-cookie'].lower())
        self.assertEqual(self.client.get('/api/account/me').json()['user']['username'], '学生_01')

    def test_password_is_not_stored_in_plain_text(self):
        username = name()
        self.register(username, 'plain-secret-123')
        with closing(accounts._connect()) as conn:
            stored = conn.execute('SELECT password_hash FROM users WHERE username=?', (username,)).fetchone()[0]
        self.assertNotIn('plain-secret-123', stored)
        self.assertTrue(stored.startswith('scrypt$'))

    def test_duplicate_username_and_bad_input_are_rejected(self):
        username = name()
        self.assertEqual(self.register(username).status_code, 200)
        self.assertEqual(self.register(username.upper()).status_code, 409)
        self.assertEqual(self.register('ab').status_code, 400)
        self.assertEqual(self.register(name(), 'short').status_code, 400)

    def test_login_logout_and_wrong_password(self):
        username = name()
        self.register(username)
        self.client.post('/api/account/logout')
        self.assertIsNone(self.client.get('/api/account/me').json()['user'])
        bad = self.client.post('/api/account/login', json={'username': username, 'password': 'wrong-password'})
        self.assertEqual(bad.status_code, 401)
        good = self.client.post('/api/account/login', json={'username': username, 'password': 'correct-horse'})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(self.client.get('/api/account/me').json()['user']['username'], username)

    def test_repeated_wrong_passwords_are_throttled(self):
        username = name()
        self.register(username)
        for _ in range(accounts.FAIL_LIMIT):
            self.client.post('/api/account/login', json={'username': username, 'password': 'wrong-password'})
        locked = self.client.post('/api/account/login', json={'username': username, 'password': 'correct-horse'})
        self.assertEqual(locked.status_code, 429)

    def test_upload_requires_login(self):
        response = self.client.post('/api/format', files={'file': ('paper.docx', b'PK')},
                                    data={'ruleId': 'school:hkjjxy', 'title': 't', 'authorName': 'a'})
        self.assertEqual(response.status_code, 401)
        quote = self.client.post('/api/tabword/billing/quote', data={'operation': 'detect_text', 'params': '{}'})
        self.assertEqual(quote.status_code, 401)

    def upload(self, title='我的论文'):
        with patch.object(web, '_run_school'):
            jid = self.client.post('/api/format', files={'file': ('paper.docx', b'PK')},
                                   data={'ruleId': 'school:hkjjxy', 'title': title, 'authorName': 'a',
                                         'ruleLabel': '海口经济学院'}).json()['job']
        self.addCleanup(lambda: shutil.rmtree(web.JOBS.pop(jid, {}).get('tmp', ''), ignore_errors=True))
        web.JOBS[jid]['state'] = 'done'
        return jid

    def pay(self, ref, order='ORDER0001', paid_at=None):
        paid_at = int(time.time()) if paid_at is None else paid_at
        with closing(alipay._connect()) as conn, conn:
            conn.execute("""INSERT INTO payment_orders (out_trade_no,job_id,amount_cents,status,qr_code,created_at,paid_at)
                            VALUES (?,?,2999,'TRADE_SUCCESS','',?,?)""", (order, ref, paid_at, paid_at))

    def test_history_lists_only_paid_uploads_of_this_account(self):
        self.register()
        jid = self.upload()
        self.assertEqual(self.client.get('/api/account/history').json()['items'], [])
        self.pay(jid)
        items = self.client.get('/api/account/history').json()['items']
        self.assertEqual([(i['ref'], i['name'], i['detail'], i['order_id'], i['state']) for i in items],
                         [(jid, '我的论文', '海口经济学院', 'ORDER0001', 'done')])
        self.assertEqual(items[0]['expires_at'] - items[0]['paid_at'], alipay.RESULT_TTL)

        other = TestClient(web.app)
        other.post('/api/account/register', json={'username': name(), 'password': 'correct-horse'})
        self.assertEqual(other.get('/api/account/history').json()['items'], [])

    def test_paid_text_service_is_listed(self):
        user_id = self.register().json()['user']['id']
        from core import service_billing
        with patch.dict(os.environ, {'TABWORD_ENABLE_CALLS': 'true', 'TABWORD_API_KEY': 'offline-only'}):
            quote = service_billing.quote('detect_text', {'text': '论文正文', 'task_platform': 'test'})
        accounts.record_upload(user_id, 'tabword', quote['quote_id'], '论文正文', 'detect_text')
        self.pay(quote['quote_id'], 'ORDER0002')
        items = self.client.get('/api/account/history').json()['items']
        self.assertEqual([(i['kind'], i['name'], i['detail']) for i in items], [('tabword', '论文正文', 'detect_text')])

    def test_paid_results_are_deleted_after_24_hours(self):
        self.register()
        jid = self.upload()
        tmp = web.JOBS[jid]['tmp']
        self.pay(jid, paid_at=int(time.time()) - alipay.RESULT_TTL - 1)
        self.assertEqual(self.client.get('/api/account/history').json()['items'][0]['state'], 'expired')
        self.assertEqual(self.client.get(f'/api/format/jobs/{jid}').status_code, 410)
        self.assertNotIn(jid, web.JOBS)
        self.assertFalse(os.path.exists(tmp))

    def test_sweeper_deletes_expired_paid_results(self):
        self.register()
        fresh, old = self.upload('新'), self.upload('旧')
        self.pay(fresh, 'ORDER0003')
        self.pay(old, 'ORDER0004', paid_at=int(time.time()) - alipay.RESULT_TTL - 1)
        web._purge_expired()
        self.assertIn(fresh, web.JOBS)
        self.assertNotIn(old, web.JOBS)


if __name__ == '__main__':
    unittest.main()
