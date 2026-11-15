"""多发信账号：按文件顺序优先，失败自动换下一个，坏账号短时间不再被先试。全部离线（假 SMTP）。"""
import json
import os
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import mailer


class FakeSMTP:
    sent, logins, fail = [], [], {}

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        FakeSMTP.logins.append(user)
        if user in FakeSMTP.fail:
            raise FakeSMTP.fail[user]
        self.user = user

    def send_message(self, message):
        FakeSMTP.sent.append((self.user, str(message['From']), str(message['To']), str(message['Subject'])))


AUTH = smtplib.SMTPAuthenticationError(535, b'Error: authentication failed')


def entry(user, host='smtp.test', **extra):
    return dict({'host': host, 'port': 465, 'security': 'ssl', 'user': user, 'password': 'secret-' + user}, **extra)


class MailerFailover(unittest.TestCase):
    def setUp(self):
        FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.fail = [], [], {}
        mailer._cooldown.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'accounts.json'
        patches = [
            patch.dict(os.environ, {'SMTP_ACCOUNTS_FILE': str(self.path), 'SMTP_HOST': '', 'SMTP_USER': '',
                                    'SMTP_PASSWORD': '', 'SMTP_PASSWORD_FILE': ''}),
            patch('core.mailer.smtplib.SMTP_SSL', FakeSMTP),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mailer._cooldown.clear)

    def write(self, accounts):
        self.path.write_text(json.dumps(accounts), encoding='utf-8')

    def send(self):
        return mailer.deliver(mailer._message('to@qq.com', 'Paper Layout 登录验证码', 'x'))

    def test_first_account_in_file_is_used_first(self):
        self.write([entry('ent@corp.com'), entry('a@163.com'), entry('last@qq.com')])
        self.assertEqual(self.send(), 'ent@corp.com')
        self.assertEqual(FakeSMTP.logins, ['ent@corp.com'])

    def test_from_header_matches_account_and_name_is_paper_layout(self):
        self.write([entry('a@163.com', **{'from': 'noreply@163.com'})])
        self.send()
        user, sender, to, subject = FakeSMTP.sent[0]
        self.assertEqual((user, to, subject), ('a@163.com', 'to@qq.com', 'Paper Layout 登录验证码'))
        self.assertIn('Paper Layout', sender)
        self.assertIn('noreply@163.com', sender)

    def test_falls_back_in_order_and_each_mail_has_the_right_sender(self):
        FakeSMTP.fail = {'ent@corp.com': AUTH, 'a@163.com': AUTH}
        self.write([entry('ent@corp.com'), entry('a@163.com'), entry('wecom@w.work'), entry('last@qq.com')])
        self.assertEqual(self.send(), 'wecom@w.work')
        self.assertEqual(FakeSMTP.logins, ['ent@corp.com', 'a@163.com', 'wecom@w.work'])
        self.assertIn('wecom@w.work', FakeSMTP.sent[0][1])
        self.assertNotIn('ent@corp.com', FakeSMTP.sent[0][1])

    def test_failed_account_is_skipped_on_the_next_mail(self):
        FakeSMTP.fail = {'a@163.com': AUTH}
        self.write([entry('a@163.com'), entry('b@qq.com')])
        self.send()
        FakeSMTP.logins.clear()
        self.assertEqual(self.send(), 'b@qq.com')
        self.assertEqual(FakeSMTP.logins, ['b@qq.com'])          # a@163.com 还在冷却，不再先撞一次

    def test_cooling_account_is_still_tried_as_last_resort(self):
        FakeSMTP.fail = {'a@163.com': AUTH, 'b@qq.com': OSError('connection refused')}
        self.write([entry('a@163.com'), entry('b@qq.com')])
        with self.assertRaises(mailer.MailError):
            self.send()
        FakeSMTP.fail = {'b@qq.com': OSError('connection refused')}      # 163 恢复了
        self.assertEqual(self.send(), 'a@163.com')                         # 虽然在冷却，但别的都不行时仍会试它

    def test_all_failed_raises_with_every_account_error_but_no_password(self):
        FakeSMTP.fail = {'a@163.com': AUTH, 'b@qq.com': OSError('timed out')}
        self.write([entry('a@163.com'), entry('b@qq.com')])
        with self.assertRaises(mailer.MailError) as caught:
            self.send()
        detail = caught.exception.detail
        self.assertIn('a@163.com: SMTPAuthenticationError', detail)
        self.assertIn('b@qq.com: OSError', detail)
        self.assertNotIn('secret-', detail)

    def test_recipient_refused_does_not_cool_the_account_down(self):
        FakeSMTP.fail = {}
        self.write([entry('a@163.com')])
        mailer._cool_down({'user': 'a@163.com'}, smtplib.SMTPRecipientsRefused({'x@y': (550, b'no')}))
        self.assertEqual(mailer._cooldown, {})

    def test_account_file_is_reread_so_new_account_on_top_takes_over(self):
        self.write([entry('a@163.com')])
        self.assertEqual(self.send(), 'a@163.com')
        self.write([entry('new@corp.com'), entry('a@163.com')])
        self.assertEqual(self.send(), 'new@corp.com')

    def test_disabled_and_incomplete_entries_are_skipped(self):
        self.write([entry('off@x.com', enabled=False), {'host': 'smtp.test', 'user': 'nopass@x.com'}, entry('ok@x.com')])
        self.assertEqual([a['user'] for a in mailer.accounts()], ['ok@x.com'])

    def test_password_file_and_default_port(self):
        secret = Path(self.tmp.name) / 'pw'
        secret.write_text('from-file\n', encoding='utf-8')
        self.write([{'host': 'smtp.test', 'user': 'a@x.com', 'password_file': str(secret), 'security': 'starttls'}])
        account, = mailer.accounts()
        self.assertEqual((account['password'], account['port'], account['sender']), ('from-file', 587, 'a@x.com'))

    def test_broken_file_falls_back_to_legacy_env_account(self):
        self.path.write_text('{ not json', encoding='utf-8')
        env = {'SMTP_HOST': 'smtp.legacy', 'SMTP_USER': 'old@163.com', 'SMTP_PASSWORD': 'x', 'SMTP_PORT': '465'}
        with patch.dict(os.environ, env):
            self.assertEqual([a['user'] for a in mailer.accounts()], ['old@163.com'])
            self.assertEqual(self.send(), 'old@163.com')

    def test_missing_file_uses_legacy_env_and_nothing_configured_is_false(self):
        self.path.unlink(missing_ok=True)
        self.assertFalse(mailer.configured())
        with patch.dict(os.environ, {'SMTP_HOST': 'h', 'SMTP_USER': 'u@x.com', 'SMTP_PASSWORD': 'p'}):
            self.assertTrue(mailer.configured())

    def test_wrapped_accounts_object_is_accepted(self):
        self.write({'accounts': [entry('a@163.com')]})
        self.assertEqual([a['user'] for a in mailer.accounts()], ['a@163.com'])


if __name__ == '__main__':
    unittest.main()
