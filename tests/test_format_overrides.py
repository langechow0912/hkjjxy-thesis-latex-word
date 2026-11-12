"""识别结果预览：analyze 给出逐条结果；用户的手动修改（overrides）要真的进到排版结果里。"""
import tempfile
import unittest
from pathlib import Path

import fitz
from docx import Document

from core import docx2tex, texparse, profile as P

LONG = '这是一段说明正文，用来说明上面的标题确实带着内容，并且长度超过四十个字，免得被当成短标题。'


def make(specs, tmp):
    """specs: 文本 / (文本, Word 样式) / (文本, 'bold') / 'IMG'（一张图）/ ('TBL', rows)。每项恰好一个条目。"""
    image = Path(tmp) / 'image.png'
    with fitz.open() as pdf:
        pdf.new_page().get_pixmap().save(image)
    doc = Document()
    for spec in specs:
        text, mark = spec if isinstance(spec, tuple) else (spec, None)
        if text == 'IMG':
            doc.add_paragraph().add_run().add_picture(str(image))
        elif text == 'TBL':
            table = doc.add_table(rows=len(mark), cols=len(mark[0]))
            for r, row in enumerate(mark):
                for c, value in enumerate(row):
                    table.cell(r, c).text = value
        elif mark == 'bold':
            doc.add_paragraph().add_run(text).bold = True
        elif mark:
            doc.add_paragraph(text, style=mark)
        else:
            doc.add_paragraph(text)
    source = Path(tmp) / 'source.docx'
    doc.save(source)
    return str(source)


def convert(specs, overrides=None):
    with tempfile.TemporaryDirectory() as tmp:
        source = make(specs, tmp)
        tex, stat = docx2tex.convert(source, tmp, P.load('hkjjxy'), log=lambda *a: None, overrides=overrides)
        return tex, stat, texparse.parse(tex)


def analyze(specs, overrides=None):
    with tempfile.TemporaryDirectory() as tmp:
        return docx2tex.analyze(make(specs, tmp), overrides)


def heads(struct):
    return [b['text'] for b in struct['blocks'] if b['kind'] == 'heading']


def subheads(struct):
    return [b['text'] for b in struct['blocks'] if b['kind'] == 'subheading']


fo_heads = None


def kinds(result):
    return [(r['idx'], r['kind']) for r in result['rows']]


DOC = ['一、财务风险', LONG, '偿债能力分析', LONG, '二、防范措施', LONG]       # idx 0..5


class NoOverrideIsIdentical(unittest.TestCase):
    def test_none_empty_and_noop_give_the_same_tex(self):
        base, _, _ = convert(DOC)
        self.assertEqual(convert(DOC, overrides=None)[0], base)
        self.assertEqual(convert(DOC, overrides=[])[0], base)
        # 只是把已经是标题的行再「设成同一级」，结果不变
        same = [{'idx': 0, 'role': 'heading', 'level': 1}]
        self.assertEqual(convert(DOC, overrides=same)[0], base)


