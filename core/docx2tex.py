# -*- coding: utf-8 -*-
"""Word 稿（.docx）→ main.tex（+ figures/）。

通用启发式，不为某一篇硬编码。转完务必人工过一遍 tex，再看一眼 PDF。

识别规则：
  * 章节标题靠编号格式识别（1 / 1.1 / 1.1.1 / （1））；**没有编号的标题识别不出来**
  * 图表按「图/表 X-Y：题名」识别，编号重排为全文连续（图 1…N、表 1…N）
  * 参考文献取「参考文献」之后的连续段落，逐条 \\item
  * 封面信息表、声明页自动跳过（以摘要位置为界）

已知会误判的地方，改稿时留意：
  * 以数字开头但不是标题的正文行（如「2024 年…」）→ 靠长度 + 无句读兜底，仍可能漏网
  * 表格里含换行 / 合并单元格 → 会被压成一行文本
"""
import os
import re
import zipfile

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

RE_TITLE_FIELD = re.compile(r"^题目[:：]\s*(.*)$")
RE_ABS_CN = re.compile(r"^摘\s*要[:：]\s*(.*)$")
RE_KEY_CN = re.compile(r"^关键词[:：]\s*(.*)$")
RE_ABS_EN = re.compile(r"^Abstract[:：]\s*(.*)$", re.I)
RE_KEY_EN = re.compile(r"^Key\s*words?[:：]\s*(.*)$", re.I)
RE_REF_HEAD = re.compile(r"^参考文献\s*$")
RE_ACK_HEAD = re.compile(r"^致\s*谢\s*$")
RE_TABTITLE = re.compile(r"^表\s*(\d+)\s*[-—－]\s*(\d+)\s*[:：]?\s*(.*)$")
RE_FIGTITLE = re.compile(r"^图\s*(\d+)\s*[-—－]\s*(\d+)\s*[:：]?\s*(.*)$")
RE_NOTE = re.compile(r"^(数据来源|资料来源|注)[:：]")
RE_H1 = re.compile(r"^(\d{1,2})(?![\d\-.])[\s\u3000]*(\S.*)$")
RE_H2 = re.compile(r"^(\d{1,2})\.(\d{1,2})[\s\u3000]*(\S.*)$")
RE_H3 = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,2})[\s\u3000]*(\S.*)$")
RE_H4 = re.compile(r"^[（(](\d+)[)）]\s*(.+)$")
RE_CITE = re.compile(r"\[(\d+(?:\s*[,，]\s*\d+)*)\]")

MAX_HEADING_LEN = 32


def esc(s):
    """LaTeX 转义。反斜杠必须最先处理"""
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"),
                 ("$", r"\$"), ("#", r"\#"), ("_", r"\_"),
                 ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"),
                 ("^", r"\textasciicircum{}")):
        s = s.replace(a, b)
    return s


def read_items(doc):
    """按文档流读取。段落带原始段落号 pno；表格记录它「紧跟在哪一段之后」"""
    items, pno = [], 0
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            imgs = []
            for blip in child.iter(qn("a:blip")):
                rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
                if rid and rid in doc.part.related_parts:
                    imgs.append(str(doc.part.related_parts[rid].partname))
            items.append({"kind": "p", "pno": pno,
                          "text": p.text.strip(), "imgs": imgs})
            pno += 1
        elif tag == "tbl":
            t = Table(child, doc)
            rows = [[c.text.strip().replace("\n", " ") for c in r.cells]
                    for r in t.rows]
            items.append({"kind": "tbl", "pno": pno, "rows": rows})
    return items


def locate(items):
    """定位摘要、英文摘要、参考文献、致谢所在的 items 下标"""
    m = {"abs_cn": None, "key_cn": None, "abs_en": None, "key_en": None,
         "ref": None, "ack": None}
    for i, it in enumerate(items):
        if it["kind"] != "p":
            continue
        t = it["text"]
        if m["abs_cn"] is None and RE_ABS_CN.match(t):
            m["abs_cn"] = i
        elif m["key_cn"] is None and RE_KEY_CN.match(t):
            m["key_cn"] = i
        elif m["abs_en"] is None and RE_ABS_EN.match(t):
            m["abs_en"] = i
        elif m["key_en"] is None and RE_KEY_EN.match(t):
            m["key_en"] = i
        elif m["ref"] is None and RE_REF_HEAD.match(t):
            m["ref"] = i
        elif m["ack"] is None and RE_ACK_HEAD.match(t):
            m["ack"] = i
    return m


