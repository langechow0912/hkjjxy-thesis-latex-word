# -*- coding: utf-8 -*-
"""识别预览的接口：归属、校验、TTL、上限；以及 analysisId + overrides 怎样进到排版任务里。"""
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient

from core import accounts, docx2tex, schoolformat
from tests import test_format_overrides as fo
from tests import signed_in
from web import app as web
from web import format_ai_routes as routes
from web.account_routes import current_user

LONG = fo.LONG
SPECS = ['一、财务风险', LONG, '偿债能力分析', LONG, '二、防范措施', LONG]


def docx_bytes(specs=SPECS):
    with tempfile.TemporaryDirectory() as tmp:
        return Path(fo.make(specs, tmp)).read_bytes()


class AnalyzeRoutes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        db = patch.object(accounts, '_db_path', lambda: Path(self.temp.name) / 'accounts.sqlite3')
        db.start(); self.addCleanup(db.stop)
        env = patch.dict(os.environ, {'ALIPAY_ORDER_DB': os.path.join(self.temp.name, 'orders.db')})
        env.start(); self.addCleanup(env.stop)
        self.addCleanup(web.app.dependency_overrides.__setitem__, current_user, signed_in)
        self.addCleanup(self.clear)
        # _run_school 被替换掉后，任务会一直停在 running，会让后面的路由测试误报「排版任务较多」
        before = set(web.JOBS)
        self.addCleanup(lambda: [web.JOBS.pop(k) for k in set(web.JOBS) - before])
        self.client = TestClient(web.app)
        self.user = {'id': 11, 'username': 'a'}
        self.as_user(self.user)

    def clear(self):
        for key in list(routes.ANALYSES):
            routes.discard(key)

    def as_user(self, user):
        web.app.dependency_overrides[current_user] = lambda: user

    def upload(self, data=None, name='thesis.docx'):
        return self.client.post('/api/format/analyze', files={'file': (name, data if data is not None else docx_bytes())})

    def test_upload_returns_rows_summary_and_ai_info(self):
        res = self.upload()
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body['analysis_id'].startswith('a'))
        self.assertEqual(body['summary']['headings'], 2)
        self.assertEqual([r['text'] for r in body['rows'] if r['kind'] == 'candidate'], ['偿债能力分析'])
        self.assertIn('available', body['ai'])
        self.assertEqual(body['ttl'], routes.TTL)

    def test_requires_login(self):
        web.app.dependency_overrides.pop(current_user, None)
        self.assertEqual(self.upload().status_code, 401)

    def test_bad_uploads_are_refused_and_leave_nothing_behind(self):
        before = set(os.listdir(tempfile.gettempdir()))
        self.assertEqual(self.upload(name='a.doc').status_code, 400)
        self.assertEqual(self.upload(name='a.pdf').status_code, 400)
        self.assertEqual(self.upload(b'', name='a.docx').status_code, 400)
        self.assertEqual(self.upload(b'not a word file', name='a.docx').status_code, 400)
        with patch.dict(os.environ, {'THESIS_MAX_UPLOAD_MB': '1'}):
            self.assertEqual(self.upload(b'x' * (1024 * 1024 + 1)).status_code, 413)
        leftovers = [d for d in set(os.listdir(tempfile.gettempdir())) - before if d.startswith('thesis-analysis-')]
        self.assertEqual(leftovers, [])
        self.assertEqual(routes.ANALYSES, {})

    def test_doc_gives_a_helpful_message(self):
        self.assertIn('另存为', self.upload(name='old.doc').json()['detail'])

    def test_only_the_owner_can_use_an_analysis(self):
        aid = self.upload().json()['analysis_id']
        self.as_user({'id': 12, 'username': 'b'})
        for method, url, body in (('get', '', None), ('post', '/preview', {'overrides': []}),
                                  ('post', '/similar', {'idx': 0}), ('post', '/ai', {'overrides': []}),
                                  ('post', '/chat', {'message': 'x', 'overrides': []})):
            res = getattr(self.client, method)('/api/format/analyze/' + aid + url, **({'json': body} if body else {}))
            self.assertEqual(res.status_code, 404, url)
        self.assertEqual(self.client.get('/api/format/analyze/not-an-id').status_code, 404)

    def test_expired_analysis_is_deleted_and_returns_404(self):
        aid = self.upload().json()['analysis_id']
        rec = routes.ANALYSES[aid]
        folder = rec['dir']
        rec['created'] -= routes.TTL + 1
        self.assertEqual(self.client.get('/api/format/analyze/' + aid).status_code, 404)
        self.assertFalse(os.path.exists(folder))
        self.assertNotIn(aid, routes.ANALYSES)

    def test_sweeper_removes_expired_records(self):
        aid = self.upload().json()['analysis_id']
        routes.ANALYSES[aid]['created'] -= routes.TTL + 1
        web._purge_expired()
        self.assertNotIn(aid, routes.ANALYSES)

    def test_each_user_keeps_at_most_three_analyses(self):
        ids = [self.upload().json()['analysis_id'] for _ in range(routes.PER_USER + 1)]
        self.assertNotIn(ids[0], routes.ANALYSES)
        self.assertTrue(all(i in routes.ANALYSES for i in ids[1:]))

    def test_restore_after_refresh(self):
        aid = self.upload().json()['analysis_id']
        res = self.client.get('/api/format/analyze/' + aid)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['summary']['headings'], 2)

    def test_preview_recomputes_with_the_users_edits(self):
        aid = self.upload().json()['analysis_id']
        res = self.client.post('/api/format/analyze/%s/preview' % aid,
                               json={'overrides': [{'idx': 2, 'role': 'heading', 'level': 2}, {'idx': 99, 'role': 'body'}]})
        rows = res.json()['rows']
        self.assertTrue(any(r['kind'] == 'heading' and r['text'] == '偿债能力分析' and r['level'] == 2 for r in rows))
        self.assertEqual(len(res.json()['rejected']), 1)

    def test_similar_lines(self):
        specs = ['一、甲', LONG, '（一）乙', '（二）丙', '（三）丁', LONG, '随手一句。']
        aid = self.upload(docx_bytes(specs)).json()['analysis_id']
        res = self.client.post('/api/format/analyze/%s/similar' % aid, json={'idx': 2})
        self.assertEqual(sorted(res.json()['idx']), [3, 4])

    # ---- 排版入口 ----
    def format(self, **extra):
        data = {'ruleId': 'school:hkjjxy', 'title': '论文', 'authorName': '张三', **extra}
        return self.client.post('/api/format', data=data, files=extra.pop('files', None))

    def test_format_with_analysis_uses_stored_bytes_and_clean_overrides_then_discards_it(self):
        stored = docx_bytes()
        aid = self.upload(stored).json()['analysis_id']
        with patch.object(web, '_run_school') as run, patch.object(web, 'LOCAL_FORMAT', True):
            res = self.client.post('/api/format', data={
                'ruleId': 'school:hkjjxy', 'title': '论文', 'authorName': '张三', 'analysisId': aid,
                'overrides': json.dumps([{'idx': 2, 'role': 'heading', 'level': 2}])})
        self.assertEqual(res.status_code, 200, res.text)
        _, payload, meta = run.call_args.args
        self.assertEqual(payload, stored)
        self.assertEqual(meta['overrides'], [{'idx': 2, 'role': 'heading', 'level': 2}])
        self.assertEqual(meta['sourceSuffix'], '.docx')
        self.assertNotIn(aid, routes.ANALYSES)

    def test_format_rejects_invalid_overrides_without_consuming_the_analysis(self):
        aid = self.upload().json()['analysis_id']
        with patch.object(web, '_run_school') as run, patch.object(web, 'LOCAL_FORMAT', True):
            for bad in ('not json', json.dumps([{'idx': 999, 'role': 'body'}])):
                res = self.client.post('/api/format', data={'ruleId': 'school:hkjjxy', 'title': 't', 'authorName': 'a',
                                                            'analysisId': aid, 'overrides': bad})
                self.assertEqual(res.status_code, 400, bad)
        run.assert_not_called()
        self.assertIn(aid, routes.ANALYSES)

    def test_format_with_someone_elses_or_unknown_analysis_is_404(self):
        aid = self.upload().json()['analysis_id']
        self.as_user({'id': 12, 'username': 'b'})
        with patch.object(web, '_run_school') as run, patch.object(web, 'LOCAL_FORMAT', True):
            for value in (aid, 'a' + '0' * 32, 'junk'):
                res = self.client.post('/api/format', data={'ruleId': 'school:hkjjxy', 'title': 't', 'authorName': 'a',
                                                            'analysisId': value})
                self.assertEqual(res.status_code, 404, value)
        run.assert_not_called()

    def test_analysis_is_only_for_the_local_school(self):
        aid = self.upload().json()['analysis_id']
        res = self.client.post('/api/format', data={'ruleId': 'dayipaper:x', 'title': 't', 'authorName': 'a', 'analysisId': aid})
        self.assertEqual(res.status_code, 400)

    def test_format_still_needs_a_file_when_there_is_no_analysis(self):
        with patch.object(web, 'LOCAL_FORMAT', True):
            res = self.client.post('/api/format', data={'ruleId': 'school:hkjjxy', 'title': 't', 'authorName': 'a'})
        self.assertEqual(res.status_code, 422)

    def test_format_without_new_fields_is_unchanged(self):
        with patch.object(web, '_run_school') as run, patch.object(web, 'LOCAL_FORMAT', True):
            res = self.client.post('/api/format', data={'ruleId': 'school:hkjjxy', 'title': 't', 'authorName': 'a'},
                                   files={'file': ('a.docx', b'bytes')})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('overrides', run.call_args.args[2])


class OverridesReachThePipeline(unittest.TestCase):
    def test_school_run_passes_overrides_to_convert_and_omits_them_otherwise(self):
        seen = []

        def fake_convert(*args, **kwargs):
            seen.append(kwargs)
            raise RuntimeError('stop here')
        for meta, expected in (({'title': 't', 'overrides': [{'idx': 1, 'role': 'body'}]}, [{'idx': 1, 'role': 'body'}]),
                               ({'title': 't'}, None)):
            with tempfile.TemporaryDirectory() as tmp, patch.object(docx2tex, 'convert', fake_convert):
                job = {'tmp': tmp, 'log': [], 'files': {}, 'suggestions': []}
                schoolformat.run(job, b'data', {'id': 'hkjjxy'}, meta)      # run 自己兜住异常，只会标记 error
                self.assertEqual(job['state'], 'error')
            self.assertEqual(seen[-1].get('overrides'), expected)


if __name__ == '__main__':
    unittest.main()
