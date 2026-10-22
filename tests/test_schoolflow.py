import io
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient

from core import schoolformat, docx2tex, texparse
from web import app as web


class SchoolRouting(unittest.TestCase):
    def test_reference_roundtrip_has_one_number_and_no_internal_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.docx'
            doc = Document()
            for text in ['1 绪论', '正文引用[1]。', '参考文献', '[1] 测试文献. 题目[M]. 2026.']:
                doc.add_paragraph(text)
            doc.save(source)
            tex, _ = docx2tex.convert(str(source), tmp, web.P.load('hkjjxy'), log=lambda *a: None)
            refs = [b['text'] for b in texparse.parse(tex)['blocks'] if b['kind'] == 'ref']
            self.assertEqual(refs, ['[1] 测试文献. 题目[M]. 2026.'])
            self.assertIn(r'\textsuperscript{[1]}', tex)

    def setUp(self):
        self.client = TestClient(web.app)

    def test_school_search_never_calls_external_service(self):
        with patch.object(web.dayipaper, 'search_rules') as remote:
            data = self.client.get('/api/format/rules', params={'q': '海口经济学院'}).json()
            self.assertEqual(data['items'][0]['id'], 'school:hkjjxy')
            remote.assert_not_called()

    def test_other_school_gets_provider_namespace(self):
        with patch.object(web.dayipaper, 'has_token', return_value=True), patch.object(
                web.dayipaper, 'search_rules', return_value={'items': [{'id': 'r'}], 'total': 1}):
            data = self.client.get('/api/format/rules?q=other').json()
            self.assertEqual(data['items'][0]['id'], 'dayipaper:r')

    def test_school_submission_requires_payment_before_download(self):
        created = []
        def run(job, payload, cfg, meta):
            created.append(job)
            out = Path(job['tmp']) / 'result.docx'
            out.write_bytes(b'word-output')
            pdf = Path(job['tmp']) / 'preview.pdf'
            pdf.write_bytes(b'%PDF-preview')
            job.update(state='done', files={'docx': str(out), 'pdf': str(pdf)})
        with patch.object(web.dayipaper, 'has_token', return_value=False), patch.object(
                web.schoolformat, 'validate', return_value={}), patch.object(
                web.schoolformat, 'run', side_effect=run), patch.object(web, '_run_cloud') as remote:
            response = self.client.post('/api/format', files={'file': ('paper.docx', b'input')},
                data={'ruleId': 'school:hkjjxy', 'title': 'Title', 'authorName': 'Author'})
            self.assertEqual(response.status_code, 200)
            jid = response.json()['job']
            try:
                for _ in range(100):
                    if web.JOBS[jid]['state'] != 'running':
                        break
                    time.sleep(.01)
                state = self.client.get('/api/format/jobs/' + jid).json()
                self.assertFalse(state['billedExport'])
                self.assertTrue(state['paymentRequired'])
                self.assertTrue(state['freePdfDownload'])
                pdf = self.client.get('/api/format/jobs/' + jid + '/download?fmt=2')
                self.assertEqual(pdf.status_code, 200)
                self.assertEqual(pdf.content, b'%PDF-preview')
                self.assertEqual(pdf.headers['content-type'], 'application/pdf')
                with patch.object(web.alipay, 'public_status', return_value={'enabled': False}):
                    blocked = self.client.get('/api/format/jobs/' + jid + '/download')
                self.assertEqual(blocked.status_code, 503)
                self.assertIn('暂不能下载', blocked.json()['detail'])
                with patch.object(web.alipay, 'public_status', return_value={'enabled': True}), patch.object(
                        web.alipay, 'get_order', return_value={'status': 'WAIT_BUYER_PAY'}):
                    unpaid = self.client.get('/api/format/jobs/' + jid + '/download')
                self.assertEqual(unpaid.status_code, 402)
                with patch.object(web.alipay, 'public_status', return_value={'enabled': True}), patch.object(
                        web.alipay, 'get_order', return_value={'status': 'TRADE_SUCCESS'}):
                    paid = self.client.get('/api/format/jobs/' + jid + '/download')
                self.assertEqual(paid.status_code, 200)
                self.assertEqual(paid.content, b'word-output')
                remote.assert_not_called()
            finally:
                job = web.JOBS.pop(jid)
                shutil.rmtree(job['tmp'])

    def test_unknown_school_cannot_become_a_path(self):
        response = self.client.post('/api/format', files={'file': ('paper.docx', b'input')},
            data={'ruleId': 'school:../../outside', 'title': 'Title', 'authorName': 'Author'})
        self.assertEqual(response.status_code, 400)

    def test_cover_keeps_template_fields_unfilled(self):
        cfg = web.P.load('hkjjxy')
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'cover.docx'
            schoolformat.prepare_cover(cfg, {'title': '论文测试标题', 'authorName': '测试作者',
                'tutorName': '测试导师', 'subjectName': '测试专业'}, target)
            text = '\n'.join(p.text for p in Document(target).paragraphs)
            self.assertNotIn('论文测试标题', text)
            self.assertNotIn('测试作者', text)
            self.assertNotIn('测试导师', text)
            template = Document(web.P.path(cfg, cfg['cover']['template']))
            self.assertEqual(text, '\n'.join(p.text for p in template.paragraphs))
