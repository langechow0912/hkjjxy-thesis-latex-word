"""原稿自带目录：识别后整段略过，不能被当成正文标题再识别一遍（否则成品里有两个目录）。"""
import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE

from core import docx2tex, texparse, profile as P

LONG = '这是一段说明正文，用来说明上面的标题确实带着内容，并且长度超过四十个字，免得被当成短标题。'
FRONT = ['摘要：本文研究企业财务风险。', '关键词：财务；风险',
         'Abstract: This paper studies financial risk.', 'Key words: finance; risk']
BODY = [('第一章 绪论', 'Heading 1'), LONG, ('1.1 研究背景', 'Heading 2'), LONG,
        ('第二章 风险分析', 'Heading 1'), LONG]


def build(specs):
    """specs 每项是文本或 (文本, 样式名)；样式不存在就现建一个。返回 (stat, 标题文字列表)。"""
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'source.docx'
        doc = Document()
        for spec in specs:
            text, style = spec if isinstance(spec, tuple) else (spec, None)
            if style and style not in [s.name for s in doc.styles]:
                doc.styles.add_style(style, WD_STYLE_TYPE.PARAGRAPH)
            doc.add_paragraph(text, style=style)
        doc.save(source)
        tex, stat = docx2tex.convert(str(source), tmp, P.load('hkjjxy'), log=lambda *a: None)
    blocks = texparse.parse(tex)['blocks']
    return stat, [b['text'] for b in blocks if b['kind'] == 'heading']


MANUAL_TOC = ['目录', '第一章 绪论\t1', '1.1 研究背景\t2', '第二章 风险分析\t5', '参考文献\t9']


class OriginalToc(unittest.TestCase):
    def test_typed_toc_after_abstract_is_skipped(self):
        stat, heads = build(FRONT + MANUAL_TOC + BODY)
        self.assertEqual(heads, ['1　绪论', '1.1　研究背景', '2　风险分析'])
        self.assertTrue(any('原稿自带的目录（5 行）' in n for n in stat['notes']))

    def test_word_toc_styles_are_skipped(self):
        toc = [('目 录', 'TOC Heading'), ('第一章 绪论 1', 'toc 1'), ('1.1 研究背景 2', 'toc 2'),
               ('第二章 风险分析 5', 'toc 1')]
        stat, heads = build(FRONT + toc + BODY)
        self.assertEqual(heads, ['1　绪论', '1.1　研究背景', '2　风险分析'])
        self.assertTrue(any('原稿自带的目录' in n for n in stat['notes']))

    def test_figure_and_table_lists_after_toc_are_skipped_too(self):
        lists = ['图目录', '图1-1 资产结构\t6', '表目录', '表1-1 偿债能力\t7']
        stat, heads = build(FRONT + MANUAL_TOC + lists + BODY)
        self.assertEqual(heads, ['1　绪论', '1.1　研究背景', '2　风险分析'])
        self.assertTrue(any('原稿自带的目录（9 行）' in n for n in stat['notes']))

    def test_toc_without_title_needs_three_consecutive_entries(self):
        stat, heads = build(FRONT + MANUAL_TOC[1:] + BODY)
        self.assertEqual(heads, ['1　绪论', '1.1　研究背景', '2　风险分析'])
        stat, _ = build(FRONT + MANUAL_TOC[1:3] + BODY)          # 只有两行，不够认作目录
        self.assertFalse(any('原稿自带的目录' in n for n in stat['notes']))

    def test_document_without_toc_is_unchanged(self):
        stat, heads = build(FRONT + BODY)
        self.assertEqual(heads, ['1　绪论', '1.1　研究背景', '2　风险分析'])
        self.assertFalse(any('原稿自带的目录' in n for n in stat['notes']))

    def test_toc_like_lines_far_into_body_are_left_alone(self):
        # 正文走了很远之后出现的「…… 3」不能被当目录，否则会把前面的章节整段吞掉
        filler = [LONG] * (docx2tex.TOC_WINDOW + 2)
        late = ['目录', '第三章 对策\t20', '第四章 结论\t25']
        stat, heads = build(FRONT + BODY + filler + late)
        self.assertEqual(heads[:3], ['1　绪论', '1.1　研究背景', '2　风险分析'])
        self.assertFalse(any('原稿自带的目录' in n for n in stat['notes']))


if __name__ == '__main__':
    unittest.main()
