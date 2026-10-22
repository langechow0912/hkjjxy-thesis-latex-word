# -*- coding: utf-8 -*-
"""PDF 侧：同步格式包、抽取封面页、调用 XeLaTeX。

封面/声明页的口径（踩过坑，务必保持）：
  PDF 不自己排封面，而是「先生成 Word → 把 Word 的前 N 页转成 PDF → 用 pdfpages 嵌入」。
  早先直接拿源稿 docx 转 PDF 取前 2 页，LibreOffice 渲染会把表格/文本框挤压变形，
  声明页内容溢到封面上。统一改走「生成的 docx」后，PDF 与 Word 的这两页完全一致。
"""
import glob
import os
import shutil
import subprocess
import tempfile
import uuid

from . import tools


def sync_format_sty(cfg, work_dir, log=print):
    """把 schools/<id>/format.sty 同步到论文工作目录，文件名由 latex.package_name 决定"""
    name = cfg["latex"].get("package_name", "thesis-format")
    src = os.path.join(cfg["_dir"], "format.sty")
    if not os.path.isfile(src):
        raise SystemExit("学校目录缺少 format.sty：%s" % src)
    dst = os.path.join(work_dir, name + ".sty")
    if os.path.isfile(dst) and open(dst, "rb").read() == open(src, "rb").read():
        return dst
    shutil.copyfile(src, dst)
    log("  已同步格式包 → %s" % dst)
    return dst


def convert_pdf(cfg, src, outdir, log=print):
    """LibreOffice 转 PDF，返回生成的 pdf 路径。

    坑：LibreOffice 默认单实例。若已有 soffice 实例在跑，新的转换请求会静默返回、
    不产出任何文件。解决办法是给每次调用指定独立的 UserInstallation，
    让它可以并存；再配合重试兜底。
    """
    soffice = tools.resolve("soffice", cfg)
    expect = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    if os.path.isfile(expect):
        os.remove(expect)
    for attempt in (1, 2, 3):
        prof = os.path.join(tempfile.gettempdir(), "lo-" + uuid.uuid4().hex[:8])
        cmd = [soffice,
               "-env:UserInstallation=file:///" + prof.replace("\\", "/"),
               "--headless", "--convert-to", "pdf", "--outdir", outdir, src]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode == 0 and os.path.isfile(expect) and os.path.getsize(expect) > 0:
            return expect
        log("  LibreOffice 未产出，重试 %d/3" % attempt)
    raise RuntimeError("LibreOffice 转换失败：%s" % src)


def extract_front_pages(cfg, docx_path, out_pdf, tmp_dir, log=print):
    """把生成的 docx 前 N 页（封面 + 声明）单独存成 PDF，供 LaTeX 嵌入"""
    import fitz
    n = int(cfg["cover"].get("pages", 2))
    tmp_pdf = convert_pdf(cfg, docx_path, tmp_dir, log=log)
    src = fitz.open(tmp_pdf)
    out = fitz.open()
    out.insert_pdf(src, from_page=0, to_page=min(n, src.page_count) - 1)
    out.save(out_pdf)
    out.close()
    src.close()
    log("  封面页 → %s（%d 页）" % (os.path.basename(out_pdf), n))
    return out_pdf


def compile_pdf(cfg, work_dir, tex_name="main.tex", times=3, log=print):
    """XeLaTeX 编译若干遍，让目录与交叉引用收敛"""
    xelatex = tools.resolve("xelatex", cfg)
    for i in range(times):
        r = subprocess.run(
            [xelatex, "-interaction=nonstopmode", "-halt-on-error",
             "-disable-installer", tex_name],
            cwd=work_dir, capture_output=True)
        if r.returncode != 0:
            log("  XeLaTeX 第 %d 遍返回 %d" % (i + 1, r.returncode))
            detail = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace")[-3000:]
            raise RuntimeError("XeLaTeX 编译失败（第 %d 遍）：%s" % (i + 1, detail))
    pdf = os.path.join(work_dir, tex_name[:-4] + ".pdf")
    if not os.path.isfile(pdf) or os.path.getsize(pdf) == 0:
        raise RuntimeError("XeLaTeX 未生成有效 PDF")
    log("  XeLaTeX 编译 %d 遍完成" % times)
    return pdf
