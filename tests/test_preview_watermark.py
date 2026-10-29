import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from core import schoolformat
from core.preview_watermark import haikou_preview


class PreviewWatermark(unittest.TestCase):
    def test_every_page_preserves_content_size_rotation_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'formatted.pdf'
            with fitz.open() as document:
                for rotation in (0, 90, 180, 270):
                    page = document.new_page(width=595, height=842)
                    page.insert_text((60, 60), 'Original content')
                    page.set_rotation(rotation)
                document.save(source)
            original = source.read_bytes()
            target = haikou_preview(str(source), 'hkjjxy')
            self.assertNotEqual(target, str(source))
            self.assertEqual(source.read_bytes(), original)
            with fitz.open(target) as document:
                self.assertEqual(len(document), 4)
                for index, page in enumerate(document):
                    self.assertEqual(page.rotation, index * 90)
                    self.assertEqual(page.get_text().count('良品论文'), 3)
                    self.assertIn('Original content', page.get_text())

    def test_other_school_does_not_open_or_modify_pdf(self):
        self.assertEqual(haikou_preview('not-a-file.pdf', 'other'), 'not-a-file.pdf')

    def test_local_job_publishes_watermarked_pdf_and_original_word(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {'tmp': directory, 'log': [], 'files': {}}
            cfg = {'id': 'hkjjxy'}
            with patch.object(schoolformat.docx2tex, 'convert', return_value=('text', None)), \
                 patch.object(schoolformat.texparse, 'parse', return_value={'blocks': [1], 'meta': {}}), \
                 patch.object(schoolformat, 'prepare_cover'), \
                 patch.object(schoolformat.toc, 'probe', return_value=[]), \
                 patch.object(schoolformat.docxbuild, 'build'), \
                 patch.object(schoolformat.compiler, 'convert_pdf', return_value='original.pdf'), \
                 patch('core.preview_watermark.haikou_preview', return_value='branded.pdf') as watermark:
                schoolformat.run(job, b'input', cfg, {'title': 'Title'})
            watermark.assert_called_once_with('original.pdf', 'hkjjxy')
            self.assertEqual(job['state'], 'done')
            self.assertEqual(job['files']['pdf'], 'branded.pdf')
            self.assertEqual(job['files']['docx'], str(Path(directory) / 'formatted.docx'))


if __name__ == '__main__':
    unittest.main()