def extract_figures(docx_path, items, out_dir, body_start, log=print):
    """正文区图片 → figures/figN.ext；返回 {原名: 新名} 与顺序列表"""
    z = zipfile.ZipFile(docx_path)
    figdir = os.path.join(out_dir, "figures")
    os.makedirs(figdir, exist_ok=True)
    figmap, order, seq = {}, [], 0
    for it in items:
        if it["kind"] != "p" or not it.get("imgs"):
            continue
        if it["pno"] < body_start:
            continue
        for m in it["imgs"]:
            name = m.split("/")[-1]
            if name in figmap:
                continue
            seq += 1
            new = "fig%d%s" % (seq, os.path.splitext(name)[1])
            try:
                data = z.read("word/media/" + name)
            except KeyError:
                seq -= 1
                continue
            with open(os.path.join(figdir, new), "wb") as f:
                f.write(data)
            figmap[name] = new
            order.append(new)
    if seq:
        log("  提取图片 %d 张" % seq)
    return figmap, order


def _is_heading_line(s):
    return len(s) <= MAX_HEADING_LEN and "。" not in s and "；" not in s


def _superscripts(s):
    """正文里的 [1] / [1,2] → \\textsuperscript{[1]}"""
    return RE_CITE.sub(lambda m: r"\textsuperscript{[%s]}" % m.group(1), s)


def scan(items, marks, figmap):
    """扫描文档流，产出 blocks / refs / ack"""
    body_start = marks["body_start"]
    ref_pno = items[marks["ref"]]["pno"] if marks["ref"] is not None else None
    ack_pno = items[marks["ack"]]["pno"] if marks["ack"] is not None else None

    # ---- 先建「表 X-Y → 全文序号」映射（按表出现顺序）----
    tabmap, keys = {}, []
    for it in items:
        if it["kind"] == "p":
            m = RE_TABTITLE.match(it["text"])
            if m and it["pno"] >= body_start:
                keys.append("%s-%s" % (m.group(1), m.group(2)))
    for i, k in enumerate(keys, 1):
        tabmap.setdefault(k, i)

    def resolve(s):
        s = re.sub(r"表\s*(\d+)\s*[-—－]\s*(\d+)",
                   lambda m: r"表~\ref{tab:t%d}"
                   % tabmap.get("%s-%s" % (m.group(1), m.group(2)), 1), s)
        s = re.sub(r"图\s*(\d+)\s*[-—－]\s*(\d+)",
                   lambda m: r"图~\ref{fig:f%d}"
                   % tabmap.get("%s-%s" % (m.group(1), m.group(2)), 1), s)
        return s

    blocks, refs, ack = [], [], []
    pending_title = ""
    pend_note = ""
    tab_seq = 0
    fig_seq = 0

    i = 0
    while i < len(items):
        it = items[i]

        # ---------- 表格 ----------
        if it["kind"] == "tbl":
            if it["pno"] >= body_start and it["rows"]:
                tab_seq += 1
                blocks.append({"t": "table", "no": tab_seq,
                               "title": pending_title, "note": pend_note,
                               "rows": it["rows"]})
                pending_title, pend_note = "", ""
            i += 1
            continue

        pno, t = it["pno"], it["text"]

        # ---------- 图片 ----------
        if pno >= body_start and it.get("imgs"):
            nm = it["imgs"][0].split("/")[-1]
            if nm in figmap:
                cap = ""
                for j in range(i + 1, min(i + 4, len(items))):
                    nx = items[j]
                    if nx["kind"] == "p" and nx["text"]:
                        mm = RE_FIGTITLE.match(nx["text"])
                        cap = mm.group(3).strip() if mm else re.sub(
                            r"^图\s*[\d\-．.]+[：:]?\s*", "", nx["text"]).strip()
                        i = j       # 图题段已被消费
                        break
                fig_seq += 1
                blocks.append({"t": "figure", "no": fig_seq,
                               "path": "figures/" + figmap[nm], "cap": cap})
            i += 1
            continue

        if pno >= body_start and RE_FIGTITLE.match(t):
            i += 1
            continue

        # ---------- 参考文献区 ----------
        if ref_pno is not None and pno >= ref_pno:
            if t and not RE_REF_HEAD.match(t):
                refs.append(t)
            i += 1
            continue

        # ---------- 致谢区 ----------
        if ack_pno is not None and pno >= ack_pno:
            if t and not RE_ACK_HEAD.match(t):
                    ack.append(t)
            i += 1
            continue

        # ---------- 表题 / 表注 ----------
        mt = RE_TABTITLE.match(t)
        if mt and pno >= body_start:
            pending_title = mt.group(3).strip()
            i += 1
            continue
        if RE_NOTE.match(t) and pno >= body_start:
            pend_note = t
            i += 1
            continue

        # ---------- 标题 ----------
        if pno >= body_start:
            m3 = RE_H3.match(t)
            m2 = RE_H2.match(t)
            m1 = RE_H1.match(t)
            m4 = RE_H4.match(t)
            if m3:
                blocks.append({"t": "sec3", "text": m3.group(4)})
            elif m2:
                blocks.append({"t": "sec2", "text": m2.group(3)})
            elif m1 and _is_heading_line(t):
                blocks.append({"t": "sec1", "text": m1.group(2)})
            elif m4 and len(m4.group(2)) <= 26:
                blocks.append({"t": "sec4", "text": m4.group(2)})
            elif t:
                blocks.append({"t": "body", "text": t})
        i += 1

    return blocks, refs, ack