class Analyze(unittest.TestCase):
    def test_reports_headings_figures_tables_and_candidates(self):
        result = analyze(['一、总体情况', '二、分析', LONG, '偿债能力分析', LONG,
                          'IMG', '图1 资产结构图', ('TBL', [['a', 'b'], ['1', '2']]), LONG])
        by_kind = {}
        for row in result['rows']:
            by_kind.setdefault(row['kind'], []).append(row)
        self.assertEqual([r['level'] for r in by_kind['heading']], [1, 1])
        self.assertEqual(by_kind['heading'][0]['text'], '总体情况')
        self.assertEqual(by_kind['heading'][0]['num'], '一、')
        self.assertEqual(by_kind['heading'][0]['src'], 'numbering')
        self.assertEqual(len(by_kind['figure']), 1)
        self.assertEqual(by_kind['figure'][0]['title'], '资产结构图')
        self.assertEqual(by_kind['figure'][0]['caption_idx'], 6)
        self.assertEqual(by_kind['figure_caption'][0]['text'], '图1 资产结构图')
        self.assertEqual(len(by_kind['table']), 1)
        self.assertIn('no_caption', by_kind['table'][0]['flags'])
        self.assertEqual([r['text'] for r in by_kind['candidate']], ['偿债能力分析'])
        self.assertEqual(result['summary'], {'headings': 2, 'figures': 1, 'tables': 1, 'issues': 1})

    def test_source_line_under_a_table_is_a_table_note_not_a_suspected_heading(self):
        specs = ['一、甲', ('TBL', [['a', 'b'], ['1', '2']]), '数据来源：公司年报', '注：单位为万元', LONG]
        result = analyze(specs)
        by_idx = {r['idx']: r for r in result['rows']}
        self.assertEqual(by_idx[2]['kind'], 'note')
        self.assertEqual(by_idx[3]['kind'], 'note')
        self.assertFalse([r for r in result['rows'] if r['kind'] == 'candidate'])
        self.assertEqual(result['summary']['issues'], 1)          # 只有「表格没有题注」这一条
        # 用户仍然可以把它强制设成标题
        _, _, struct = convert(specs, [{'idx': 2, 'role': 'heading', 'level': 2}])
        self.assertIn('1.1　数据来源：公司年报', fo_heads(struct))

    def test_long_paragraphs_never_leak_their_text_into_rows_as_candidates(self):
        result = analyze(DOC)
        body = [r for r in result['rows'] if r['kind'] == 'body']
        self.assertEqual(len(body), 3)
        self.assertTrue(all(r['chars'] == len(LONG) for r in body))

    def test_flags_clamped_stray_caption_and_missing_figure_caption(self):
        result = analyze([('1.1.1 直接三级', 'Heading 3'), LONG, 'IMG', '图1显示了变化趋势。', LONG])
        flags = {r['idx']: r['flags'] for r in result['rows']}
        self.assertIn('clamped', flags[0])
        self.assertIn('no_caption', flags[2])         # 「图1显示了……。」是正文引用，不是题注

    def test_short_caption_looking_line_that_was_not_consumed_is_flagged(self):
        result = analyze(['一、甲', LONG, '图3 孤立的图题'])
        row = [r for r in result['rows'] if r['idx'] == 2][0]
        self.assertEqual(row['kind'], 'candidate')
        self.assertIn('stray_caption', row['flags'])

    def test_front_matter_and_reference_zone_are_not_listed_row_by_row(self):
        result = analyze(['摘要：这是摘要内容。', '关键词：甲；乙', 'Abstract: abstract text.', 'Keywords: a; b',
                          '一、引言', LONG, '参考文献', '[1] 张三. 论文[J]. 期刊, 2020.', '[2] 李四. 论文[J]. 期刊, 2021.'])
        listed = [r['text'] for r in result['rows']]
        self.assertNotIn('摘要：这是摘要内容。', listed)
        zones = [r for r in result['rows'] if r['kind'] == 'zone']
        self.assertEqual([z['zone'] for z in zones], ['ref'])
        self.assertFalse(any('张三' in t for t in listed))


class HeadingOverrides(unittest.TestCase):
    def test_promote_a_candidate_line_to_a_heading(self):
        _, _, struct = convert(DOC, [{'idx': 2, 'role': 'heading', 'level': 2}])
        self.assertEqual(heads(struct), ['1　财务风险', '1.1　偿债能力分析', '2　防范措施'])

    def test_demote_a_detected_heading_to_body(self):
        _, _, struct = convert(DOC, [{'idx': 4, 'role': 'body'}])
        self.assertEqual(heads(struct), ['1　财务风险'])
        self.assertTrue(any(b['kind'] == 'text' and '二、防范措施' in b['text'] for b in struct['blocks']))

    def test_changing_a_level_renumbers_everything_after_it(self):
        specs = ['一、甲', '（一）乙', LONG, '（二）丙', LONG, '二、丁', LONG]
        _, _, base = convert(specs)
        self.assertEqual(heads(base), ['1　甲', '1.1　乙', '1.2　丙', '2　丁'])
        _, _, struct = convert(specs, [{'idx': 3, 'role': 'heading', 'level': 1}])
        self.assertEqual(heads(struct), ['1　甲', '1.1　乙', '2　丙', '3　丁'])

    def test_level_that_skips_a_parent_is_compressed_and_does_not_crash(self):
        _, stat, struct = convert(['一、甲', LONG, '细节说明行', LONG], [{'idx': 2, 'role': 'heading', 'level': 3}])
        self.assertEqual(heads(struct), ['1　甲', '1.1　细节说明行'])
        self.assertTrue(any('缺少上级' in n for n in stat['notes']))

    def test_editing_heading_text_keeps_it_a_heading_with_new_wording(self):
        _, _, struct = convert(DOC, [{'idx': 0, 'role': 'heading', 'level': 1, 'text': '企业财务风险概述'}])
        self.assertEqual(heads(struct)[0], '1　企业财务风险概述')

    def test_text_only_edit_on_a_detected_heading(self):
        _, _, struct = convert(DOC, [{'idx': 4, 'text': '三、新的防范措施'}])
        self.assertIn('2　新的防范措施', heads(struct))

    def test_forced_heading_inside_the_reference_zone_is_not_swallowed(self):
        specs = ['一、引言', LONG, '参考文献', '[1] 张三. 论文[J]. 期刊, 2020.', '补充说明标题', '[2] 李四. 论文[J]. 期刊, 2021.']
        _, _, struct = convert(specs, [{'idx': 4, 'role': 'heading', 'level': 1}])
        self.assertIn('1　补充说明标题', heads(struct))

    def test_level_four_becomes_a_subheading(self):
        specs = ['一、甲', '（一）乙', '1. 丙', LONG, '丁']
        _, _, struct = convert(specs, [{'idx': 4, 'role': 'heading', 'level': 4}])
        self.assertEqual(subheads(struct), ['（1）丁'])


