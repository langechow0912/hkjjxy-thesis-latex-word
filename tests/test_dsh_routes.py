# -*- coding: utf-8 -*-
"""DSH 调试页接口：归属校验、结构不泄漏正文、限额、模型失败不回显细节。全部离线。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import accounts, profile
from core.dsh import runner
from tests import TEST_USER
from web import app as web
from web.account_routes import current_user

SECRET = '这是一段绝不能发给外部模型的未发表论文正文，里面有具体研究结论。'
GOOD = json.dumps({'issues': [{'type': 'heading_skipped', 'severity': 'high', 'message': '跳级',
                               'suggestion': '补二级标题', 'actions': []}], 'summary': '1 个问题'})
STAT = {
    'body_start': 0, 'resolved_levels': {'引言': 1},
    'items': [{'kind': 'p', 'pno': 0, 'text': '1 引言', 'heading_level': 1},
              {'kind': 'p', 'pno': 1, 'text': SECRET, 'heading_level': None},
              {'kind': 'p', 'pno': 2, 'text': '研究背景', 'heading_level': None}],
}


class DshRoutes(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        env = patch.dict(os.environ, {'ALIPAY_ORDER_DB': str(root / 'orders.sqlite3'),
                                      'DEEPSEEK_API_KEY': 'test-key', 'DSH_ENABLED': 'true',
                                      'THESIS_DSH_PAGE': 'true'})
        env.start()
        self.addCleanup(env.stop)

        cfg = profile.load('hkjjxy')
        cfg['dsh']['cost_control']['state_file'] = str(root / 'cost.json')
        cfg['dsh']['chat'] = {'per_user_daily': 2}
        patcher = patch('web.dsh_routes._config', return_value=cfg)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.prompts = []
        self.reply = {'content': 'ok', 'usage': {'total_tokens': 42}}

        async def fake_chat(client, prompt, system=None, **kwargs):
            self.prompts.append(prompt)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply
        chat = patch('core.dsh.client.DeepSeekClient.chat', fake_chat)
        chat.start()
        self.addCleanup(chat.stop)

        self.jobs = {}
        for owner, key in ((TEST_USER['id'], 'mine'), (2, 'theirs')):
            jid = 'j' + (('a' if key == 'mine' else 'b') * 32)
            work = root / key
            work.mkdir()
            runner.save_outline(work, STAT)
            web.JOBS[jid] = {'id': jid, 'kind': 'school', 'state': 'done', 'name': '测试论文',
                             'tmp': str(work), 'files': {}, 'log': []}
            accounts.record_upload(owner, 'format', jid, '测试论文', 'x')
            self.addCleanup(web.JOBS.pop, jid, None)
            self.jobs[key] = jid
        self.client = TestClient(web.app)

    def tearDown(self):
        self.tmp.cleanup()

    def chat(self, message='目录有问题吗', job='mine', **extra):
        return self.client.post('/api/dsh/chat', json={'job_id': self.jobs[job], 'message': message, **extra})

    def test_requires_login(self):
        web.app.dependency_overrides.pop(current_user, None)
        try:
            self.assertEqual(self.client.get('/api/dsh/skills').status_code, 401)
        finally:
            web.app.dependency_overrides[current_user] = lambda: TEST_USER

    def test_skills_lists_builtin_and_quota(self):
        data = self.client.get('/api/dsh/skills').json()
        pre = next(s for s in data['skills'] if s['dir'] == 'dsh-preprocess')
        self.assertTrue(pre['enabled'])
        self.assertEqual(data['quota']['user_limit'], 2)
        self.assertTrue(data['available'])

    def test_jobs_lists_only_my_jobs_with_outline(self):
        jobs = self.client.get('/api/dsh/jobs').json()['jobs']
        self.assertEqual([j['job_id'] for j in jobs], [self.jobs['mine']])

    def test_outline_shows_structure_without_prose(self):
        data = self.client.get('/api/dsh/outline', params={'job_id': self.jobs['mine']}).json()
        self.assertEqual([r['kind'] for r in data['rows']], ['heading', 'body', 'heading'])
        self.assertEqual(data['rows'][0]['level'], 1)
        self.assertNotIn(SECRET[:8], json.dumps(data, ensure_ascii=False))

    def test_cannot_read_someone_elses_job(self):
        self.assertEqual(self.client.get('/api/dsh/outline', params={'job_id': self.jobs['theirs']}).status_code, 404)
        self.assertEqual(self.chat(job='theirs').status_code, 404)
        self.assertEqual(self.prompts, [])

    def test_malformed_job_id_is_404(self):
        self.assertEqual(self.client.get('/api/dsh/outline', params={'job_id': '../etc'}).status_code, 404)

    def test_chat_sends_structure_only_and_counts_usage(self):
        res = self.chat()
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body['reply'], 'ok')
        self.assertEqual(body['tokens'], 42)
        self.assertEqual(body['quota']['user_used'], 1)
        self.assertIn('引言', self.prompts[0])
        self.assertNotIn(SECRET[:8], self.prompts[0])

    def test_chat_stops_at_per_user_limit(self):
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.chat().status_code, 200)
        self.assertEqual(self.chat().status_code, 429)
        self.assertEqual(len(self.prompts), 2)

    def test_chat_rejects_empty_and_overlong_message(self):
        self.assertEqual(self.chat(' ').status_code, 422)
        self.assertEqual(self.chat('字' * 501).status_code, 422)
        self.assertEqual(self.prompts, [])

    def test_model_failure_does_not_leak_provider_detail(self):
        self.reply = RuntimeError('DeepSeek API error 401: secret-provider-body')
        res = self.chat()
        self.assertEqual(res.status_code, 502)
        self.assertNotIn('secret-provider-body', res.text)

    def test_run_checkpoint_returns_suggestions(self):
        self.reply = {'content': GOOD, 'usage': {'total_tokens': 10}}
        res = self.client.post('/api/dsh/run', json={'job_id': self.jobs['mine'], 'checkpoint': 'cp1_preprocess'})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['suggestions'][0]['type'], 'heading_skipped')
        self.assertEqual(body['quota']['user_used'], 1)

    def test_run_unknown_or_disabled_checkpoint(self):
        run = lambda name: self.client.post('/api/dsh/run', json={'job_id': self.jobs['mine'], 'checkpoint': name})
        self.assertEqual(run('nope').status_code, 404)
        self.assertEqual(run('cp4_quality').status_code, 409)   # 占位检查点默认不启用

    def test_disabled_dsh_refuses_chat(self):
        with patch.dict(os.environ, {'DSH_ENABLED': 'false'}):
            self.assertEqual(self.chat().status_code, 503)


if __name__ == '__main__':
    unittest.main()


class DshPage(unittest.TestCase):
    def test_hidden_by_default(self):
        with patch.dict(os.environ):
            os.environ.pop('THESIS_DSH_PAGE', None)
            client = TestClient(web.app)
            for url in ('/dsh', '/dsh.js', '/api/dsh/skills', '/api/dsh/jobs'):
                self.assertEqual(client.get(url).status_code, 404, url)
            self.assertNotIn('/dsh', client.get('/').text)

    def test_page_and_script_are_served(self):
      with patch.dict(os.environ, {'THESIS_DSH_PAGE': 'true'}):
        self.assertIn('/dsh"', TestClient(web.app).get('/').text)
        client = TestClient(web.app)
        page = client.get('/dsh')
        self.assertEqual(page.status_code, 200)
        self.assertIn('dsh.js', page.text)
        script = client.get('/dsh.js')
        self.assertEqual(script.status_code, 200)
        self.assertIn('javascript', script.headers['content-type'])
