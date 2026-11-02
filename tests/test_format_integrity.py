import tempfile
import unittest
from pathlib import Path

import fitz
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from core import docx2tex, texparse, docxbuild, profile, toc
from core.format_validation import verify


class FormatIntegrity(unittest.TestCase):
    def convert(self, doc, work):
        path = Path(work) / 'source.docx'
        doc.save(path)
        tex, stats = docx2tex.convert(str(path), work, profile.load('hkjjxy'), log=lambda *a: None)
        return texparse.parse(tex), stats

    def test_short_prose_does_not_create_a_second_chapter(self):
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            for text in ['1 第一章', '短句说明', '1.1 背景']:
                doc.add_paragraph(text)
            result, _ = self.convert(doc, work)
            headings = [b['text'] for b in result['blocks'] if b['kind'] == 'heading']
            self.assertEqual(headings, ['1　第一章', '1.1　背景'])
            self.assertIn({'kind': 'text', 'text': '短句说明'}, result['blocks'])

    def test_missing_later_chapter_does_not_promote_an_arbitrary_short_line(self):
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            for text in ['1 第一章', '短句说明', '2.1 背景']:
                doc.add_paragraph(text)
            result, _ = self.convert(doc, work)
            chapters = [b['text'] for b in result['blocks'] if b.get('level') == 1]
            self.assertEqual(chapters, ['1　第一章'])
            self.assertIn({'kind':'text', 'text':'短句说明'}, result['blocks'])

    def test_word_heading_styles_and_direct_outline_survive(self):
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            for text, style in [('章', 'Heading 1'), ('节', 'Heading 2'), ('小节', 'Heading 3'), ('细项', 'Heading 4')]:
                doc.add_paragraph(text, style=style)
            result, _ = self.convert(doc, work)
            self.assertEqual([b.get('level') for b in result['blocks'] if b['kind']=='heading'], [1, 2, 3])
            self.assertIn({'kind':'subheading', 'text':'（1）细项'}, result['blocks'])

    def test_references_and_acknowledgements_are_separate_in_both_orders(self):
        for endings in [['参考文献', '[1] 文献[J].', '致谢', '感谢老师。'],
                        ['致谢', '感谢老师。', '参考文献', '[1] 文献[J].']]:
            with self.subTest(endings=endings), tempfile.TemporaryDirectory() as work:
                doc = Document()
                for text in ['1 正文', '正文。'] + endings:
                    doc.add_paragraph(text)
                result, _ = self.convert(doc, work)
                refs = [b['text'] for b in result['blocks'] if b['kind']=='ref']
                self.assertEqual(refs, ['[1] 文献[J].'])
                self.assertIn({'kind':'text', 'text':'感谢老师。'}, result['blocks'])
                self.assertIn({'kind':'space', 'lines':3}, result['blocks'])
                self.assertFalse(any('vspace' in b.get('text','') for b in result['blocks']))

    def test_two_images_and_following_prose_are_preserved(self):
        with tempfile.TemporaryDirectory() as work:
            image = Path(work) / 'image.png'
            with fitz.open() as pdf:
                pdf.new_page().get_pixmap().save(image)
            doc = Document()
            doc.add_paragraph('1 正文')
            paragraph = doc.add_paragraph()
            paragraph.add_run().add_picture(str(image))
            paragraph.add_run().add_picture(str(image))
            doc.add_paragraph('这是普通正文，不是图题。')
            result, stats = self.convert(doc, work)
            self.assertEqual(stats['figures'], 2)
            self.assertIn({'kind':'text', 'text':'这是普通正文，不是图题。'}, result['blocks'])

    def test_last_table_note_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            doc.add_paragraph('1 正文')
            doc.add_paragraph('表1-1 数据')
            doc.add_table(rows=1, cols=1).cell(0,0).text = '数据'
            doc.add_paragraph('数据来源：年度报告。')
            result, _ = self.convert(doc, work)
            self.assertEqual(result['tables'][0]['note'], '数据来源：年度报告。')

    def test_seq_field_number_in_table_caption_is_read(self):
        """Word 的 SEQ 域编号在 w:fldSimple 里，Paragraph.text 会漏掉。

        漏掉后「表　资产负债率分析表」既不匹配表题，表就拿不到题名，
        还会被当成正文多排一段。这里按真实稿子的写法构造。
        """
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            doc.add_paragraph('1 正文')
            caption = doc.add_paragraph()
            caption.add_run('表')
            fld = OxmlElement('w:fldSimple')
            fld.set(qn('w:instr'), r' SEQ 表 \* ARABIC ')
            run = OxmlElement('w:r')
            text = OxmlElement('w:t')
            text.text = '1'
            run.append(text)
            fld.append(run)
            caption._p.append(fld)
            caption.add_run('　资产负债率分析表')
            doc.add_table(rows=1, cols=2)
            result, stats = self.convert(doc, work)
            self.assertEqual(stats['tables'], 1)
            self.assertEqual(result['tables'][0]['name'], '资产负债率分析表')
            self.assertNotIn('资产负债率分析表',
                             [b.get('text') for b in result['blocks']])

    def test_single_segment_table_number_and_cross_reference(self):
        """全文连续编号「表1」也要认，正文「如表 表1 所示」换成 \\ref。

        断言在原始 tex 上做：texparse 会把 \\ref 再解析回「表1」，
        在解析后的 block 里看不到反斜杠。
        """
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            doc.add_paragraph('1 正文')
            doc.add_paragraph('具体数据 如表 表1 所示。')
            doc.add_paragraph('表1　资产负债率分析表')
            doc.add_table(rows=1, cols=2)
            path = Path(work) / 'source.docx'
            doc.save(path)
            tex, stats = docx2tex.convert(str(path), work, profile.load('hkjjxy'),
                                          log=lambda *a: None)
            self.assertEqual(stats['tables'], 1)
            self.assertIn(r'\label{tab:t1}}{', tex)
            self.assertIn(r'\ref{tab:t1}', tex)
            self.assertNotIn('表 表1', tex)

    def test_chinese_chapter_numbering_is_recognised(self):
        """「第一章」「一、」这类中文编号很多学校模板在用，要能认出来。"""
        cases = [
            (['第一章 绪论', '正文。', '第二章 文献综述', '正文。'],
             ['绪论', '文献综述']),
            (['一、绪论', '正文。', '二、文献综述', '正文。'],
             ['绪论', '文献综述']),
        ]
        for lines, want in cases:
            with tempfile.TemporaryDirectory() as work:
                doc = Document()
                for line in lines:
                    doc.add_paragraph(line)
                result, _ = self.convert(doc, work)
                heads = [b['text'] for b in result['blocks'] if b['kind'] == 'heading']
                # 解析层会把自动编号拼回标题前面（「1　绪论」），只断言题名部分
                names = [h.split('　')[-1] for h in heads]
                self.assertEqual(names, want, lines)

    def test_caption_below_table_is_attached(self):
        """题注写在表下面的稿子也要认，不能丢给下一张表。"""
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            doc.add_paragraph('1 正文')
            doc.add_table(rows=1, cols=2)
            doc.add_paragraph('表1　资产负债率分析表')
            result, _ = self.convert(doc, work)
            self.assertEqual(result['tables'][0]['name'], '资产负债率分析表')

    def test_english_table_caption_is_recognised(self):
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            doc.add_paragraph('1 正文')
            doc.add_paragraph('Table 1 Asset analysis')
            doc.add_table(rows=1, cols=2)
            result, _ = self.convert(doc, work)
            self.assertEqual(result['tables'][0]['name'], 'Asset analysis')

    def test_untitled_table_is_reported_not_swallowed(self):
        """识别不到题名要报出来，而不是静默出一张没有表题的表。"""
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            doc.add_paragraph('1 正文')
            doc.add_table(rows=1, cols=2)
            path = Path(work) / 'source.docx'
            doc.save(path)
            _tex, stats = docx2tex.convert(str(path), work, profile.load('hkjjxy'),
                                           log=lambda *a: None)
            self.assertTrue(any('题名' in n for n in stats['notes']), stats['notes'])

    def test_generated_word_has_spacing_action_and_no_body_grid(self):
        with tempfile.TemporaryDirectory() as work:
            doc = Document()
            for text in ['1 正文', '正文。', '参考文献', '[1] 文献[J].']:
                doc.add_paragraph(text)
            result, _ = self.convert(doc, work)
            cfg = profile.load('hkjjxy')
            out = Path(work) / 'out.docx'
            docxbuild.build(cfg, result, out, work, profile.path(cfg,cfg['cover']['template']))
            generated = Document(out)
            self.assertTrue(all(s._sectPr.find(qn('w:docGrid')) is None for s in generated.sections[1:]))
            ref = next(p for p in generated.paragraphs if p.text=='参考文献')
            self.assertEqual(ref.paragraph_format.space_before.pt, 54)
            item = next(p for p in generated.paragraphs if p.text.startswith('[1]'))
            self.assertEqual(item.paragraph_format.line_spacing, 1.0)
            self.assertFalse(any('vspace' in p.text for p in generated.paragraphs))

    def test_toc_uses_actual_page_numbers_with_a_split_abstract(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'sample.pdf'
            with fitz.open() as pdf:
                for i, text in enumerate(['Abstract begins', 'Key words', '1 Chapter'], 1):
                    page=pdf.new_page()
                    page.insert_text((70,100), text)
                    page.insert_text((290,800),str(i))
                pdf.save(path)
            cfg = profile.load('hkjjxy')
            cfg['toc']['front_entries']=[]
            struct={'toc':[(1,'1 Chapter')], 'tables':[], 'figures':[]}
            self.assertEqual(toc.read_entries(cfg,struct,path)['toc'], [('1 Chapter',3)])
            struct['toc']=[(1,'Missing heading')]
            with self.assertRaisesRegex(ValueError, '无法定位'):
                toc.read_entries(cfg,struct,path,strict=True)

    def test_content_gate_rejects_replaced_image_even_with_same_count(self):
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            for name, shade in [('source', 0), ('other', 1)]:
                with fitz.open() as pdf:
                    page = pdf.new_page(width=20, height=20)
                    page.draw_rect(page.rect, fill=(shade, shade, shade))
                    page.get_pixmap().save(work / (name + '.png'))
            for name, image in [('source', 'source'), ('output', 'other')]:
                doc = Document()
                doc.add_paragraph('1 Chapter')
                doc.add_picture(str(work / (image + '.png')))
                doc.save(work / (name + '.docx'))
            with fitz.open() as pdf:
                pdf.new_page().insert_text((70,100), 'Chapter')
                pdf.save(work / 'out.pdf')
            with self.assertRaisesRegex(ValueError, '保全'):
                verify(work/'source.docx', work/'output.docx', work/'out.pdf', work/'report.json', strict=True)
            import json
            self.assertTrue(json.loads((work/'report.json').read_text(encoding='utf-8'))['missing_image_hashes'])

    def test_long_reference_renumbering_is_not_reported_as_content_loss(self):
        with tempfile.TemporaryDirectory() as work:
            work=Path(work)
            long_ref='Wang et al. Reference title. ' + 'Long bibliography content. ' * 10
            source=Document();source.add_paragraph('1 Chapter');source.add_paragraph('参考文献')
            source.add_paragraph('Unnumbered reference.')
            source.add_paragraph('[1] '+long_ref);source.save(work/'source.docx')
            output=Document();output.add_paragraph('1 Chapter');output.add_paragraph('参考文献')
            output.add_paragraph('[1] Unnumbered reference.')
            output.add_paragraph('[2] '+long_ref);output.save(work/'output.docx')
            with fitz.open() as pdf:
                page=pdf.new_page()
                page.insert_textbox(fitz.Rect(70,70,520,700), 'Chapter\n参考文献\n[1] Unnumbered reference.\n[2] '+long_ref, fontname='china-s', fontsize=10)
                pdf.save(work/'output.pdf')
            report=verify(work/'source.docx',work/'output.docx',work/'output.pdf',work/'report.json')
            self.assertTrue(report['passed'])

    def test_missing_toc_entry_is_warning_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sample.pdf'
            with fitz.open() as pdf:
                page=pdf.new_page();page.insert_text((290,800),'1');pdf.save(path)
            cfg=profile.load('hkjjxy');cfg['toc']['front_entries']=[]
            warnings=[]
            result=toc.read_entries(cfg,{'toc':[(1,'Missing')],'tables':[],'figures':[]},path,log=warnings.append)
            self.assertEqual(result['toc'], [('Missing','待核对')])
            self.assertTrue(warnings)

    def test_content_gate_rejects_missing_paragraph(self):
        with tempfile.TemporaryDirectory() as work:
            source=Path(work)/'source.docx'; output=Path(work)/'output.docx'; pdfpath=Path(work)/'out.pdf'
            doc=Document();doc.add_paragraph('1 Chapter');doc.add_paragraph('Required sentence');doc.save(source)
            Document().save(output)
            with fitz.open() as pdf:
                pdf.new_page().insert_text((70,100),'Unrelated')
                pdf.save(pdfpath)
            report=verify(source,output,pdfpath,Path(work)/'validation.json')
            self.assertFalse(report['passed'])
            self.assertTrue(report['warnings'])
            with self.assertRaisesRegex(ValueError,'保全'):
                verify(source,output,pdfpath,Path(work)/'validation.json', strict=True)