def render(blocks, refs, ack, meta, cfg, figmap, fig_order):
    pkg = (cfg or {}).get("latex", {}).get("package_name", "thesis-format")
    pages = int((cfg or {}).get("cover", {}).get("pages", 2))
    page_list = ",".join(str(i) for i in range(1, pages + 1))
    wfrac = float((cfg or {}).get("figure", {}).get("default_width_frac", 0.82))

    def E(s):
        return esc(s or "")

    out = [
        "%" + "=" * 68,
        "%%  《%s》" % meta["cn_title"],
        "%%  由 core/docx2tex.py 从 Word 稿自动转换 —— 转完请人工复核",
        "%%  编译：XeLaTeX 运行 2~3 次",
        "%" + "=" * 68,
        r"\documentclass[12pt,a4paper,fontset=windows,UTF8]{ctexart}",
        r"\usepackage{%s}" % pkg,
        r"\usepackage{pdfpages}",
        "",
        r"\cnTitle{" + E(meta["cn_title"]) + "}",
        r"\enTitle{" + E(meta["en_title"]) + "}",
        "",
        r"\begin{document}",
        r"\includepdf[pages={%s},pagecommand={}]{front.pdf}" % page_list,
        r"\tocpage",
        "",
        r"\setcounter{page}{1}",
        r"\pagenumbering{arabic}",
        "",
        r"\cnTitleBlock",
        "",
        r"\cnAbstract{%",
        "  " + E(meta["cn_abstract"]),
        "}",
        "",
        r"\cnKeywords{" + E(meta["cn_keywords"]) + "}",
        "",
        r"\enTitleBlock",
        "",
        r"\enAbstract{%",
        "  " + E(meta["en_abstract"]),
        "}",
        "",
        r"\enKeywords{" + E(meta["en_keywords"]) + "}",
        "",
        r"\newpage",
        "",
    ]

    for b in blocks:
        k = b["t"]
        if k == "sec1":
            out.append(r"\section{%s}" % E(b["text"]))
        elif k == "sec2":
            out.append(r"\subsection{%s}" % E(b["text"]))
        elif k == "sec3":
            out.append(r"\subsubsection{%s}" % E(b["text"]))
        elif k == "sec4":
            out.append(r"\paragraph*{%s}" % E(b["text"]))
        elif k == "body":
            out.append(_superscripts(E(b["text"])))
            out.append("")
        elif k == "table":
            rows = b["rows"]
            ncol = len(rows[0]) if rows else 1
            out.append(r"\begin{thesTab}[htbp]{%s\label{tab:t%d}}{%s}"
                       % (E(b["title"]), b["no"], E(b["note"])))
            out.append(r"\begin{tabularx}{\linewidth}{X" + " c" * (ncol - 1) + "}")
            out.append(r"  \toprule")
            for ri, row in enumerate(rows):
                out.append("  " + " & ".join(E(c) for c in row) + r" \\")
                if ri == 0:
                    out.append(r"  \midrule")
            out.append(r"  \bottomrule")
            out.append(r"\end{tabularx}")
            out.append(r"\end{thesTab}")
            out.append("")
        elif k == "figure":
            out.append(r"\begin{figure}[htbp]")
            out.append(r"  \centering")
            out.append(r"  \includegraphics[width=%.2f\linewidth]{%s}"
                       % (wfrac, b["path"]))
            out.append(r"  \caption{%s}\label{fig:f%d}" % (E(b["cap"]), b["no"]))
            out.append(r"\end{figure}")
            out.append("")

    out += ["", r"\newpage", r"\vspace*{3\baselineskip}",
            r"\unnumsec{参考文献}", "", r"\begin{thesref}"]
    out += ["  " + r"\item " + _superscripts(E(r)) for r in refs]
    out += [r"\end{thesref}", ""]

    if ack:
        out += ["", r"\newpage", r"\vspace*{3\baselineskip}",
                r"\unnumsec{致谢}", ""]
        out += [E(a) for a in ack]
        out += [""]

    out += [r"\end{document}", ""]
    return "\n".join(out)


