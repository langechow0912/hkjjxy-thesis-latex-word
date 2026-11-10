"""附录：另起一页、标题同参考文献、内容按正文；表图题注编号支持小数点写法（表1.1）。"""
import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from core import docx2tex, docxbuild, texparse, profile as P

LONG = '这是一段说明正文，用来说明上面的标题确实带着内容，并且长度超过四十个字，免得被当成短标题。'
ROOT = Path(__file__).resolve().parent.parent


def build(lines, table_after=None):
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'source.docx'
        doc = Document()
        for text in lines:
            doc.add_paragraph(text)
        doc.save(source)
        cfg = P.load('hkjjxy')
        tex, stat = docx2tex.convert(str(source), tmp, cfg, log=lambda *a: None)
        struct = texparse.parse(tex)
        out = Path(tmp) / 'out.docx'
        docxbuild.build(cfg, struct, str(out), str(Path(tmp) / 'figures'),
                        str(ROOT / 'schools/hkjjxy/cover-template.docx'), None)
        paragraphs = [(p.text, p) for p in Document(out).paragraphs]
        return tex, stat, struct, paragraphs


class Appendix(unittest.TestCase):
    BODY = ['1 绪论', LONG, '参考文献', '[1] 文献[J]. 2025.']

    def test_each_appendix_starts_a_new_page_with_reference_style_title(self):
        tex, stat, struct, _ = build(self.BODY + ['附录1 调查问卷', '问卷第一段。',
                                                  '附录2 访谈提纲', '提纲内容。'])
        self.assertIn('\\newpage\n\\vspace*{3\\baselineskip}\n\\unnumsec{附录1　调查问卷}', tex)
        self.assertIn('\\newpage\n\\vspace*{3\\baselineskip}\n\\unnumsec{附录2　访谈提纲}', tex)
        self.assertEqual(stat['refs'], 1)                 # 附录没有被吞进参考文献
        unnum = [b['text'] for b in struct['blocks'] if b['kind'] == 'unnum']
        # texparse 把全角空格归一成普通空格（参考文献、致谢同样），tex 里仍是全角
        self.assertEqual(unnum, ['参考文献', '附录1 调查问卷', '附录2 访谈提纲'])
        self.assertLess(tex.index('参考文献'), tex.index('附录1'))
        self.assertIn({'kind': 'text', 'text': '问卷第一段。'}, struct['blocks'])

    def test_appendix_title_is_formatted_exactly_like_the_references_title(self):
        _, _, _, paragraphs = build(self.BODY + ['附录1 调查问卷', '问卷第一段。'])
        by_text = dict(paragraphs)
        ref, app = by_text['参考文献'], by_text['附录1 调查问卷']
        for p in (ref, app):
            run = p.runs[0]
            self.assertEqual(run.font.size.pt, 12)        # 小四
            self.assertEqual(run._element.rPr.find(qn('w:rFonts')).get(qn('w:eastAsia')), '黑体')
            self.assertIsNone(p.alignment)                # 左对齐
            self.assertIsNone(p.paragraph_format.first_line_indent)   # 顶格
        # 标题之前有分页，正文段落仍是正文格式（首行缩进）
        texts = [t for t, _ in paragraphs]
        before = paragraphs[texts.index('附录1 调查问卷') - 1][1]
        self.assertIn('w:br', before._p.xml)
        self.assertIn('w:type="page"', before._p.xml)
        body = by_text['问卷第一段。']
        self.assertTrue(body.paragraph_format.first_line_indent)

    def test_appendix_follows_the_source_order_relative_to_acknowledgements(self):
        tex, _, _, _ = build(self.BODY + ['致谢', '感谢老师。', '附录1 问卷', '内容。'])
        self.assertLess(tex.index('\\unnumsec{致谢}'), tex.index('\\unnumsec{附录1　问卷}'))
        tex, _, _, _ = build(self.BODY + ['附录1 问卷', '内容。', '致谢', '感谢老师。'])
        self.assertLess(tex.index('\\unnumsec{附录1　问卷}'), tex.index('\\unnumsec{致谢}'))

    def test_appendix_placed_before_references_is_still_output_after_them(self):
        tex, stat, _, _ = build(['1 绪论', LONG, '附录1 问卷', '内容。', '参考文献', '[1] 文献[J]. 2025.'])
        self.assertEqual(stat['refs'], 1)
        self.assertLess(tex.index('\\unnumsec{参考文献}'), tex.index('\\unnumsec{附录1　问卷}'))

    def test_sentence_mentioning_appendix_is_not_a_title(self):
        tex, _, _, _ = build(['1 绪论', '详见附录1的问卷。', '附录见文末', LONG])
        self.assertNotIn('\\unnumsec{附录', tex)

    def test_headings_inside_appendix_do_not_become_chapters(self):
        tex, stat, _, _ = build(self.BODY + ['附录1 问卷', '1. 基本情况', '您的年龄：'])
        self.assertEqual(stat['sec1'], 1)
        self.assertIn('1. 基本情况', tex)


class DottedCaptionNumbers(unittest.TestCase):
    def convert(self, lines):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.docx'
            doc = Document()
            doc.add_paragraph('1 正文')
            for kind, value in lines:
                if kind == 'p':
                    doc.add_paragraph(value)
                else:
                    doc.add_table(rows=1, cols=2).rows[0].cells[0].text = value
            doc.save(source)
            return docx2tex.convert(str(source), tmp, P.load('hkjjxy'), log=lambda *a: None)

    def test_table_captions_written_as_1_dot_1_have_clean_titles(self):
        tex, stat = self.convert([
            ('p', '表1.1 调查对象年龄与性别分布及合作行为核心指标（n=85）'), ('t', '数据'),
            ('p', '表2．2　访谈对象基本信息'), ('t', '数据'),
            ('p', '表3.4  家庭教养方式分布及幼儿合作行为表现（n=85）'), ('t', '数据'),
            ('p', '如表3.4所示，结果明确。')])
        self.assertIn('{调查对象年龄与性别分布及合作行为核心指标（n=85）\\label{tab:t1}}', tex)
        self.assertIn('{访谈对象基本信息\\label{tab:t2}}', tex)
        self.assertIn('{家庭教养方式分布及幼儿合作行为表现（n=85）\\label{tab:t3}}', tex)
        self.assertNotIn('{.', tex)
        self.assertIn('如\\ref{tab:t3}所示', tex)      # 「表」字由解析阶段按编号补回
        self.assertEqual(stat['notes'], [])


if __name__ == '__main__':
    unittest.main()
