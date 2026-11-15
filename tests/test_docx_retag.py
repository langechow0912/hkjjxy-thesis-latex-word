import io
import unittest

from docx import Document

from core import docx_retag, docx2tex


def make(lines, styles=None):
    doc = Document()
    for i, text in enumerate(lines):
        p = doc.add_paragraph(text)
        if styles and styles.get(i):
            p.style = doc.styles[styles[i]]
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def levels(data):
    doc = Document(io.BytesIO(data))
    return [(p.text, p.style.name) for p in doc.paragraphs]


BODY = '这是一段足够长的正文内容，用来保证它不会被当成标题。' * 4


class Retag(unittest.TestCase):
    def test_plain_lines_get_heading_styles_without_changing_text(self):
        src = make(['摘要：略', '关键词：甲', 'Abstract: x', 'Key words: a', '引言', BODY,
                    '1 理论', '1.1 概念', BODY, '1.1.1 细分', BODY])
        out, rep = docx_retag.retag(src)
        got = dict(levels(out))
        self.assertEqual(got['引言'], 'Heading 1')
        self.assertEqual(got['1 理论'], 'Heading 1')
        self.assertEqual(got['1.1 概念'], 'Heading 2')
        self.assertEqual(got['1.1.1 细分'], 'Heading 3')
        self.assertEqual([t for t, _ in levels(out)], [t for t, _ in levels(src)])
        self.assertGreaterEqual(rep['retagged'], 4)

    def test_wrong_heading_style_is_corrected(self):
        src = make(['引言', BODY, '1 理论', '1.1 概念', BODY], {3: 'Heading 3'})
        out, _ = docx_retag.retag(src)
        self.assertEqual(dict(levels(out))['1.1 概念'], 'Heading 2')

    def test_user_forced_body_removes_heading_style(self):
        lines = ['引言', BODY, '1 理论', '1.1 概念', BODY, '本行不是标题']
        src = make(lines, {5: 'Heading 2'})
        idx = next(i for i, it in enumerate(docx2tex.read_items(Document(io.BytesIO(src))))
                   if it.get('text') == '本行不是标题')
        out, rep = docx_retag.retag(src, [{'idx': idx, 'role': 'body'}])
        self.assertEqual(dict(levels(out))['本行不是标题'], 'Normal')
        self.assertEqual(rep['demoted'], 1)


if __name__ == '__main__':
    unittest.main()
