"""标题归一：不管原稿用哪套编号，都剥掉原编号再按模板重新编号。"""
import re
import tempfile
import unittest
from pathlib import Path

from docx import Document

from core import docx2tex, texparse, profile as P

LONG = '这是一段说明正文，用来说明上面的标题确实带着内容，并且长度超过四十个字，免得被当成短标题。'


def build(specs):
    """specs 每项是文本，或 (文本, Word 样式)，或 (文本, 'bold')。返回 (tex, stat, 解析块)。"""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'source.docx'
        doc = Document()
        for spec in specs:
            text, mark = spec if isinstance(spec, tuple) else (spec, None)
            if mark == 'bold':
                doc.add_paragraph().add_run(text).bold = True
            elif mark:
                doc.add_paragraph(text, style=mark)
            else:
                doc.add_paragraph(text)
        doc.save(source)
        tex, stat = docx2tex.convert(str(source), tmp, P.load('hkjjxy'), log=lambda *a: None)
    return tex, stat, texparse.parse(tex)['blocks']


def heads(blocks):
    return [b['text'] for b in blocks if b['kind'] == 'heading']


def subheads(blocks):
    return [b['text'] for b in blocks if b['kind'] == 'subheading']


# 任何一级标题里都不该残留中文数字编号：这正是「1.1　一、财务风险」那种丑结果
LEFTOVER = re.compile(r'[一二三四五六七八九十]、|（[一二三四五六七八九十]）|^\S+　[①-⑳]')


class HeadingNormalize(unittest.TestCase):
    def test_full_chinese_numbering_system_maps_onto_template_levels(self):
        _, stat, blocks = build([
            '一、财务风险', '（一）偿债能力', '1. 流动比率', LONG,
            '（1）计算方式', '按流动资产除以流动负债计算。',
            '二、防范措施', '（一）完善制度', LONG])
        self.assertEqual(heads(blocks), ['1　财务风险', '1.1　偿债能力', '1.1.1　流动比率',
                                         '2　防范措施', '2.1　完善制度'])
        self.assertEqual(subheads(blocks), ['（1）计算方式'])
        self.assertFalse(any(LEFTOVER.search(h) for h in heads(blocks)))
        self.assertTrue(any('原编号' in n for n in stat['notes']))

    def test_styled_heading_with_numeral_in_text_is_not_double_numbered(self):
        # 用户报告的问题：带 Word 标题样式、文字里又写了「一、」「（一）」
        _, _, blocks = build([('一、研究背景', 'Heading 1'), ('（一）国内现状', 'Heading 2'),
                              LONG, ('二、研究方法', 'Heading 1')])
        self.assertEqual(heads(blocks), ['1　研究背景', '1.1　国内现状', '2　研究方法'])

    def test_second_level_without_parent_is_promoted_not_invented(self):
        _, stat, blocks = build([('（一）国内现状', 'Heading 2'), LONG])
        self.assertEqual(heads(blocks), ['1　国内现状'])
        self.assertTrue(any('缺少上级标题' in n for n in stat['notes']))

    def test_numbered_list_items_are_not_taken_for_headings(self):
        _, _, blocks = build(['1 绪论', '主要措施包括：', '1. 提高效率', '2. 降低成本',
                              '3. 控制风险', '以上措施同时推进。'])
        self.assertEqual(heads(blocks), ['1　绪论'])

    def test_levels_below_template_are_kept_as_bold_body(self):
        tex, stat, blocks = build(['一、甲', '（一）乙', '1. 丙', LONG, '（1）丁', '① 戊', LONG])
        self.assertEqual(heads(blocks), ['1　甲', '1.1　乙', '1.1.1　丙'])
        self.assertEqual(subheads(blocks), ['（1）丁'])
        self.assertIn(r'\textbf{① 戊}', tex)       # 不丢内容
        self.assertTrue(any('超出模板' in n for n in stat['notes']))

    def test_short_bold_line_before_prose_is_recognised_as_heading(self):
        _, stat, blocks = build(['1 风险', '1.1 偿债', ('流动比率分析', 'bold'), LONG])
        self.assertEqual(heads(blocks), ['1　风险', '1.1　偿债', '1.1.1　流动比率分析'])
        self.assertTrue(any('加粗短行' in n for n in stat['notes']))

    def test_bold_line_inside_prose_flow_stays_body(self):
        _, _, blocks = build(['1 风险', ('注意', 'bold'), '很短。'])
        self.assertEqual(heads(blocks), ['1　风险'])

    def test_numbered_item_after_unnumbered_siblings_stays_a_sibling(self):
        # 真实样本：4.4 下面两个指标小标题没有编号，后面紧跟「（4）经营活动现金流量」，
        # 它是前两个的同级（4.4.3），不是 4.4.2 的子级。
        _, _, blocks = build([
            '1 风险', '1.1 发展风险',
            '营业收入增长率', '本企业营收增长率在五年内发生变化，' + '指标解释。' * 12,
            '净利润增长率', '净利润增长率用于观察企业变化，' + '指标解释。' * 12,
            '（4）经营活动现金流量', '经营现金流波动剧烈，' + '指标解释。' * 12])
        self.assertEqual(heads(blocks), ['1　风险', '1.1　发展风险', '1.1.1　营业收入增长率',
                                         '1.1.2　净利润增长率', '1.1.3　经营活动现金流量'])
        self.assertEqual(subheads(blocks), [])

    def test_arabic_system_unchanged_when_already_correct(self):
        _, stat, blocks = build(['1 绪论', '1.1 背景', '1.1.1 现状', '2 理论', '2.1 概念'])
        self.assertEqual(heads(blocks), ['1　绪论', '1.1　背景', '1.1.1　现状', '2　理论', '2.1　概念'])
        self.assertEqual(stat['numbering'], 'user')
        self.assertEqual(stat['notes'], [])


if __name__ == '__main__':
    unittest.main()
