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

    return read_entries(cfg, struct, probe_pdf, log=log)


def read_entries(cfg, struct, pdf_path, log=print, strict=False):
    """Read actual body page numbers; never fabricate a successful page match."""
    import fitz
    def compact(text):
        return re.sub(r"\s+", "", text)

    # Calculate footer height from bottom margin (default 30mm = ~85pt)
    # Use 70% of bottom margin to safely capture page number without body text
    bottom_margin_mm = cfg.get("page", {}).get("margins", {}).get("bottom", 30)
    if isinstance(bottom_margin_mm, str):
        # Handle string values like "30mm" by extracting the number
        bottom_margin_mm = float(re.search(r'[\d.]+', bottom_margin_mm).group())
    footer_height_pt = float(bottom_margin_mm) * 2.835 * 0.7  # mm to pt conversion, 70% safety margin

    with fitz.open(pdf_path) as pdf:
        texts = [compact(page.get_text(clip=fitz.Rect(0, 0, page.rect.width, page.rect.height - footer_height_pt))) for page in pdf]
        footers = [page.get_text(clip=fitz.Rect(0, page.rect.height - footer_height_pt, page.rect.width, page.rect.height)).strip() for page in pdf]
    start_value = int(cfg["page"].get("body_page_start", 1))
    starts = [i for i, footer in enumerate(footers) if footer == str(start_value)]
    if len(starts) != 1:
        if strict:
            raise ValueError("无法唯一定位正文起始页码，目录核验失败")
        log("正文起始页码无法唯一确认，目录仅供参考，请核对页码。")
        title = compact(struct.get('meta', {}).get('cn_title', ''))
        body_start = next((i for i, text in enumerate(texts) if title and title in text), 0)
    else:
        body_start = starts[0]
    for offset, footer in enumerate(footers[body_start:]):
        if footer != str(start_value + offset):
            if strict:
                raise ValueError("正文页码不连续，目录核验失败")
            log("正文页码不连续，已继续生成，请核对目录页码。")
            break

    def find(text, cursor):
        pattern = compact(text)
        if not pattern:
            if strict:
                raise ValueError("目录条目为空，不能生成页码")
            log("存在空目录条目，请核对目录。")
            return "待核对", cursor
        for i in range(cursor, len(texts)):
            if pattern in texts[i]:
                return start_value + i - body_start, i
        if strict:
            raise ValueError("目录条目无法定位：%s" % text)
        log("目录条目无法定位，页码标记为待核对：%s" % text)
        return "待核对", cursor

    result = {}
    groups = {
        "toc": [text for _, text in D.toc_entry_list(cfg, struct)],
        "lot": ["表%d　%s" % (i, table["name"]) for i, table in enumerate(struct["tables"], 1)],
        "lof": ["图%d　%s" % (i, figure["caption"]) for i, figure in enumerate(struct["figures"], 1)],
    }
    for key, labels in groups.items():
        cursor = body_start
        entries = []
        for label in labels:
            page, cursor = find(label, cursor)
            entries.append((label, page))
        result[key] = entries
    log("  目录页码核验完成")
    return result


def cleanup(tmp_dir):
    for f in glob.glob(os.path.join(tmp_dir, "probe.*")):
        os.remove(f)
