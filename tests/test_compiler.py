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


class LegacyWordConversion(unittest.TestCase):
    def test_output_is_validated_and_conversion_uses_private_profile(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'paper.doc'
            source.write_bytes(b'legacy')
            def convert(command, **kwargs):
                self.assertIn('docx:Office Open XML Text', command)
                self.assertTrue(any(arg.startswith('-env:UserInstallation=file:') for arg in command))
                Document().save(Path(directory)/'paper.docx')
                return subprocess.CompletedProcess(command, 0, b'', b'')
            with patch.object(compiler.tools, 'resolve', return_value='soffice'), patch.object(compiler.subprocess, 'run', side_effect=convert):
                result = compiler.convert_docx({}, str(source), directory)
                self.assertTrue(Path(result).is_file())

    def test_stale_or_invalid_output_is_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'paper.doc'; source.write_bytes(b'broken')
            (Path(directory)/'paper.docx').write_bytes(b'stale')
            with patch.object(compiler.tools,'resolve',return_value='soffice'), patch.object(compiler.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'',b'')):
                with self.assertRaisesRegex(ValueError,'转换失败'):
                    compiler.convert_docx({}, str(source), directory)
