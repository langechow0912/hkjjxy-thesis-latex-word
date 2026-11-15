# -*- coding: utf-8 -*-
"""AI 助手：只给待确认的修改、不泄露正文、按实际 token 扣积分（先预留后结算，失败全额退）。假模型，离线。"""
import json
import os
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import accounts, points, profile
from tests import test_format_overrides as fo
from tests import test_points as tp
from web import app as web
from web import format_ai_routes as routes

SECRET = '独一无二的未发表正文句子，绝对不能发给外部模型，里面有具体研究结论和数据。'
SPECS = ['一、财务风险', SECRET, '偿债能力分析', SECRET, '二、防范措施', SECRET]


def reply(changes=None, text='看了一遍', tokens=3000):
    return {'content': json.dumps({'reply': text, 'changes': changes if changes is not None else []}),
            'usage': {'prompt_tokens': tokens - 300, 'completion_tokens': 300, 'total_tokens': tokens}}


class AiBase(tp.PointsBase):
    def setUp(self):
        super().setUp()
        cfg = profile.load('hkjjxy')
        cfg['dsh']['enabled'] = True
        cfg['dsh']['cost_control']['state_file'] = os.path.join(self.temp.name, 'cost.json')
        cfg['dsh']['chat'] = {'per_user_daily': 50}
        for p in (patch.object(routes, '_config', return_value=cfg),
                  patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key', 'DSH_ENABLED': 'true'})):
            p.start(); self.addCleanup(p.stop)
        self.prompts, self.answers, self.kwargs = [], [reply()], []

        async def fake_chat(client, prompt, system=None, **kwargs):
            self.prompts.append(prompt)
            self.kwargs.append((kwargs, client.reasoning_effort))
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if isinstance(answer, Exception):
                raise answer
            return answer
        p = patch('core.dsh.client.DeepSeekClient.chat', fake_chat)
        p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.clear)
        self.user = tp.make_user(100)
        self.as_user(self.user)
        self.analysis = self.client.post('/api/format/analyze', files={'file': ('t.docx', self.docx())}).json()
        self.aid = self.analysis['analysis_id']

    def docx(self):
        with tempfile.TemporaryDirectory() as tmp:
            return Path(fo.make(SPECS, tmp)).read_bytes()

    def clear(self):
        for key in list(routes.ANALYSES):
            routes.discard(key)

    def scan(self, overrides=None):
        return self.client.post('/api/format/analyze/%s/ai' % self.aid, json={'overrides': overrides or []})

    def chat(self, message='把偿债能力分析设为二级标题', overrides=None):
        return self.client.post('/api/format/analyze/%s/chat' % self.aid,
                                json={'message': message, 'overrides': overrides or []})

    def kinds(self):
        return [r['kind'] for r in points.ledger(self.user['id'])]


