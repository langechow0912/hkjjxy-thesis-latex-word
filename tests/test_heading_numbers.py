import tempfile
import unittest
from pathlib import Path

from docx import Document

from core import docx2tex, texparse, profile as P


def convert(lines):
    logs = []
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'source.docx'
        doc = Document()
        for text in lines:
            doc.add_paragraph(text)
        doc.save(source)
        tex, stat = docx2tex.convert(str(source), tmp, P.load('hkjjxy'), log=logs.append)
    headings = [b['text'].split('　')[0]
                for b in texparse.parse(tex)['blocks'] if b['kind'] == 'heading']
    return stat, headings, logs


class HeadingNumbers(unittest.TestCase):
    def test_complete_source_numbering_is_kept(self):
        stat, headings, logs = convert(
            ['1 绪论', '1.1 背景', '1.1.1 现状', '1.2 意义', '2 理论', '2.1 概念'])
        self.assertEqual(stat['numbering'], 'user')
        self.assertEqual(headings, ['1', '1.1', '1.1.1', '1.2', '2', '2.1'])
        self.assertFalse(any('编号有误' in s for s in logs))

    def test_wrong_source_numbering_falls_back_to_sequence(self):
        stat, headings, logs = convert(
            ['1 绪论', '1.1 背景', '1.3 意义', '3 理论', '3.1 概念'])
        self.assertEqual(stat['numbering'], 'auto')
        self.assertEqual(headings, ['1', '1.1', '1.2', '2', '2.1'])
        self.assertTrue(any('1.3 意义' in s and '1.2' in s for s in logs))

    def test_long_chapter_title_is_recovered_before_checking(self):
        long_title = '2 数字经济背景下海南自由贸易港跨境电商企业的物流成本控制与优化路径研究'
        self.assertGreater(len(long_title), docx2tex.MAX_HEADING_LEN)
        stat, headings, _ = convert(
            ['1 绪论', '1.1 背景', long_title, '本章介绍理论。', '2.1 概念'])
        self.assertEqual(stat['sec1'], 2)
        self.assertEqual(stat['numbering'], 'user')
        self.assertEqual(headings, ['1', '1.1', '2', '2.1'])


if __name__ == '__main__':
    unittest.main()
