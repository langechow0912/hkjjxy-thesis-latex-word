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
    scratch.mkdir()
    source = work / 'source.docx'
    source.write_bytes(payload)
    try:
        job['stage'] = '识别论文结构'
        tex, _ = docx2tex.convert(str(source), str(work), cfg,
                                log=lambda *a: None)
        struct = texparse.parse(tex)
        if not struct['blocks']:
            raise ValueError('未识别到论文正文，请检查 Word 内容与标题样式')
        struct['meta'].update(cn_title=meta['title'])
        for field, key in [('titleEn', 'en_title'), ('subTitle', 'cn_subtitle')]:
            if meta.get(field):
                struct['meta'][key] = meta[field]
        cover = work / 'cover.docx'
        prepare_cover(cfg, meta, cover)
        job['stage'] = '按学校规范生成目录与正文'
        entries = toc.probe(cfg, struct, str(work / 'figures'), str(cover),
                            str(scratch), log=lambda *a: None)
        output = work / 'formatted.docx'
        docxbuild.build(cfg, struct, str(output), str(work / 'figures'),
                        str(cover), entries)
        job['stage'] = '生成 PDF 预览'
        pdf = compiler.convert_pdf(cfg, str(output), str(work), log=lambda *a: None)
        from .preview_watermark import haikou_preview
        pdf = haikou_preview(pdf, cfg.get('id'))
        job['files'] = {'docx': str(output), 'pdf': pdf}
        job['suggestions'] = ['请补全封面的学号、二级学院和日期，并核对目录、图表及分页。']
        job['stage'] = '排版完成'
        job['state'] = 'done'
    except Exception:
        job['stage'] = '学校排版失败，请检查原稿结构或联系维护人员'
        job['state'] = 'error'
        import traceback
        job['log'].append(traceback.format_exc())
