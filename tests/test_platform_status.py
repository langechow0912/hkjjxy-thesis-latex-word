# -*- coding: utf-8 -*-
"""检测平台是否可用是动态的：以实际报错为准，连续出错才暂停，到点自动放行，成功就恢复。全部离线。"""
import io
import json
import time
from unittest.mock import patch

from core import service_billing as billing, tabword
from tests.test_points import PointsBase, make_user


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


class Base(PointsBase):
    def setUp(self):
        super().setUp()
        self.as_user(make_user(2000))
        billing._PLATFORM_FAILS.clear()

    def quote(self, platform):
        values = {'text': '论文正文' * 30, 'task_platform': platform}
        return self.client.post('/api/tabword/billing/quote', data={
            'operation': 'detect_text', 'params': json.dumps(values, ensure_ascii=False)})

    def run_detect(self, platform, reply=None, error=None):
        quote = self.quote(platform)
        self.assertEqual(quote.status_code, 200, quote.text)
        paid = self.client.post('/api/points/pay', json={'quote_id': quote.json()['quote_id']}).json()
        values = {'text': '论文正文' * 30, 'task_platform': platform}
        data = {'params': json.dumps(values, ensure_ascii=False), 'submit': 'true', 'payment_order': paid['order_id']}
        if error:
            with patch.object(tabword, 'call', side_effect=error):
                return self.client.post('/api/tabword/detect_text', data=data)
        with provider_reply(reply):
            return self.client.post('/api/tabword/detect_text', data=data)


class NothingIsHardCoded(Base):
    def test_no_platform_is_unavailable_by_default(self):
        self.assertEqual(billing.unavailable_platforms(), {})
        self.assertEqual(self.client.get('/api/tabword').json()['unavailable_platforms'], {})

    def test_gezida_works_when_the_provider_has_fixed_it(self):
        self.assertEqual(self.quote('格子达_zh').status_code, 200)
        res = self.run_detect('格子达_zh', {'code': 0, 'message': 'success', 'data': {'score': 12}})
        self.assertEqual(res.status_code, 200)


class InterfaceErrors(Base):
    def test_first_interface_error_tells_the_user_to_retry_but_keeps_the_platform_open(self):
        res = self.run_detect('维普_zh', {'code': 500, 'message': '服务繁忙'})
        self.assertEqual(res.status_code, 502)
        detail = res.json()['detail']
        self.assertIn('「维普」平台接口暂时不可用，请稍候重试，或选择其他检测平台', detail)
        self.assertIn('积分已全额退还', detail)
        self.assertNotIn('已暂停', detail)
        self.assertEqual(billing.unavailable_platforms(), {})
        self.assertEqual(self.quote('维普_zh').status_code, 200)

    def test_two_failures_in_a_row_pause_the_platform_for_a_short_while(self):
        self.run_detect('维普_zh', {'code': 500, 'message': '服务繁忙'})
        res = self.run_detect('维普_zh', {'code': 500, 'message': '服务繁忙'})
        self.assertIn('已暂停该平台，稍后会自动恢复', res.json()['detail'])
        self.assertIn('维普_zh', billing.unavailable_platforms())
        blocked = self.quote('维普_zh')
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('平台接口暂时不可用，请稍候重试', blocked.json()['detail'])
        self.assertEqual(self.quote('知网_zh').status_code, 200)

    def test_a_success_in_between_resets_the_count(self):
        self.run_detect('维普_zh', {'code': 500, 'message': '服务繁忙'})
        self.run_detect('维普_zh', {'code': 0, 'message': 'success', 'data': {'score': 3}})
        self.run_detect('维普_zh', {'code': 500, 'message': '服务繁忙'})
        self.assertEqual(billing.unavailable_platforms(), {})

    def test_failures_older_than_the_window_do_not_add_up(self):
        self.run_detect('维普_zh', {'code': 500, 'message': '服务繁忙'})
        later = time.time() + billing.FAIL_WINDOW + 5
        with patch('core.service_billing.time.time', return_value=later):
            note = billing.note_platform_failure('detect_text', {'task_platform': '维普_zh'}, tabword.TabwordError('x', 502))
        self.assertNotIn('已暂停', note)

    def test_timeouts_and_broken_connections_count_as_interface_errors(self):
        self.run_detect('朱雀_zh', error=tabword.TabwordError('服务商连接中断，提交状态可能未确认；未自动重试', 502))
        res = self.run_detect('朱雀_zh', error=tabword.TabwordError('服务商在 180 秒内没有返回结果，已放弃等待', 504))
        self.assertIn('朱雀_zh', billing.unavailable_platforms())
        self.assertIn('请稍候重试', res.json()['detail'])

    def test_missing_preset_pauses_at_once_because_it_is_a_certain_fault(self):
        res = self.run_detect('Paperyy_zh', {'code': 500, 'message': '检测预设不存在: aigcmove_tp_s_1'})
        self.assertIn('Paperyy_zh', billing.unavailable_platforms())
        self.assertIn('已暂停该平台', res.json()['detail'])

    def test_the_users_own_mistakes_never_blame_the_platform(self):
        for _ in range(3):
            res = self.run_detect('大雅_zh', {'code': 400, 'message': '文件内容为空'})
            self.assertEqual(res.status_code, 422)
            self.assertNotIn('接口暂时不可用', res.json()['detail'])
        self.assertEqual(billing.unavailable_platforms(), {})

    def test_other_http_client_errors_are_not_counted_either(self):
        for _ in range(3):
            self.run_detect('华宸_zh', error=tabword.TabwordError('服务商返回 HTTP 400，未自动重试', 400))
        self.assertEqual(billing.unavailable_platforms(), {})


