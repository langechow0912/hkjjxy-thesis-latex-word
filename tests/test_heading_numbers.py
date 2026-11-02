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
    def test_missing_parent_continues_with_original_number_and_warning(self):
        stat, headings, logs = convert(['1.1 焦虑表达形式的研究', '正文内容。'])
        self.assertEqual(headings, ['1.1 焦虑表达形式的研究'])
        self.assertEqual(stat['numbering'], 'auto')
        self.assertTrue(any('缺少上级标题' in text for text in logs))

    def test_unnumbered_word_chapter_style_is_recognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.docx'
            doc = Document()
            doc.add_paragraph('绪论', style='Heading 1')
            doc.add_paragraph('1.1 背景')
            doc.save(source)
            tex, _ = docx2tex.convert(str(source), tmp, P.load('hkjjxy'), log=lambda *a: None)
            headings = [b['text'] for b in texparse.parse(tex)['blocks'] if b['kind'] == 'heading']
            self.assertEqual(headings, ['1　绪论', '1.1　背景'])

    def test_latex_fourth_level_keeps_numbering_and_references_do_not_force_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.docx'
            doc = Document()
            for line in ['1 绪论', '1.1 背景', '1.1.1 现状', '（1）流动比率',
                         '内容。', '（2）速动比率', '参考文献', '[1] 文献[J]. 2025.']:
                doc.add_paragraph(line)
            doc.save(source)
            tex, _ = docx2tex.convert(str(source), tmp, P.load('hkjjxy'), log=lambda *a: None)
            self.assertIn(r'\paragraph{流动比率}', tex)
            self.assertIn(r'\paragraph{速动比率}', tex)
            self.assertNotIn(r'\paragraph*', tex)
            tail = tex[tex.index(r'\paragraph{速动比率}'):]
            self.assertNotIn(r'\newpage', tail)
            self.assertIn(r'\par\vspace{3\baselineskip}', tail)

    def test_unnumbered_chapter_before_zero_prefixed_child_is_recovered(self):
        stat, headings, _ = convert(['引言', '这里是正文。', '企业筹资风险相关理论基础',
                                    '0.1 企业筹资风险的内涵与分类', '0.1.1 企业筹资风险',
                                    '1 比亚迪风险识别', '1.1 概况'])
        self.assertEqual(stat['numbering'], 'auto')
        self.assertEqual(headings, ['1', '1.1', '1.1.1', '2', '2.1'])

    def test_bracket_titles_follow_parent_level_and_keep_fourth_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Document()
            for text in ['1 风险', '1.1 偿债', '1.1.1 短期', '（1）流动比率',
                         '（2）速动比率', '1.2 营运', '（1）应收账款周转率', '（2）存货周转率']:
                doc.add_paragraph(text)
            source = Path(tmp) / 'source.docx'
            doc.save(source)
            tex, _ = docx2tex.convert(str(source), tmp, P.load('hkjjxy'), log=lambda *a: None)
            blocks = texparse.parse(tex)['blocks']
            self.assertEqual([b['text'] for b in blocks if b['kind']=='subheading'],
                             ['（1）流动比率', '（2）速动比率'])
            third = [b['text'] for b in blocks if b.get('level')==3]
            self.assertEqual(third, ['1.1.1　短期', '1.2.1　应收账款周转率', '1.2.2　存货周转率'])

    def test_plain_growth_metric_labels_under_second_level_are_third_level(self):
        _, headings, _ = convert(['1 风险', '1.1 发展风险', '营业收入增长率',
                                  '本企业营收增长率在五年内发生变化，' + '指标解释。' * 12,
                                  '净利润增长率', '净利润增长率用于观察企业变化，' + '指标解释。' * 12])
        self.assertEqual(headings, ['1', '1.1', '1.1.1', '1.1.2'])
        _, headings, _ = convert(['1 风险', '1.1 发展风险', '营业收入增长率', '简短备注。'])
        self.assertEqual(headings, ['1', '1.1'])

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