class Billing(AiBase):
    def test_charges_by_actual_tokens_and_refunds_the_unused_reserve(self):
        res = self.scan()
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body['usage']['total'], 3000)
        self.assertEqual(body['charged'], 0.6)                 # 3000 token × 0.2 积分 / 1000 token
        self.assertGreater(body['reserved'], body['charged'])
        self.assertEqual(body['balance'], 99.4)
        self.assertEqual(points.balance(self.user['id']), 99.4)
        self.assertEqual(self.kinds()[:3], ['refund', 'spend', 'admin'])

    def test_minimum_charge_applies_to_tiny_calls(self):
        self.answers = [reply(tokens=100)]
        self.assertEqual(self.scan().json()['charged'], 0.5)

    def test_charge_rounds_up_to_a_tenth_of_a_point(self):
        self.answers = [reply(tokens=4500)]                    # 0.9 积分
        self.assertEqual(self.scan().json()['charged'], 0.9)
        self.answers = [reply(tokens=4501)]                    # 0.9002 → 1.0
        self.assertEqual(self.scan().json()['charged'], 1.0)

    def test_totals_accumulate_per_analysis(self):
        self.scan(); body = self.chat().json()
        self.assertEqual(body['total_tokens'], 6000)
        self.assertEqual(body['total_charged'], 1.2)
        self.assertEqual(body['calls_left'], routes.MAX_AI_CALLS - 2)

    def test_pricing_follows_the_admin_settings(self):
        points.save_settings({'ai_points_per_1k_tokens': 1.0, 'ai_min_charge': 0.1})
        self.answers = [reply(tokens=2000)]
        self.assertEqual(self.scan().json()['charged'], 2.0)

    def test_model_error_refunds_everything(self):
        self.answers = [RuntimeError('DeepSeek API error 500: boom')]
        res = self.scan()
        self.assertEqual(res.status_code, 502)
        self.assertNotIn('boom', res.text)
        self.assertEqual(points.balance(self.user['id']), 100)
        self.assertEqual(self.kinds()[:2], ['refund', 'spend'])

    def test_unparseable_answer_refunds_everything(self):
        self.answers = [{'content': '我觉得还行', 'usage': {'total_tokens': 900}}]
        res = self.scan()
        self.assertEqual(res.status_code, 502)
        self.assertIn('全额退还', res.json()['detail'])
        self.assertEqual(points.balance(self.user['id']), 100)

    def test_insufficient_balance_stops_before_calling_the_model(self):
        poor = tp.make_user(0.2)
        self.as_user(poor)
        poor_aid = self.client.post('/api/format/analyze', files={'file': ('t.docx', self.docx())}).json()['analysis_id']
        res = self.client.post('/api/format/analyze/%s/ai' % poor_aid, json={'overrides': []})
        self.assertEqual(res.status_code, 402)
        self.assertEqual(self.prompts, [])
        self.assertEqual(points.balance(poor['id']), 0.2)

    def test_crash_after_the_model_call_still_returns_the_reserve(self):
        with patch.object(points, 'ai_settle', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.client.post('/api/format/analyze/%s/ai' % self.aid, json={'overrides': []})
        self.assertEqual(points.balance(self.user['id']), 100)

    def test_a_reserve_left_behind_by_a_dead_process_is_returned_on_the_next_balance_read(self):
        ref = 'ai-%s-9' % uuid.uuid4().hex
        points.ai_reserve(self.user['id'], ref, 5000)
        held = points.balance(self.user['id'])
        self.assertLess(held, 100)
        points.summary(self.user['id'])
        self.assertEqual(points.balance(self.user['id']), held)       # 还没超时，不动
        with closing(points._connect()) as conn, conn:
            conn.execute('UPDATE ai_holds SET created_at=created_at-?', (points.AI_HOLD_TIMEOUT + 5,))
        self.assertEqual(points.summary(self.user['id'])['balance'], 100)
        self.assertFalse(points.ai_release(ref))                      # 退过的不会再退

    def test_settle_and_release_are_single_use(self):
        ref = 'ai-%s-1' % uuid.uuid4().hex
        points.ai_reserve(self.user['id'], ref, 1000)
        points.ai_settle(ref, 1000)
        with self.assertRaises(points.PointsError):
            points.ai_settle(ref, 1000)
        self.assertFalse(points.ai_release(ref))
        with self.assertRaises(points.PointsError):                   # 同一个 ref 不能预留两次
            points.ai_reserve(self.user['id'], ref, 1000)

    def test_balance_never_goes_negative_under_concurrent_ai_calls(self):
        from concurrent.futures import ThreadPoolExecutor
        user = tp.make_user(3)
        refs = ['ai-%s-%d' % (uuid.uuid4().hex, n) for n in range(10)]

        def attempt(ref):
            try:
                points.ai_reserve(user['id'], ref, 5000)
                return True
            except points.PointsError:
                return False
        with ThreadPoolExecutor(10) as pool:
            ok = list(pool.map(attempt, refs))
        self.assertGreaterEqual(points.balance(user['id']), 0)
        self.assertLess(sum(ok), 10)

    def test_ledger_shows_both_the_hold_and_the_refund_with_token_note(self):
        self.scan()
        rows = points.ledger(self.user['id'])
        self.assertIn('3000 tokens', rows[0]['note'])


class Behaviour(AiBase):
    def test_prompt_contains_headings_but_never_body_text(self):
        self.scan()
        prompt = self.prompts[0]
        self.assertIn('财务风险', prompt)
        self.assertIn('偿债能力分析', prompt)
        self.assertNotIn(SECRET[:10], prompt)
        self.assertIn('共 %d 字' % len(SECRET), prompt)        # 正文只给字数

    def test_table_notes_and_suspected_headings_are_labelled_in_the_structure_sent_to_the_model(self):
        from core.dsh import outline
        rows = [{'idx': 3, 'kind': 'candidate', 'text': '偿债能力', 'flags': []},
                {'idx': 4, 'kind': 'note', 'text': '数据来源：年报', 'chars': 7, 'flags': []}]
        text = ' || '.join(outline.analysis_lines(rows)[0])
        self.assertIn('3 | 疑似标题 | 偿债能力', text)
        self.assertIn('4 | 表注 | 数据来源：年报', text)

    def test_reasoning_is_off_and_output_is_capped(self):
        self.scan()
        kwargs, effort = self.kwargs[0]
        self.assertEqual(effort, 'off')
        self.assertEqual(kwargs['max_tokens'], 3000)

    def test_changes_are_whitelisted_and_cannot_touch_text(self):
        self.answers = [reply([
            {'idx': 2, 'role': 'heading', 'level': 2, 'reason': '短行像二级标题', 'text': '偷偷改文字'},
            {'idx': 999, 'role': 'body'},                                  # 不在清单里
            {'idx': 1, 'role': 'heading', 'level': 1},                     # 正文段，不能改
            {'idx': 4, 'role': 'explode'},                                 # 未知类型
            {'idx': 2, 'role': 'body'},                                    # 重复 idx
        ])]
        body = self.scan().json()
        self.assertEqual(body['changes'], [{'idx': 2, 'role': 'heading', 'reason': '短行像二级标题', 'level': 2}])
        self.assertNotIn('偷偷改文字', json.dumps(body, ensure_ascii=False))

    def test_a_change_that_matches_the_current_state_is_dropped(self):
        self.answers = [reply([{'idx': 0, 'role': 'heading', 'level': 1}])]
        self.assertEqual(self.scan().json()['changes'], [])

    def test_user_edits_are_sent_to_the_model_and_the_scan_sees_them(self):
        self.scan([{'idx': 2, 'role': 'heading', 'level': 2}])
        prompt = self.prompts[0]
        self.assertIn('2 | 用户设为标题2级', prompt)
        self.assertIn('2 | 标题2级 | 偿债能力分析', prompt)

    def test_chat_history_keeps_only_the_last_turns_and_the_message_is_sent(self):
        for n in range(6):
            self.chat('第%d句话' % n)
        prompt = self.prompts[-1]
        self.assertIn('第5句话', prompt)
        self.assertNotIn('第0句话', prompt.split('<history>')[1].split('</history>')[0])
        self.assertEqual(len(self.prompts), 6)

    def test_message_limits(self):
        self.assertEqual(self.chat('  ').status_code, 422)
        self.assertEqual(self.chat('字' * 501).status_code, 422)
        self.assertEqual(self.prompts, [])

    def test_call_cap_per_analysis_is_enforced_before_charging(self):
        with patch.object(routes, 'MAX_AI_CALLS', 1):
            self.assertEqual(self.scan().status_code, 200)
            before = points.balance(self.user['id'])
            self.assertEqual(self.scan().status_code, 429)
        self.assertEqual(points.balance(self.user['id']), before)

    def test_a_second_request_while_one_is_running_is_refused_without_charging(self):
        rec = routes.ANALYSES[self.aid]
        rec['lock'].acquire()
        try:
            self.assertEqual(self.scan().status_code, 409)
        finally:
            rec['lock'].release()
        self.assertEqual(points.balance(self.user['id']), 100)

    def test_ai_unavailable_returns_503_and_charges_nothing(self):
        with patch.dict(os.environ, {'DSH_ENABLED': 'false'}):
            self.assertEqual(self.scan().status_code, 503)
        self.assertEqual(points.balance(self.user['id']), 100)

    def test_per_user_daily_limit(self):
        cfg = routes._config()
        cfg['dsh']['chat'] = {'per_user_daily': 1}
        self.assertEqual(self.scan().status_code, 200)
        self.assertEqual(self.scan().status_code, 429)
        self.assertEqual(points.balance(self.user['id']), 99.4)

    def test_analysis_response_tells_the_ui_the_price_and_availability(self):
        ai = self.analysis['ai']
        self.assertTrue(ai['available'])
        self.assertEqual((ai['rate'], ai['min_charge']), (0.2, 0.5))
        self.assertGreater(ai['reserve'], 0.5)
        self.assertEqual(ai['calls_left'], routes.MAX_AI_CALLS)

    def test_end_to_end_accepting_the_suggestion_changes_the_formatted_result(self):
        self.answers = [reply([{'idx': 2, 'role': 'heading', 'level': 2, 'reason': '短行'}])]
        change = self.scan().json()['changes'][0]
        overrides = [{'idx': change['idx'], 'role': change['role'], 'level': change['level']}]
        _, _, struct = fo.convert(SPECS, overrides)
        self.assertEqual(fo.heads(struct), ['1　财务风险', '1.1　偿债能力分析', '2　防范措施'])


if __name__ == '__main__':
    unittest.main()
