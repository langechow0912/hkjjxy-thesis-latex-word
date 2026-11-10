"""图与题注：上方、下方、同段都认；表格里的图不丢；图号引用只指向图。"""
import tempfile
import unittest
from pathlib import Path

import fitz
from docx import Document

from core import docx2tex, profile as P


class FigureCaptions(unittest.TestCase):
    def run_doc(self, build):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'image.png'
            with fitz.open() as pdf:
                pdf.new_page().get_pixmap().save(image)
            doc = Document()
            doc.add_paragraph('1 正文')
            build(doc, str(image))
            source = Path(tmp) / 'source.docx'
            doc.save(source)
            return docx2tex.convert(str(source), tmp, P.load('hkjjxy'), log=lambda *a: None)

    @staticmethod
    def picture(doc, image, text=''):
        p = doc.add_paragraph()
        if text:
            p.add_run(text)
        p.add_run().add_picture(image)
        return p

    def test_caption_below_the_figure(self):
        def build(doc, image):
            self.picture(doc, image)
            doc.add_paragraph('图1 资产结构图')
        tex, stat = self.run_doc(build)
        self.assertEqual(stat['figures'], 1)
        self.assertIn(r'\caption{资产结构图}', tex)
        self.assertEqual(stat['notes'], [])

    def test_caption_above_the_figure(self):
        def build(doc, image):
            doc.add_paragraph('图1 资产结构图')
            self.picture(doc, image)
            doc.add_paragraph('图2 负债结构图')
            self.picture(doc, image)
        tex, stat = self.run_doc(build)
        self.assertEqual(stat['figures'], 2)
        self.assertIn(r'\caption{资产结构图}', tex)
        self.assertIn(r'\caption{负债结构图}', tex)
        self.assertNotIn('图1 资产结构图\n', tex)      # 题注没有再作为正文多排一遍
        self.assertEqual(stat['notes'], [])

    def test_caption_in_the_same_paragraph_as_the_figure(self):
        def build(doc, image):
            self.picture(doc, image, text='图1 同段题注')
        tex, stat = self.run_doc(build)
        self.assertIn(r'\caption{同段题注}', tex)
        self.assertEqual(stat['notes'], [])

    def test_prose_that_starts_with_a_figure_reference_is_not_a_caption(self):
        def build(doc, image):
            self.picture(doc, image)
            doc.add_paragraph('图1显示了公司资产结构的变化情况，整体呈上升趋势。')
        tex, stat = self.run_doc(build)
        self.assertIn(r'\caption{}', tex)
        self.assertIn('图1显示了公司资产结构的变化情况', tex)
        self.assertTrue(any('没识别到图题' in n for n in stat['notes']))

    def test_figure_inside_a_table_cell_is_not_lost(self):
        def build(doc, image):
            table = doc.add_table(rows=1, cols=1)
            table.cell(0, 0).paragraphs[0].add_run().add_picture(image)
            doc.add_paragraph('图1 表内图')
        tex, stat = self.run_doc(build)
        self.assertEqual(stat['figures'], 1)
        self.assertIn(r'\caption{表内图}', tex)
        self.assertTrue(any('表格单元格' in n for n in stat['notes']))

    def test_figure_references_point_at_figures_not_tables(self):
        def build(doc, image):
            doc.add_paragraph('表1 数据表')
            table = doc.add_table(rows=1, cols=1)
            table.cell(0, 0).text = '数据'
            self.picture(doc, image)
            doc.add_paragraph('图1 结构图')
            doc.add_paragraph('如图1所示，结构合理；图3 并不存在。')
        tex, _ = self.run_doc(build)
        self.assertIn(r'\ref{fig:f1}', tex)
        self.assertNotIn(r'\ref{fig:f3}', tex)
        self.assertIn('图3', tex)      # 引用了不存在的图：保持原文，不乱指

    def test_caption_like_line_without_a_figure_is_reported(self):
        def build(doc, image):
            doc.add_paragraph('图2')
            doc.add_paragraph('这里没有图。')
        _, stat = self.run_doc(build)
        self.assertTrue(any('像图/表题注' in n for n in stat['notes']))


if __name__ == '__main__':
    unittest.main()
