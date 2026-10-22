import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from core.localconfig import load_pat, parse_pat
from web import app as web


class UnifiedFlow(unittest.TestCase):
    def test_pat_assignment_file_is_not_sent_as_part_of_token(self):
        self.assertEqual(parse_pat('\ufeff PAT=sample-token\n'), 'sample-token')
        self.assertEqual(parse_pat('raw-token'), 'raw-token')
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            secret = Path(root) / '.secrets'
            secret.mkdir()
            (secret / 'dayipaper.pat').write_text('PAT=sample-token', encoding='utf-8')
            load_pat(root)
            with patch('urllib.request.urlopen') as request:
                request.return_value.__enter__.return_value.read.return_value = b'{"code":0,"data":{}}'
                web.dayipaper.search_rules('school')
                self.assertEqual(request.call_args.args[0].get_header('Authorization'), 'Bearer sample-token')

    def test_auth_password_can_be_loaded_from_a_file_and_required_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            password_file = Path(root) / 'auth-pass'
            password_file.write_text('temporary-pass\n', encoding='utf-8')
            with patch.dict(os.environ, {'THESIS_AUTH_PASS':'', 'THESIS_AUTH_PASS_FILE':str(password_file)}):
                self.assertEqual(web._secret_value('THESIS_AUTH_PASS'), 'temporary-pass')
            with patch.dict(os.environ, {'THESIS_AUTH_PASS':'', 'THESIS_AUTH_PASS_FILE':str(Path(root)/'missing')}):
                with self.assertRaises(RuntimeError):
                    web._secret_value('THESIS_AUTH_PASS')
            with patch.dict(os.environ, {'THESIS_AUTH_PASS':'inline', 'THESIS_AUTH_PASS_FILE':str(password_file)}):
                with self.assertRaises(RuntimeError):
                    web._secret_value('THESIS_AUTH_PASS')

    def test_pat_can_be_loaded_from_a_dedicated_file(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            path = Path(root) / 'pat'
            path.write_text('PAT=dedicated-token\n', encoding='utf-8')
            os.environ['DAYIPAPER_PAT_FILE'] = str(path)
            load_pat(root)
            self.assertEqual(os.environ['DAYIPAPER_PAT'], 'dedicated-token')

    def setUp(self):
        self.client = TestClient(web.app)

    def test_local_pat_load_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            secret = Path(root) / '.secrets'
            secret.mkdir()
            (secret / 'dayipaper.pat').write_text('test-file-token', encoding='utf-8-sig')
            load_pat(root)
            self.assertEqual(os.environ['DAYIPAPER_PAT'], 'test-file-token')
            os.environ['DAYIPAPER_PAT'] = 'override'
            load_pat(root)
            self.assertEqual(os.environ['DAYIPAPER_PAT'], 'override')

    def test_rules_forward_keyword_and_sanitize_auth_error(self):
        with patch.dict(os.environ, {'DAYIPAPER_PAT':'fake'}), patch.object(web.dayipaper, 'search_rules') as search:
            search.return_value = {'items': [{'id':'r','name':'Example'}], 'total':1}
            self.assertEqual(self.client.get('/api/format/rules?q=school').json()['total'], 1)
            search.assert_called_once_with('school', page_size=5)
            search.side_effect = web.dayipaper.ApiError('sensitive upstream response', http_status=401)
            response = self.client.get('/api/format/rules?q=school')
            self.assertEqual(response.status_code, 401)
            self.assertNotIn('sensitive', response.text)

    def test_submit_forwards_metadata_and_status_omits_internal_data(self):
        captured = {}
        def run(job, payload, filename, meta):
            captured.update(meta)
            job.update(state='done', preview='https://example.com/preview', log=['private'], taskId='private')
        with patch.dict(os.environ, {'DAYIPAPER_PAT':'fake'}), patch.object(web, '_run_cloud', side_effect=run):
            response = self.client.post('/api/format', files={'file':('paper.docx',b'word')},
                data={'ruleId':'r','title':'Title','authorName':'Author','subjectName':'Subject','titleEn':'English'})
            self.assertEqual(response.status_code, 200)
            jid = response.json()['job']
            import time
            for _ in range(30):
                if web.JOBS[jid]['state'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(captured['subjectName'], 'Subject')
            self.assertEqual(captured['titleEn'], 'English')
            status = self.client.get('/api/format/jobs/'+jid).json()
            self.assertTrue(status['preview'])
            self.assertNotIn('log',status)
            self.assertNotIn('taskId',status)
            with patch.object(web.dayipaper,'download_to') as export:
                with patch.object(web.alipay, 'public_status', return_value={'enabled': False}):
                    self.assertEqual(self.client.get('/api/format/jobs/'+jid+'/download').status_code,503)
                with patch.object(web.alipay, 'public_status', return_value={'enabled': True}), patch.object(
                        web.alipay, 'get_order', return_value={'status':'WAIT_BUYER_PAY'}):
                    self.assertEqual(self.client.get('/api/format/jobs/'+jid+'/download').status_code,402)
                export.assert_not_called()

    def test_job_ids_do_not_collide_after_reaping(self):
        first = web._new_job()
        web.JOBS.pop(first['id'])
        second = web._new_job()
        self.assertNotEqual(first['id'],second['id'])
        first['state'] = second['state'] = 'error'
        import shutil
        for job in (first,second):
            web.JOBS.pop(job['id'],None)
            shutil.rmtree(job['tmp'])
