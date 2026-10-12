# -*- coding: utf-8 -*-
"""一条命令产出某校某篇论文的可编辑 Word + PDF。

用法
    python -m core.build --school hkjjxy --sample "企业筹资风险研究"
    python -m core.build --school hkjjxy --sample "以岭药业财务风险分析" --no-pdf
    python -m core.build --school hkjjxy --sample "企业筹资风险研究" --suffix v2

流程
    1. 读 schools/<school>/profile.yaml              ← 学校数值真源
    2. 解析 samples/<school>/<sample>/main.tex
    3. pass1 探针 docx → LibreOffice → PDF → 读回每个标题的真实页码
    4. pass2 用真页码生成正式 docx（目录是 TOC 域，F9 可重算）
    5. 正式 docx 前 N 页 → front.pdf
    6. 同步 format.sty → XeLaTeX ×3 → main.pdf
"""
import argparse
import io
import os
import sys
import tempfile

from . import compiler, docxbuild, profile as P, texparse, toc


def resolve_paths(cfg, sample, suffix, tmp=None):
    work = os.path.join(P.SAMPLES, cfg["id"], sample)
    if not os.path.isdir(work):
        raise SystemExit("找不到样例目录：%s" % work)
    out_cfg = cfg.get("output", {})
    name_tpl = out_cfg.get("docx_name", "{sample}-毕业论文（可编辑）{suffix}.docx")
    docx_path = os.path.join(work, name_tpl.format(sample=sample, suffix=suffix))
    return {
        "work": work,
        "tex": os.path.join(work, cfg["latex"].get("main", "main.tex")),
        "docx": docx_path,
        "front": os.path.join(work, cfg["cover"].get("front_pdf", "front.pdf")),
        "fig": os.path.join(work, cfg["figure"].get("dir", "figures")),
        "tpl": P.path(cfg, cfg["cover"]["template"]),
        "tmp": tmp or tempfile.mkdtemp(prefix="thesis-"),
    }


def build_one(cfg, sample, suffix="", tmp=None, do_docx=True, do_pdf=True,
              log=print):
    paths = resolve_paths(cfg, sample, suffix, tmp)
    log("[%s / %s]" % (cfg["id"], sample))

    tex = io.open(paths["tex"], encoding="utf-8").read()
    struct = texparse.parse(tex)
    log("  解析：%d 块 / %d 表 / %d 图"
        % (len(struct["blocks"]), len(struct["tables"]), len(struct["figures"])))

    if do_docx:
        entries = toc.probe(cfg, struct, paths["fig"], paths["tpl"],
                            paths["tmp"], log=log)
        docxbuild.build(cfg, struct, paths["docx"], paths["fig"],
                        paths["tpl"], entries)
        log("  Word → %s" % os.path.basename(paths["docx"]))

    if do_pdf:
        compiler.extract_front_pages(cfg, paths["docx"], paths["front"],
                                     paths["tmp"], log=log)
        compiler.sync_format_sty(cfg, paths["work"], log=log)
        compiler.compile_pdf(cfg, paths["work"],
                             os.path.basename(paths["tex"]), log=log)
        log("  PDF  → main.pdf")

    toc.cleanup(paths["tmp"])
    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成毕业论文的 Word 与 PDF")
    ap.add_argument("--school", required=True, help="schools/ 下的学校目录名")
    ap.add_argument("--sample", required=True, help="samples/<school>/ 下的论文目录名")
    ap.add_argument("--suffix", default="", help="输出文件名后缀，如 v2")
    ap.add_argument("--tmp", default=None, help="临时目录（探针文件）")
    ap.add_argument("--no-docx", action="store_true", help="只出 PDF")
    ap.add_argument("--no-pdf", action="store_true", help="只出 Word")
    a = ap.parse_args(argv)

    cfg = P.load(a.school)
    paths = build_one(cfg, a.sample, a.suffix, a.tmp,
                      do_docx=not a.no_docx, do_pdf=not a.no_pdf)

    # 编译体检
    log_path = os.path.join(paths["work"], os.path.basename(paths["tex"])[:-4] + ".log")
    if os.path.isfile(log_path):
        txt = io.open(log_path, encoding="utf-8", errors="ignore").read()
        bad = [l for l in txt.split("\n") if "LaTeX Warning" in l or "Overfull" in l]
        print("编译提示：%d 条（Overfull / 未解析引用等）" % len(bad))
        for l in bad[:10]:
            print("   " + l.strip()[:110])
    return 0


if __name__ == "__main__":
    sys.exit(main())