def convert(docx_path, out_dir, cfg=None, log=print):
    """docx → out_dir/main.tex + out_dir/figures/。返回 (tex 文本, 统计 dict)"""
    doc = Document(docx_path)
    items = read_items(doc)
    marks = locate(items)
    marks["body_start"] = _body_start(items, marks)
    figmap, fig_order = extract_figures(docx_path, items, out_dir,
                                        marks["body_start"], log=log)
    blocks, refs, ack = scan(items, marks, figmap)
    meta = _meta(items, marks)
    tex = render(blocks, refs, ack, meta, cfg, figmap, fig_order)
    stat = {
        "tables": sum(1 for b in blocks if b["t"] == "table"),
        "figures": sum(1 for b in blocks if b["t"] == "figure"),
        "refs": len(refs),
        "sec1": sum(1 for b in blocks if b["t"] == "sec1"),
        "sec2": sum(1 for b in blocks if b["t"] == "sec2"),
        "sec3": sum(1 for b in blocks if b["t"] == "sec3"),
        "sec4": sum(1 for b in blocks if b["t"] == "sec4"),
        "title": meta["cn_title"],
    }
    return tex, stat


def _body_start(items, marks):
    """正文从哪一段开始：英文关键词之后；没有英文关键词就用中文关键词"""
    idx = marks["key_en"] if marks["key_en"] is not None else marks["key_cn"]
    if idx is None:
        return 0
    return items[idx]["pno"] + 1


def _meta(items, marks):
    def txt(i):
        if i is None:
            return ""
        return items[i]["text"]

    def grab(i, rx):
        if i is None:
            return ""
        m = rx.match(items[i]["text"])
        return m.group(1).strip() if m else ""

    cn_title = ""
    for it in items:
        if it["kind"] == "p":
            m = RE_TITLE_FIELD.match(it["text"])
            if m and m.group(1).strip():
                cn_title = m.group(1).strip()
                break

    en_title = ""
    if marks["abs_en"] is not None:
        for j in range(marks["abs_en"] - 1, -1, -1):
            s = items[j]["text"]
            if s and not RE_ABS_CN.match(s) and not RE_KEY_CN.match(s) \
                    and not RE_ABS_EN.match(s):
                en_title = s
                break

    return {
        "cn_title": cn_title or "（在此填写中文题目）",
        "cn_abstract": grab(marks["abs_cn"], RE_ABS_CN),
        "cn_keywords": grab(marks["key_cn"], RE_KEY_CN),
        "en_title": en_title or "（English Title Here）",
        "en_abstract": grab(marks["abs_en"], RE_ABS_EN),
        "en_keywords": grab(marks["key_en"], RE_KEY_EN),
    }
