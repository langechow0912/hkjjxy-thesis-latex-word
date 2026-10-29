import json
import os
import re
import tempfile
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs
from core import tabword as t
from core import service_billing
from fastapi.testclient import TestClient
from web.app import app


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self): self.respond()
    def do_POST(self): self.respond()
    def do_DELETE(self): self.respond()
    def respond(self):
        body=self.rfile.read(int(self.headers.get('Content-Length',0)))
        self.server.requests.append((self.command,self.path,self.headers,body))
        code=200;ctype='application/json';payload={'success':True,'echo':'服务商结果','remaining_balance':0,'secret':'sk-offline-only'}
        if 'stream' in self.path:
            ctype='text/event-stream';payload='event: progress\ndata: {"rewritten":"改写文本"}\n\nevent: billing\ndata: {"cost":0.1}\n\nevent: done\ndata: {"message":"完成"}\n\n'
        scenario = self.server.scenario
        if scenario == 'unauthorized': code=401
        if scenario == 'redirect': code=302
        if scenario == 'failure': payload={'success':False,'message':'sk-offline-only'}
        if scenario == 'malformed': payload='not-json'
        if scenario == 'sse_error': payload='event: error\ndata: {"message":"failed"}\n\n'
        data=payload.encode() if isinstance(payload,str) else json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


class TabwordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.server.requests=[]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        base='http://127.0.0.1:'+str(self.server.server_port)
        self.env=patch.dict(os.environ,{'TABWORD_BASE_URL':base,'TABWORD_FORMAT_BASE_URL':base,'TABWORD_API_KEY':'sk-offline-only','TABWORD_ENABLE_CALLS':'true','TABWORD_ENABLE_DEVELOPMENT':'true'})
        self.env.start();self.addCleanup(self.env.stop);self.server.requests.clear()
        self.server.scenario = None

    def test_api_key_reads_configured_secret_file(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'tabword.key';path.write_text('file-key\n',encoding='utf-8')
            with patch.dict(os.environ,{'TABWORD_API_KEY':'','TABWORD_API_KEY_FILE':str(path)}):
                self.assertEqual(t.api_key(),'file-key')
    def sample(self,o):
        values={k:{'text':'待改写的论文段落','preset':'preset-zh','task_platform':'知网_zh'}.get(k,'sample') for k in o.required}
        file=('测试'+o.extensions[0],b'local-test-file') if o.extensions else None
        return values,file
    def test_all_26_contracts_over_local_http(self):
        self.assertEqual(len(t.OPERATIONS),26)
        for name,o in t.OPERATIONS.items():
            with self.subTest(operation=name):
                values,file=self.sample(o)
                if o.stream:
                    data=b''.join(t.stream_call(name,values,file)).decode();self.assertIn('event: billing',data);self.assertIn('改写文本',data)
                else:
                    data=t.call(name,values,file);self.assertTrue(data['success']);self.assertEqual(data['secret'],'[REDACTED]')
                method,path,headers,body=self.server.requests[-1]
                self.assertEqual(method,o.method);self.assertEqual(headers['Authorization'],'Bearer sk-offline-only')
                for key in values:
                    if '{'+key+'}' in o.path: self.assertIn('sample',path)
                    elif o.encoding=='query': self.assertIn(key,parse_qs(urlsplit(path).query))
                    elif o.encoding=='json': self.assertEqual(json.loads(body)[key],values[key])
                    else: self.assertIn(('name="'+key+'"').encode(),body)
                if file:self.assertIn(file[1],body);self.assertIn('multipart/form-data',headers['Content-Type'])
    def test_no_submission_blocks_even_billing_get(self):
        with patch.dict(os.environ,{'TABWORD_ENABLE_CALLS':'false'}):
            for name,o in t.OPERATIONS.items():
                values,file=self.sample(o)
                with self.subTest(operation=name):
                    self.assertFalse(t.dry_run(name,values,file)['submitted'])
                    with self.assertRaises(t.TabwordError):t.open_call(name,values,file)
        self.assertEqual(self.server.requests,[])
    def test_free_catalog_reads_are_separately_allowlisted(self):
        with patch.dict(os.environ,{'TABWORD_ENABLE_CALLS':'false'}):
            self.assertTrue(t.read_catalog('presets',{'mode':'reduce_ai'})['success'])
            self.assertTrue(t.read_catalog('detection_options',{'language':'zh'})['success'])
            with self.assertRaises(t.TabwordError): t.read_catalog('balance')
        self.assertEqual([urlsplit(request[1]).path for request in self.server.requests], ['/api/v2/presets','/api/v2/detection/options'])
    def test_contract_paths_match_individual_documentation(self):
        root = Path(__file__).resolve().parents[1] / 'docs' / 'tabword-api-pages'
        for o in t.OPERATIONS.values():
            with self.subTest(operation=o.name):
                text = (root / (o.doc+'.txt')).read_text(encoding='utf-8')
                self.assertIn(o.method+'\n'+o.path,text)
    def test_failures_do_not_retry_or_expose_key(self):
        for scenario in ('unauthorized','redirect','failure','malformed'):
            with self.subTest(scenario=scenario):
                self.server.scenario=scenario;self.server.requests.clear()
                with self.assertRaises(t.TabwordError) as context:t.call('presets')
                self.assertNotIn('sk-offline-only',str(context.exception))
                self.assertEqual(len(self.server.requests),1)
    def test_stream_error_and_wrong_entrypoint(self):
        self.server.scenario='sse_error'
        self.assertIn(b'event: error',b''.join(t.stream_call('paraphrase_stream',{'text':'test','preset':'x'})))
        self.server.requests.clear()
        with self.assertRaises(t.TabwordError):t.call('paraphrase_stream',{'text':'test','preset':'x'})
        with self.assertRaises(t.TabwordError):t.stream_call('presets')
        self.assertEqual(self.server.requests,[])
    def test_development_and_key_are_separate_gates(self):
        with patch.dict(os.environ,{'TABWORD_ENABLE_DEVELOPMENT':'false'}):
            with self.assertRaises(t.TabwordError):t.call('format_result',{'job_id':'abc'})
        with patch.dict(os.environ,{'TABWORD_API_KEY':'','DAYIPAPER_PAT':'do-not-use'}), patch.object(t,'api_key',return_value=''):
            with self.assertRaises(t.TabwordError):t.call('presets')
        self.assertEqual(self.server.requests,[])
    def test_bad_input_never_reaches_network(self):
        cases=[('paraphrase',{'text':'','preset':'a'},None),('presets',{'mode':'other'},None),('format_result',{'job_id':'../evil'},None),('format_result',{'job_id':'abc','wait_seconds':121},None),('detect_file',{'task_platform':'a','mode':'bad'},('x.docx',b'x')),('format_async',{'owner_id':'x','template_id':'y'},('x.txt',b'x')),('paraphrase',[],None)]
        for name,values,file in cases:
            with self.subTest(name=name,values=values):
                with self.assertRaises(t.TabwordError):t.call(name,values,file)
        self.assertEqual(self.server.requests,[])
    def test_route_dry_run_and_forced_submit(self):
        with TestClient(app) as client,patch.dict(os.environ,{'TABWORD_ENABLE_CALLS':'false'}):
            response=client.post('/api/tabword/document_async',data={'params':json.dumps({'preset_zh':'a','generate_diff':True})},files={'file':('论文.docx',b'fake-docx')})
            self.assertEqual(response.status_code,200);self.assertFalse(response.json()['charged'])
            response=client.post('/api/tabword/presets',data={'submit':'true'})
            self.assertEqual(response.status_code,409)
            self.assertEqual(client.post('/api/tabword/paraphrase',data={'params':'not json'}).status_code,422)
            self.assertNotIn('sk-offline-only',client.get('/api/tabword').text)
        self.assertEqual(self.server.requests,[])
    def test_routes_block_unpaid_calls_even_when_provider_enabled(self):
        with TestClient(app) as client:
            for name, operation in t.OPERATIONS.items():
                if name in t.FREE_CATALOG_OPERATIONS:
                    continue
                values, file = self.sample(operation)
                with self.subTest(operation=name):
                    response=client.post('/api/tabword/'+name,data={'submit':'true','params':json.dumps(values)},
                                         files={'file':file} if file else None)
                    expected = 402 if name in service_billing.SUBMISSIONS | {'document_task','paraphrase_task'} else 403
                    self.assertEqual(response.status_code,expected)
                    self.assertNotIn('sk-offline-only',response.text)
        self.assertEqual(self.server.requests,[])
    def test_free_catalog_route_with_paid_calls_disabled(self):
        with TestClient(app) as client,patch.dict(os.environ,{'TABWORD_ENABLE_CALLS':'false'}):
            response=client.get('/api/tabword/catalog/presets?mode=reduce_ai')
            self.assertEqual(response.status_code,200)
            self.assertEqual(urlsplit(self.server.requests[-1][1]).path,'/api/v2/presets')
            self.assertEqual(parse_qs(urlsplit(self.server.requests[-1][1]).query),{'mode':['reduce_ai']})
            self.assertEqual(client.get('/api/tabword/catalog/balance').status_code,403)

if __name__=='__main__':unittest.main()