class SelfHealing(Base):
    def pause(self, platform='维普_zh'):
        self.run_detect(platform, {'code': 500, 'message': '检测预设不存在: x'})

    def test_the_pause_ends_by_itself(self):
        self.pause()
        self.assertIn('维普_zh', billing.unavailable_platforms())
        with patch('core.service_billing.time.time', return_value=time.time() + billing.AUTO_PAUSE_SECONDS + 1):
            self.assertEqual(billing.unavailable_platforms(), {})

    def test_after_the_pause_one_more_failure_pauses_again_immediately(self):
        self.pause()
        later = time.time() + billing.AUTO_PAUSE_SECONDS + 1
        with patch('core.service_billing.time.time', return_value=later):
            billing.note_platform_failure('detect_text', {'task_platform': '维普_zh'}, tabword.TabwordError('服务繁忙', 502))
            self.assertIn('维普_zh', billing.unavailable_platforms())

    def test_after_the_pause_a_success_clears_everything(self):
        self.pause()
        with patch('core.service_billing.time.time', return_value=time.time() + billing.AUTO_PAUSE_SECONDS + 1):
            billing.note_platform_success('detect_text', {'task_platform': '维普_zh'})
            self.assertEqual(billing.unavailable_platforms(), {})
            self.assertEqual(billing._PLATFORM_FAILS, {})

    def test_a_success_lifts_an_automatic_pause_immediately(self):
        self.pause()
        billing.note_platform_success('detect_text', {'task_platform': '维普_zh'})
        self.assertEqual(billing.unavailable_platforms(), {})

    def test_default_pause_is_three_minutes(self):
        self.assertEqual(billing.AUTO_PAUSE_SECONDS, 180)


class AdminSwitch(Base):
    def test_admin_can_stop_a_platform_by_hand_and_it_stays_stopped(self):
        self.assertEqual(self.client.put('/api/admin/detection-platforms', json={'platform': '维普_zh', 'disabled': True}).status_code, 401)
        res = self.client.put('/api/admin/detection-platforms', auth=self.auth,
                              json={'platform': '维普_zh', 'disabled': True, 'reason': '维护中'})
        self.assertEqual(res.json()['unavailable']['维普_zh'], '维护中')
        self.assertEqual(self.quote('维普_zh').status_code, 409)
        with patch('core.service_billing.time.time', return_value=time.time() + 30 * 24 * 3600):
            self.assertIn('维普_zh', billing.unavailable_platforms())     # 手动停用不会自己过期

    def test_admin_can_restore_it(self):
        self.client.put('/api/admin/detection-platforms', auth=self.auth, json={'platform': '维普_zh', 'disabled': True})
        res = self.client.put('/api/admin/detection-platforms', auth=self.auth, json={'platform': '维普_zh', 'disabled': False})
        self.assertEqual(res.json()['unavailable'], {})
        self.assertEqual(self.quote('维普_zh').status_code, 200)
        listing = self.client.get('/api/admin/detection-platforms', auth=self.auth).json()
        self.assertEqual(listing['unavailable'], {})


class Scope(Base):
    def test_text_rewrite_is_never_affected(self):
        self.assertIsNone(billing.check_platform('paraphrase_stream', {'preset': 'x', 'task_platform': '维普_zh'}))
        self.assertEqual(billing.note_platform_failure('paraphrase_stream', {'task_platform': '维普_zh'},
                                                       tabword.TabwordError('x', 502)), '')


if __name__ == '__main__':
    import unittest
    unittest.main()
