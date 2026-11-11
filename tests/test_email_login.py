# -*- coding: utf-8 -*-
"""邮箱验证码登录：一次性、限次、限频、不泄露账号是否存在。邮件用假发送器，不联网。"""
import os
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import accounts, mailer, points
from web import app as web
from web.account_routes import current_user
from tests import signed_in

REAL_SEND = mailer.send_code        # setUp 会把它换成假的；生产分支的用例要用真的


def mail():
    return 'u%s@example.com' % uuid.uuid4().hex[:10]


class EmailLogin(unittest.TestCase):
    def setUp(self):
        web.app.dependency_overrides.pop(current_user, None)
        self.addCleanup(web.app.dependency_overrides.__setitem__, current_user, signed_in)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'ALIPAY_ORDER_DB': os.path.join(self.temp.name, 'orders.db')})
        env.start(); self.addCleanup(env.stop)
        # 每个用例用独立的账号库，积分赠送的「每日上限」等统计才不会互相污染
        db = patch.object(accounts, '_db_path', lambda: Path(self.temp.name) / 'accounts.sqlite3')
        db.start(); self.addCleanup(db.stop)
        self.sent = []
        patcher = patch.object(mailer, 'send_code',
                               side_effect=lambda to, code, purpose='登录': self.sent.append((to, code, purpose)))
        patcher.start(); self.addCleanup(patcher.stop)
        self.client = TestClient(web.app)

    def send(self, email):
        return self.client.post('/api/account/email/send', json={'email': email})

    def login(self, email, code):
        return self.client.post('/api/account/email/login', json={'email': email, 'code': code})

    def last_code(self):
        return self.sent[-1][1]

    def age_codes(self, seconds=3600):
        with closing(accounts._connect()) as conn, conn:
            conn.execute('UPDATE email_codes SET created_at=created_at-?', (seconds,))

    def test_code_login_creates_account_and_sets_cookie(self):
        email = mail()
        self.assertEqual(self.send(email).status_code, 200)
        res = self.login(email, self.last_code())
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()['created'])
        self.assertIn('HttpOnly', res.headers['set-cookie'])
        me = self.client.get('/api/account/me').json()['user']
        self.assertEqual(me['email'], email)
        self.assertEqual(me['points'], 0)

    def test_session_cookie_lasts_seven_days(self):
        email = mail(); self.send(email)
        res = self.login(email, self.last_code())
        self.assertIn('Max-Age=%d' % (7 * 86400), res.headers['set-cookie'])
        self.assertEqual(accounts.SESSION_DAYS, 7)

    def test_sessions_older_than_seven_days_are_rejected_even_if_issued_for_30(self):
        email = mail(); self.send(email); self.login(email, self.last_code())
        self.assertIsNotNone(self.client.get('/api/account/me').json()['user'])
        with closing(accounts._connect()) as conn, conn:        # 模拟旧版本签发的 30 天会话，已经用了 8 天
            conn.execute('UPDATE user_sessions SET created_at=?, expires_at=?',
                         (int(time.time()) - 8 * 86400, int(time.time()) + 22 * 86400))
        self.assertIsNone(self.client.get('/api/account/me').json()['user'])

    def test_second_login_reuses_the_same_account(self):
        email = mail()
        self.send(email)
        first = self.login(email, self.last_code()).json()['user']['id']
        self.client.post('/api/account/logout')
        self.age_codes()
        self.send(email)
        res = self.login(email, self.last_code())
        self.assertFalse(res.json()['created'])
        self.assertEqual(res.json()['user']['id'], first)

    def test_wrong_code_then_code_is_single_use(self):
        email = mail(); self.send(email); good = self.last_code()
        bad = '000000' if good != '000000' else '111111'
        self.assertEqual(self.login(email, bad).status_code, 401)
        self.assertEqual(self.login(email, good).status_code, 200)
        self.assertEqual(self.login(email, good).status_code, 401)

    def test_five_wrong_guesses_burn_the_code(self):
        email = mail(); self.send(email); good = self.last_code()
        bad = '000000' if good != '000000' else '111111'
        for _ in range(accounts.CODE_MAX_ATTEMPTS):
            self.assertEqual(self.login(email, bad).status_code, 401)
        self.assertEqual(self.login(email, good).status_code, 401)

    def test_expired_code_is_rejected(self):
        email = mail(); self.send(email)
        with closing(accounts._connect()) as conn, conn:
            conn.execute('UPDATE email_codes SET expires_at=?', (int(time.time()) - 1,))
        self.assertEqual(self.login(email, self.last_code()).status_code, 401)

    def test_cooldown_and_hourly_limit(self):
        email = mail()
        self.assertEqual(self.send(email).status_code, 200)
        self.assertEqual(self.send(email).status_code, 429)
        for _ in range(accounts.CODE_HOURLY - 1):
            self.age_codes(120)
            self.assertEqual(self.send(email).status_code, 200)
        self.age_codes(120)
        self.assertEqual(self.send(email).status_code, 429)

    def test_plus_tag_is_the_same_mailbox(self):
        self.assertEqual(accounts.normalize_email('A.B+tag@Example.com'), ('a.b+tag@example.com', 'a.b@example.com'))
        local = 'p' + uuid.uuid4().hex[:8]
        self.send(local + '@example.com')
        a = self.login(local + '@example.com', self.last_code()).json()['user']['id']
        self.client.post('/api/account/logout')
        self.age_codes()
        self.send(local + '+x@example.com')
        b = self.login(local + '+x@example.com', self.last_code()).json()['user']['id']
        self.assertEqual(a, b)

    def test_bad_address_is_rejected_and_nothing_sent(self):
        for bad in ('', 'nope', 'a@b', 'a b@example.com', 'x@' + 'a' * 250 + '.com'):
            self.assertEqual(self.send(bad).status_code, 400, bad)
        self.assertEqual(self.sent, [])

    def test_response_does_not_reveal_whether_account_exists(self):
        known = mail(); self.send(known); self.login(known, self.last_code())
        self.client.post('/api/account/logout')
        self.age_codes()
        self.assertEqual(self.send(known).json(), self.send(mail()).json())

    def test_code_is_not_stored_in_plain_text(self):
        email = mail(); self.send(email); code = self.last_code()
        with closing(accounts._connect()) as conn:
            rows = conn.execute('SELECT * FROM email_codes').fetchall()
        self.assertNotIn(code, ' '.join(str(tuple(r)) for r in rows))

    def test_mail_failure_returns_502_and_does_not_burn_the_cooldown(self):
        email = mail()
        with patch.object(mailer, 'send_code', side_effect=mailer.MailError('发送失败')):
            self.assertEqual(self.send(email).status_code, 502)
        self.assertEqual(self.send(email).status_code, 200)

    def test_production_without_smtp_refuses_instead_of_logging_the_code(self):
        with patch.dict(os.environ, {'THESIS_ENV': 'production', 'SMTP_HOST': '', 'SMTP_USER': '',
                                     'SMTP_PASSWORD': '', 'SMTP_PASSWORD_FILE': ''}):
            with self.assertRaises(mailer.MailError) as caught:
                REAL_SEND('a@example.com', '123456')
        self.assertEqual(caught.exception.status, 503)

    def test_development_without_smtp_logs_the_code(self):
        with patch.dict(os.environ, {'THESIS_ENV': '', 'SMTP_HOST': '', 'SMTP_USER': '',
                                     'SMTP_PASSWORD': '', 'SMTP_PASSWORD_FILE': ''}):
            with self.assertLogs('core.mailer', level='WARNING') as logs:
                REAL_SEND('a@example.com', '654321')
        self.assertIn('654321', '\n'.join(logs.output))

    def test_bind_email_to_password_account(self):
        name = 'bind' + uuid.uuid4().hex[:6]
        user = self.client.post('/api/account/register', json={'username': name, 'password': 'correct-horse'}).json()['user']
        self.assertIsNone(user.get('email'))
        email = mail()
        self.assertEqual(self.client.post('/api/account/email/bind/send', json={'email': email}).status_code, 200)
        res = self.client.post('/api/account/email/bind/verify', json={'email': email, 'code': self.last_code()})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.client.get('/api/account/me').json()['user']['email'], email)

    def test_bind_to_a_taken_email_sends_nothing_and_fails_generically(self):
        taken = mail(); self.send(taken); self.login(taken, self.last_code())
        self.client.post('/api/account/logout')
        self.client.post('/api/account/register', json={'username': 'b' + uuid.uuid4().hex[:6], 'password': 'correct-horse'})
        self.age_codes(120)           # 避开 60 秒冷却，单测「已被占用」这个分支
        before = len(self.sent)
        res = self.client.post('/api/account/email/bind/send', json={'email': taken})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(self.sent), before)
        res = self.client.post('/api/account/email/bind/verify', json={'email': taken, 'code': '123456'})
        self.assertEqual(res.status_code, 401)

    def test_bind_requires_login(self):
        self.client.post('/api/account/logout')
        self.assertEqual(self.client.post('/api/account/email/bind/send', json={'email': mail()}).status_code, 401)

    def test_email_account_cannot_log_in_with_a_password(self):
        email = mail(); self.send(email); self.login(email, self.last_code())
        self.client.post('/api/account/logout')
        for password in ('!', 'correct-horse', '        '):
            res = self.client.post('/api/account/login', json={'username': email, 'password': password})
            self.assertEqual(res.status_code, 401, password)

    def test_signup_bonus_only_for_verified_new_email_and_only_once(self):
        points.save_settings({'enabled': True, 'signup_bonus': 30, 'signup_bonus_daily_cap': 100,
                              'topup_packages': [5, 10]})
        name = 'pw' + uuid.uuid4().hex[:6]
        pw = self.client.post('/api/account/register', json={'username': name, 'password': 'correct-horse'}).json()['user']
        self.assertEqual(points.balance(pw['id']), 0)
        self.client.post('/api/account/logout')
        email = mail(); self.send(email); self.login(email, self.last_code())
        self.assertEqual(self.client.get('/api/account/me').json()['user']['points'], 30)
        self.client.post('/api/account/logout')
        self.age_codes()
        self.send(email); self.login(email, self.last_code())
        self.assertEqual(self.client.get('/api/account/me').json()['user']['points'], 30)

    def test_signup_bonus_respects_daily_cap(self):
        points.save_settings({'enabled': True, 'signup_bonus': 10, 'signup_bonus_daily_cap': 1,
                              'topup_packages': [5]})
        got = []
        for _ in range(2):
            email = mail(); self.send(email); self.login(email, self.last_code())
            got.append(self.client.get('/api/account/me').json()['user']['points'])
            self.client.post('/api/account/logout')
        self.assertEqual(sorted(got), [0, 10])


if __name__ == '__main__':
    unittest.main()
