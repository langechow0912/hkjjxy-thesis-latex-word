"""School-profile formatter used by the unified upload flow.

Each job owns its source, figures and outputs. The existing school Word builder
is the source of the preview PDF too, so both downloads have the same layout.
"""
from pathlib import Path
import shutil

from . import compiler, docx2tex, docxbuild, profile, texparse, toc, tools


def validate(school):
    cfg = profile.load(school)
    if not Path(profile.path(cfg, cfg['cover']['template'])).is_file():
        raise RuntimeError('学校封面模板缺失')
    tools.resolve('soffice', cfg)
    return cfg


def prepare_cover(cfg, meta, target):
    # Keep the school's cover and declaration pages exactly as supplied.
    shutil.copyfile(profile.path(cfg, cfg['cover']['template']), target)


def run(job, payload, cfg, meta):
    work = Path(job['tmp'])
    scratch = work / 'probe'
    warnings = []
    def feedback(*messages):
        text = ' '.join(map(str, messages))
        if not text.startswith('  目录页码核验完成') and not text.startswith('  提取图片'):
            if text.strip() and text not in warnings:
                warnings.append(text)
    source = work / 'source.docx'
    try:
        scratch.mkdir(exist_ok=True)
        if meta.get('sourceSuffix', '.docx').lower() == '.doc':
            original = work / 'source.doc'
            original.write_bytes(payload)
            job['stage'] = '自动转换 DOC 为 DOCX'
            source = Path(compiler.convert_docx(cfg, str(original), str(work)))
        else:
            source.write_bytes(payload)
        job['stage'] = '识别论文结构'
        tex, stat = docx2tex.convert(str(source), str(work), cfg,
                                     log=feedback)
        struct = texparse.parse(tex)
        # 规则层能认定「这张表/这张图没有题名」时直接告诉用户，
        # 不要等到看 PDF 才发现少了个表题。
        for note in (stat or {}).get('notes', []):
            feedback(note)

        # 规则层拿不准的地方（没有编号的短行是不是标题、层级是否跳级）
        # 交给 DSH 判断；它只返回建议，不改原稿，失败也不影响排版。
        from .dsh import runner as dsh_runner
        stat = stat or {}
        dsh_runner.save_outline(work, stat)
        suspicious = bool((stat.get('numbering') or (None, None))[1])
        report = dsh_runner.run_preprocess(cfg, {
            'source': str(source),
            'work_dir': str(work),
            'meta': meta,
            'suspicious': suspicious,
            'items': stat.get('items') or [],
            'body_start': stat.get('body_start', 0),
            'resolved_levels': stat.get('resolved_levels') or {},
        }, job_id=job.get('id'))
        for message in dsh_runner.to_messages(report):
            feedback(message)
        if not struct['blocks']:
            feedback('未识别到章节结构，已继续生成，请核对正文内容。')
        if struct['meta'].get('cn_title', '') in ('', '（在此填写中文题目）'):
            struct['meta']['cn_title'] = meta['title']
        for field, key in [('titleEn', 'en_title'), ('subTitle', 'cn_subtitle')]:
            if meta.get(field):
                struct['meta'][key] = meta[field]
        cover = work / 'cover.docx'
        try:
            prepare_cover(cfg, meta, cover)
        except RuntimeError as e:
            feedback(str(e) + '，已使用默认封面继续生成。')
            # Create minimal cover as fallback
            from docx import Document
            doc = Document()
            doc.add_paragraph(meta.get('title', '论文题目'))
            doc.save(str(cover))
        job['stage'] = '按学校规范生成目录与正文'
        entries = toc.probe(cfg, struct, str(work / 'figures'), str(cover),
                            str(scratch), log=feedback)
        output = work / 'formatted.docx'
        docxbuild.build(cfg, struct, str(output), str(work / 'figures'),
                        str(cover), entries)
        job['stage'] = '生成 PDF 预览'
        for attempt in range(3):
            pdf = compiler.convert_pdf(cfg, str(output), str(work), log=feedback)
            actual_entries = toc.read_entries(cfg, struct, pdf, log=feedback)
            if actual_entries == entries:
                break
            entries = actual_entries
            docxbuild.build(cfg, struct, str(output), str(work / 'figures'), str(cover), entries)
        else:
            feedback('目录页码未稳定，已保留生成结果，请核对长标题和分页。')
            pdf = compiler.convert_pdf(cfg, str(output), str(work), log=feedback)
        job['stage'] = '核验内容保全与目录页码'
        from .format_validation import verify
        try:
            integrity = verify(str(source), str(output), pdf, str(work / 'validation.json'))
        except Exception:
            integrity = {'warnings': ['自动内容核验未能完成，结果已生成，请对照原稿检查。']}
            import traceback
            job['log'].append(traceback.format_exc())
        from .preview_watermark import haikou_preview
        pdf = haikou_preview(pdf, cfg.get('id'))
        job['files'] = {'docx': str(output), 'pdf': pdf}
        job['suggestions'] = ['请补全封面的学号、二级学院和日期，并核对目录、图表及分页。']
        job['suggestions'].extend(warnings)
        job['suggestions'].extend(integrity.get('warnings', []))
        job['stage'] = '排版完成（有待核对事项）' if warnings or integrity.get('warnings') else '排版完成'
        job['state'] = 'done'
    except Exception as exc:
        if isinstance(exc, ValueError):
            job['stage'] = str(exc)
        else:
            job['stage'] = '学校排版失败，请检查原稿结构或联系维护人员'
        job['state'] = 'error'
        import traceback
        job['log'].append(traceback.format_exc())
