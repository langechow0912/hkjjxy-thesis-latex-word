"""API deployment must boot without the legacy document toolchain."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ApiRuntime(unittest.TestCase):
    def test_boot_without_local_document_dependencies(self):
        script = '''
import importlib.abc
import sys
class BlockLocal(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'docx', 'fitz', 'pymupdf', 'yaml'}:
            raise AssertionError('API runtime imported local dependency: ' + fullname)
sys.meta_path.insert(0, BlockLocal())
from fastapi.testclient import TestClient
from web.app import app
with TestClient(app) as client:
    assert client.get('/').status_code == 200
    assert client.get('/api/dayipaper').status_code == 200
    assert client.get('/api/tabword').status_code == 200
    assert client.get('/api/schools').status_code == 410
    assert client.post('/api/convert', files={'file': ('paper.docx', b'input')}).status_code == 410
    assert client.post('/api/build', json={'school':'hkjjxy', 'name':'paper', 'tex':'text'}).status_code == 410
'''
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('THESIS_', 'DAYIPAPER_', 'TABWORD_', 'ALIPAY_'))}
        result = subprocess.run([sys.executable, '-c', script],
                                cwd=Path(__file__).resolve().parents[1], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
