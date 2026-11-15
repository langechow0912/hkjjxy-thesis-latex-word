# -*- coding: utf-8 -*-
"""报警：发给谁、不刷屏、不抛异常、SMTP 抽风会重试；各个出问题的地方都会报警。全部离线，不真发邮件。"""
import json
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import alerts, mailer, service_billing as billing, tabword
from tests.test_platform_status import Base as PlatformBase, provider_reply


class AlertsOn:
    """打开报警，把发信换成记录，线程改成当场执行。"""

    def setUp(self):
        super().setUp()
        self.sent = []
        for patcher in (patch.dict('os.environ', {'ALERTS_ENABLED': 'true'}),
                        patch.dict(alerts._last, clear=True),
                        patch.object(alerts, '_spawn', lambda fn, *args: fn(*args)),
                        patch.object(alerts.time, 'sleep', lambda _s: None),
                        patch.object(mailer, 'send_text', side_effect=lambda to, subject, body: self.sent.append((to, subject, body)))):
            patcher.start(); self.addCleanup(patcher.stop)

    def subjects(self):
        return [s for _, s, _ in self.sent]


class Basics(AlertsOn, PlatformBase):
    def test_goes_to_the_configured_address_with_a_clear_subject(self):
        self.assertTrue(alerts.alert(('x',), '测试问题', '细节内容'))
        to, subject, body = self.sent[0]
        self.assertEqual(to, '529813342@qq.com')
        self.assertEqual(subject, '[论文站报警] 测试问题')
        self.assertIn('细节内容', body)
        self.assertIn('北京时间', body)

    def test_address_can_be_changed(self):
        with patch.dict('os.environ', {'ALERT_EMAIL': 'ops@example.com'}):
            alerts.alert(('x',), 'a')
        self.assertEqual(self.sent[0][0], 'ops@example.com')

    def test_same_problem_is_sent_once_then_summarised(self):
        self.assertTrue(alerts.alert(('p',), '平台坏了'))
        self.assertFalse(alerts.alert(('p',), '平台坏了'))
        self.assertFalse(alerts.alert(('p',), '平台坏了'))
        self.assertEqual(len(self.sent), 1)
        later = time.time() + alerts.QUIET_SECONDS + 1
        with patch.object(alerts.time, 'time', return_value=later):
            self.assertTrue(alerts.alert(('p',), '平台坏了'))
        self.assertIn('又发生了 2 次', self.sent[1][2])

    def test_different_problems_are_not_merged(self):
        alerts.alert(('a',), '一'); alerts.alert(('b',), '二')
        self.assertEqual(self.subjects(), ['[论文站报警] 一', '[论文站报警] 二'])

    def test_smtp_hiccups_are_retried(self):
        calls = []

        def flaky(to, subject, body):
            calls.append(1)
            if len(calls) < 3:
                error = mailer.MailError('x'); error.detail = '535 authentication failed'; raise error
        with patch.object(mailer, 'send_text', side_effect=flaky):
            alerts.alert(('r',), '重试')
        self.assertEqual(len(calls), 3)

    def test_a_broken_mailer_never_breaks_the_caller(self):
        with patch.object(mailer, 'send_text', side_effect=RuntimeError('boom')):
            self.assertTrue(alerts.alert(('z',), '不会抛'))
        with patch.object(alerts, '_body', side_effect=RuntimeError('boom')):
            self.assertFalse(alerts.alert(('y',), '也不会抛'))

    def test_can_be_switched_off(self):
        with patch.dict('os.environ', {'ALERTS_ENABLED': 'false'}):
            self.assertFalse(alerts.alert(('off',), 'x'))
        self.assertEqual(self.sent, [])


class WhereAlertsFire(AlertsOn, PlatformBase):
    def test_a_failed_detection_alerts_with_order_and_reason(self):
        res = self.run_detect('维普_zh', {'code': 500, 'message': '服务繁忙'})
        self.assertEqual(res.status_code, 502)
        subject, body = next((s, b) for _, s, b in self.sent if '文本检测失败' in s)
        self.assertIn('服务繁忙', body)
        self.assertIn('TW-', body)
        self.assertIn('已自动退还积分', body)
        self.assertNotIn('论文正文', body)                                  # 不把用户正文发出去

    def test_a_paused_platform_alerts(self):
        self.run_detect('维普_zh', {'code': 500, 'message': '检测预设不存在: x'})
        self.assertTrue(any('检测平台「维普」接口不可用' in s for s in self.subjects()), self.subjects())

    def test_the_users_own_mistake_is_not_an_alert_about_the_platform(self):
        self.run_detect('大雅_zh', {'code': 400, 'message': '文件内容为空'})
        self.assertFalse(any('检测平台' in s for s in self.subjects()), self.subjects())

    def test_stuck_orders_alert(self):
        quote = self.quote('知网_zh').json()
        self.client.post('/api/points/pay', json={'quote_id': quote['quote_id']})
        from contextlib import closing
        with closing(billing.connect()) as conn, conn:
            conn.execute("UPDATE service_quotes SET state='processing', started_at=? WHERE id=?",
                         (time.time() - 3600, quote['quote_id']))
        billing.fail_stuck()
        body = next(b for _, s, b in self.sent if '处理中断' in s)
        self.assertIn(quote['quote_id'], body)

    def test_verification_mail_failure_alerts_without_leaking_the_code(self):
        def fail(message):
            error = mailer.MailError('x'); error.detail = 'SMTPAuthenticationError: 535'; raise error
        with patch.object(mailer, 'configured', return_value=True), patch.object(mailer, 'deliver', side_effect=fail):
            with self.assertRaises(mailer.MailError):
                mailer.send_code('someone@qq.com', '123456')
        subject, body = next((s, b) for _, s, b in self.sent if '验证码邮件发送失败' in s)
        self.assertIn('535', body)
        self.assertIn('qq.com', body)
        self.assertNotIn('123456', body)
        self.assertNotIn('someone@', body)

    def test_an_unhandled_server_error_alerts_and_still_returns_500(self):
        from web import app as web
        client = TestClient(web.app, raise_server_exceptions=False)
        with patch.object(billing, 'prices', side_effect=RuntimeError('数据库坏了')):
            self.assertEqual(client.get('/api/tabword').status_code, 500)
        subject, body = next((s, b) for _, s, b in self.sent if '接口异常' in s)
        self.assertIn('GET /api/tabword', subject)
        self.assertIn('数据库坏了', body)

    def test_ids_in_paths_are_merged_into_one_kind_of_problem(self):
        from web import app as web
        client = TestClient(web.app, raise_server_exceptions=False)
        with patch('core.alipay.sync_order', side_effect=RuntimeError('x')):
            client.get('/api/tabword/billing/TW-%s/PP1234567890ABCDEF1234' % ('a' * 32))
            client.get('/api/tabword/billing/TW-%s/PP1234567890ABCDEF9999' % ('b' * 32))
        self.assertEqual(len([s for s in self.subjects() if '接口异常' in s]), 1, self.subjects())


if __name__ == '__main__':
    import unittest
    unittest.main()