class CaptionOverrides(unittest.TestCase):
    def test_mark_a_plain_line_below_a_figure_as_its_caption(self):
        tex, stat, struct = convert(['一、甲', 'IMG', '资产结构一览'], [{'idx': 2, 'role': 'figure_caption'}])
        self.assertIn(r'\caption{资产结构一览}', tex)
        self.assertEqual(stat['figures'], 1)
        self.assertFalse(any('没识别到图题' in n for n in stat['notes']))

    def test_mark_a_plain_line_above_a_table_as_its_caption(self):
        tex, _, struct = convert(['一、甲', '资产负债表', ('TBL', [['项目', '金额'], ['现金', '1']])],
                                 [{'idx': 1, 'role': 'table_caption'}])
        tables = [b for b in struct['blocks'] if b['kind'] == 'table']
        self.assertEqual(tables[0]['name'], '资产负债表')

    def test_unmark_a_wrongly_detected_caption(self):
        tex, stat, struct = convert(['一、甲', 'IMG', '图1 本来是题注'], [{'idx': 2, 'role': 'body'}])
        self.assertIn(r'\caption{}', tex)
        self.assertTrue(any('没识别到图题' in n for n in stat['notes']))

    def test_forced_caption_with_no_figure_next_to_it_falls_back_to_body_with_a_note(self):
        _, stat, struct = convert(['一、甲', LONG, '孤零零的一行', LONG], [{'idx': 2, 'role': 'figure_caption'}])
        self.assertTrue(any('旁边没有对应的图片' in n for n in stat['notes']))
        self.assertTrue(any('孤零零的一行' in b['text'] for b in struct['blocks'] if b['kind'] == 'text'))

    def test_editing_a_caption_text_changes_the_caption(self):
        tex, _, _ = convert(['一、甲', 'IMG', '图1 旧题名'], [{'idx': 2, 'text': '图1 新题名'}])
        self.assertIn(r'\caption{新题名}', tex)

    def test_changing_a_table_caption_does_not_misnumber_in_text_references(self):
        specs = ['一、甲', '表1 第一张表', ('TBL', [['a', 'b'], ['1', '2']]), '如表1所示，数据见上。',
                 '看看这一行', ('TBL', [['c', 'd'], ['3', '4']]), '如表2所示。']
        tex, _, _ = convert(specs, [{'idx': 4, 'role': 'table_caption'}])
        self.assertIn(r'\ref{tab:t1}', tex)
        self.assertIn(r'\ref{tab:t2}', tex)


class Validation(unittest.TestCase):
    def items(self):
        with tempfile.TemporaryDirectory() as tmp:
            return docx2tex.read_items(Document(make(['一、甲', 'IMG', LONG, ''], tmp)))

    def test_bad_entries_are_dropped_with_a_reason_and_good_ones_kept(self):
        items = self.items()
        clean, rejected = docx2tex.normalize_overrides([
            {'idx': 0, 'role': 'heading', 'level': 2},          # 好
            {'idx': 99, 'role': 'body'},                         # 越界
            {'idx': 1, 'role': 'body'},                          # 图片行
            {'idx': 2, 'role': 'sneaky'},                        # 未知类型
            {'idx': 2, 'role': 'heading'},                       # 缺级别
            {'idx': 2, 'role': 'heading', 'level': 9},           # 级别越界
            {'idx': 2, 'text': 'x' * 500},                       # 过长
            {'idx': True, 'role': 'body'},                       # 布尔不算整数
            {'idx': 2},                                          # 没有任何修改
            'not a dict',
        ], items)
        self.assertEqual(clean, [{'idx': 0, 'role': 'heading', 'level': 2}])
        self.assertEqual(len(rejected), 9)

    def test_text_is_normalised_and_last_duplicate_wins(self):
        items = self.items()
        clean, _ = docx2tex.normalize_overrides([{'idx': 0, 'text': '甲\n  乙'},
                                                 {'idx': 0, 'role': 'body'}], items)
        self.assertEqual(clean, [{'idx': 0, 'role': 'body'}])
        clean, _ = docx2tex.normalize_overrides([{'idx': 0, 'text': '甲\n  乙'}], items)
        self.assertEqual(clean[0]['text'], '甲 乙')

    def test_not_a_list(self):
        self.assertEqual(docx2tex.normalize_overrides(None, self.items()), ([], []))
        self.assertEqual(docx2tex.normalize_overrides({'idx': 0}, self.items())[0], [])

    def test_analyze_with_overrides_reports_rejections(self):
        result = analyze(DOC, [{'idx': 2, 'role': 'heading', 'level': 2}, {'idx': 50, 'role': 'body'}])
        self.assertEqual(len(result['rejected']), 1)
        self.assertTrue(any(r['kind'] == 'heading' and r['text'] == '偿债能力分析' and r['manual'] for r in result['rows']))


fo_heads = heads


if __name__ == '__main__':
    unittest.main()
