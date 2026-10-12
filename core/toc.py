# -*- coding: utf-8 -*-
"""目录页码探测 —— 两遍构建的第一遍。

Word 的 TOC 域要到「更新域」时才算出页码，直接生成的 docx 打开是一串占位。
办法是：先用同样的内容生成一份探针 docx → LibreOffice 渲染成 PDF →
从 PDF 里读回每个标题实际落在第几页 → 第二遍把页码写进 TOC 域缓存。
这样用户不按 F9 也能看到正确页码，按 F9 也能重算。
"""
import glob
import os
import re
import subprocess

from . import docxbuild as D


def probe(cfg, struct, fig_dir, tpl_path, tmp_dir, log=print):
    """返回 {"toc": [(文本, 页码)…], "lot": […], "lof": […]}"""
    from . import compiler
    os.makedirs(tmp_dir, exist_ok=True)
    probe_docx = os.path.join(tmp_dir, "probe.docx")
    for f in glob.glob(os.path.join(tmp_dir, "probe.*")):
        os.remove(f)

    D.build(cfg, struct, probe_docx, fig_dir, tpl_path, None)
    probe_pdf = compiler.convert_pdf(cfg, probe_docx, tmp_dir, log=log)

    import fitz
    d = fitz.open(probe_pdf)
    texts = [d[i].get_text().replace("\u3000", " ") for i in range(d.page_count)]
    d.close()

    try:
        body_start = next(i for i, t in enumerate(texts) if "Key words" in t)
    except StopIteration:
        body_start = 0
        log("  警告：探针 PDF 中未找到 Key words，页码从第 1 页起算")

    cur = [body_start]

    def find_page(pattern):
        for i in range(cur[0], len(texts)):
            if re.search(pattern, texts[i]):
                cur[0] = i
                return i - body_start + 1
        return 1

    loose = set(cfg["toc"].get("loose_match", []))

    def pattern_of(disp):
        plain = disp.replace("\u3000", " ")
        if plain in loose:
            return r"\s*".join(re.escape(c) for c in plain)
        m = re.match(r"([\d.]+)\s+(.*)", plain)
        if m:
            return re.escape(m.group(1)) + r"\s*" + re.escape(m.group(2)[:8])
        return re.escape(plain[:6])

    entries = D.toc_entry_list(cfg, struct)
    toc_map = [(d_, find_page(pattern_of(d_))) for _, d_ in entries]

    cur[0] = body_start
    lot_map = [("表%d　%s" % (i, t["name"]), find_page(re.escape(t["name"][:8])))
               for i, t in enumerate(struct["tables"], 1)] if struct["tables"] else []

    cur[0] = body_start
    lof_map = [("图%d　%s" % (i, f["caption"]),
                find_page(r"图\s*%d\s*[\u3000\s]" % i + re.escape(f["caption"][:6])))
               for i, f in enumerate(struct["figures"], 1)] if struct["figures"] else []

    log("  页码探测：目录 %d 条 / 表目录 %d 条 / 图目录 %d 条"
        % (len(toc_map), len(lot_map), len(lof_map)))
    return {"toc": toc_map, "lot": lot_map, "lof": lof_map}


def cleanup(tmp_dir):
    for f in glob.glob(os.path.join(tmp_dir, "probe.*")):
        os.remove(f)
