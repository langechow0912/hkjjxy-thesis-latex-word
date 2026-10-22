import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import compiler


class CompilerFailure(unittest.TestCase):
    def test_xelatex_failure_rejects_stale_pdf(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'main.pdf').write_bytes(b'stale')
            with patch.object(compiler.tools, 'resolve', return_value='xelatex'), patch.object(
                    compiler.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'error', b'')):
                with self.assertRaisesRegex(RuntimeError, 'XeLaTeX'):
                    compiler.compile_pdf({}, root, log=lambda *a: None)

    def test_office_nonzero_exit_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as root:
            def failure(*args, **kwargs):
                (Path(root) / 'source.pdf').write_bytes(b'incomplete')
                return subprocess.CompletedProcess([], 1, b'', b'error')
            with patch.object(compiler.tools, 'resolve', return_value='soffice'), patch.object(
                    compiler.subprocess, 'run', side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, 'LibreOffice'):
                    compiler.convert_pdf({}, 'source.docx', root, log=lambda *a: None)
