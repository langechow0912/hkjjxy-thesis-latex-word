# -*- coding: utf-8 -*-
"""把 main.tex 解析成与排版无关的中间结构。

职责：只负责「读懂 tex」，不碰 Word / PDF 的任何细节。
新增学校时这里通常不用改；只有当该校 tex 引入新的自定义命令时才需要加分支
（加之前先想清楚：能不能放进 format.sty 里变成标准命令，那样更省事）。

输出的 block 字典：
  {"kind": "heading", "level": 1|2|3, "text": "1　引言"}
  {"kind": "text",    "text": "……"}
  {"kind": "table",   "name": "表名", "note": "表注", "rows": [[...]]}
  {"kind": "figure",  "path": "figures/fig1.png", "frac": 0.82, "caption": "图题"}
  {"kind": "ref",     "text": "作者. 题名[J]..."}
  {"kind": "break"}
"""
import re

#: 正文扫描时整行忽略的命令。注意 \newpage 不在其中——它是「动作」不是「噪音」。
SKIP = (
    "\\setcounter", "\\pagenumbering", "\\pagestyle", "\\cnTitleBlock",
    "\\enTitleBlock", "\\includepdf", "\\phantomsection", "\\addcontentsline",
    "\\documentclass", "\\usepackage", "\\cnTitle{", "\\cnSubTitle{", "\\enTitle{",
    "\\begin{document}", "\\end{document}", "\\toprule", "\\midrule", "\\bottomrule",
    "\\begin{tabularx}", "\\end{tabularx}", "\\begin{table}", "\\end{table}",
    "\\vspace", "\\tocpage", "\\cnKeywords{", "\\enKeywords{", "\\listoffigures",
    "\\listoftables", "\\tableofcontents",
)

_META_PATTERNS = {
    "cn_title": r"\\cnTitle\{([^}]*)\}",
    "cn_subtitle": r"\\cnSubTitle\{([^}]*)\}",
    "en_title": r"\\enTitle\{([^}]*)\}",
    "cn_abstract": r"\\cnAbstract\{%([\s\S]*?)\n\}",
    "en_abstract": r"\\enAbstract\{%([\s\S]*?)\n\}",
    "cn_keywords": r"\\cnKeywords\{([^}]*)\}",
    "en_keywords": r"\\enKeywords\{([^}]*)\}",
}


def normalize(text):
    """统一换行为 \\n。

    Windows 上写出的 .tex/.sty 常带 \\r\\r\\n，会让 `$` 结尾类正则和
    lines.index() 失配，症状是「解析错位」。所有入口第一件事就是归一化。
    """
    return text.replace("\r\r\n", "\n").replace("\r\n", "\n").replace("\r", "\n")


def unesc(s):
    """去掉 LaTeX 转义，还原成 Word 里该显示的字符"""
    s = s.replace(r"\&", "&").replace(r"\%", "%").replace(r"\#", "#")
    s = s.replace(r"\_", "_").replace(r"\{", "{").replace(r"\}", "}").replace("~", " ")
    return re.sub(r"\s+", " ", s).strip()


def extract_meta(tex):
    meta = {}
    for key, pat in _META_PATTERNS.items():
        m = re.search(pat, tex)
        val = m.group(1).strip() if m else ""
        meta[key] = unesc(val) if key.endswith(("abstract", "keywords")) else val
    return meta


def scan_tables(lines):
    """扫描 \\begin{thesTab}{表名\\label{tab:tN}}{表注} ... \\end{thesTab}"""
    tables, labels = [], {}
    k = 0
    while k < len(lines):
        if "\\begin{thesTab}" in lines[k]:
            m = re.search(r"\{([^{}]*?)\\label\{(tab:t\d+)\}\}\{([^{}]*)\}\s*$", lines[k])
            if not m:
                k += 1
                continue
            tname, tlab, tnote = m.group(1).strip(), m.group(2), m.group(3).strip()
            labels[tlab.split(":")[1]] = len(tables) + 1
            rows = []
            k += 1
            while k < len(lines) and "\\end{thesTab}" not in lines[k]:
                l2 = lines[k].strip()
                if l2.endswith("\\\\") and not l2.startswith("\\"):
                    rows.append([unesc(c.strip()) for c in l2[:-2].split("&")])
                k += 1
            tables.append({"name": tname, "note": tnote, "rows": rows})
        k += 1
    return tables, labels


def scan_figures(lines):
    """扫描 figure 环境，取出图片路径、宽度比例、图题"""
    figures, labels = [], {}
    k = 0
    while k < len(lines):
        if "\\begin{figure}" in lines[k]:
            blk = []
            k += 1
            while k < len(lines) and "\\end{figure}" not in lines[k]:
                blk.append(lines[k])
                k += 1
            blk = "\n".join(blk)
            mi = re.search(r"includegraphics\[([^\]]*)\]\{([^}]*)\}", blk)
            mc = re.search(r"caption\{([^{}]*)\}", blk)
            ml = re.search(r"\\label\{fig:([^}]*)\}", blk)
            frac = 0.82
            if mi:
                mw = re.search(r"width=([\d.]+)\\linewidth", mi.group(1))
                if mw:
                    frac = float(mw.group(1))
            if ml:
                labels[ml.group(1)] = len(figures) + 1
            figures.append({
                "path": mi.group(2) if mi else "",
                "frac": frac,
                "caption": mc.group(1).strip() if mc else "",
            })
        k += 1
    return figures, labels


def _skip_block(lines, k, end_pred):
    """跳到满足 end_pred 的行之后"""
    while k < len(lines) and not end_pred(lines[k]):
        k += 1
    return k + 1


def scan_body(lines, tables, figures, tab_labels, fig_labels):
    """逐行扫描 \\begin{document} 之后的内容，产出 block 列表与目录条目"""
    def resolve(s):
        s = re.sub(r"~?\\ref\{tab:(t\d+)\}",
                   lambda m: "表" + str(tab_labels.get(m.group(1), "?")), s)
        s = re.sub(r"~?\\ref\{fig:([^}]*)\}",
                   lambda m: "图" + str(fig_labels.get(m.group(1), "?")), s)
        return s

    blocks = []
    toc_entries = []
    cnt = [0, 0, 0]
    buf = []
    refn = 0
    tab_iter = iter(tables)
    fig_iter = iter(figures)

    def flush():
        if buf:
            text = resolve(unesc(" ".join(buf)))
            if text:
                blocks.append({"kind": "text", "text": text})
            del buf[:]

    i0 = lines.index("\\begin{document}")
    k = i0 + 1
    while k < len(lines):
        ln = lines[k].strip()

        if ln.startswith("%") or ln == "":
            flush()
            k += 1
            continue

        # ---- \newpage：真正的分页动作，绝不能当噪音跳过 ----
        if ln.startswith("\\newpage"):
            flush()
            blocks.append({"kind": "break"})
            rest = ln[len("\\newpage"):].strip()
            if rest:
                lines[k] = rest
                continue
            k += 1
            continue

        # ---- 一体化三线表 ----
        if "\\begin{thesTab}" in ln:
            flush()
            t = dict(next(tab_iter))
            t["kind"] = "table"
            blocks.append(t)
            k = _skip_block(lines, k, lambda s: "\\end{thesTab}" in s)
            continue

        # ---- 图片 ----
        if "\\begin{figure}" in ln:
            flush()
            f = dict(next(fig_iter))
            f["kind"] = "figure"
            blocks.append(f)
            k = _skip_block(lines, k, lambda s: "\\end{figure}" in s)
            continue

        # ---- 各级标题 ----
        m = re.match(r"\\section(\*)?\{(.+)\}\s*$", ln)
        if m:
            flush()
            title = resolve(unesc(m.group(2)))
            if m.group(1):
                blocks.append({"kind": "heading", "level": 1, "text": title})
                toc_entries.append((1, title))
            else:
                cnt[0] += 1
                cnt[1] = cnt[2] = 0
                t = "%d　%s" % (cnt[0], title)
                blocks.append({"kind": "heading", "level": 1, "text": t})
                toc_entries.append((1, t))
            k += 1
            continue

        m = re.match(r"\\subsection\{(.+)\}\s*$", ln)
        if m:
            flush()
            cnt[1] += 1
            cnt[2] = 0
            t = "%d.%d　%s" % (cnt[0], cnt[1], resolve(unesc(m.group(1))))
            blocks.append({"kind": "heading", "level": 2, "text": t})
            toc_entries.append((2, t))
            k += 1
            continue

        m = re.match(r"\\subsubsection\{(.+)\}\s*$", ln)
        if m:
            flush()
            cnt[2] += 1
            t = "%d.%d.%d　%s" % (cnt[0], cnt[1], cnt[2], resolve(unesc(m.group(1))))
            blocks.append({"kind": "heading", "level": 3, "text": t})
            toc_entries.append((3, t))
            k += 1
            continue

        m = re.match(r"\\paragraph\*?\{(.+)\}\s*$", ln)
        if m:
            flush()
            blocks.append({"kind": "subheading",
                           "text": resolve(unesc(m.group(1)))})
            k += 1
            continue

        # ---- 不带编号的一级块（参考文献 / 致谢）----
        if ln.startswith("\\unnumsec{"):
            flush()
            t = resolve(unesc(ln[len("\\unnumsec{"):-1]))
            blocks.append({"kind": "unnum", "text": t})
            toc_entries.append((1, t))
            k += 1
            continue

        if "\\begin{thesref}" in ln or "\\end{thesref}" in ln:
            flush()
            k += 1
            continue

        if ln.startswith("\\item "):
            refn += 1
            blocks.append({"kind": "ref",
                           "text": "[%d] " % refn + resolve(unesc(ln[6:]))})
            k += 1
            continue

        # ---- 中英文摘要多行块：由 meta 显式输出，此处整块跳过 ----
        if ln.startswith(("\\cnAbstract{", "\\enAbstract{")):
            flush()
            k = _skip_block(lines, k, lambda s: s.rstrip() == "}")
            continue

        if ln.startswith(SKIP):
            flush()
            k += 1
            continue

        buf.append(ln)
        k += 1

    flush()
    return blocks, toc_entries


def parse(tex):
    """tex 源码 → 中间结构"""
    tex = normalize(tex)
    lines = tex.split("\n")
    meta = extract_meta(tex)
    tables, tab_labels = scan_tables(lines)
    figures, fig_labels = scan_figures(lines)
    blocks, toc_entries = scan_body(lines, tables, figures, tab_labels, fig_labels)
    return {
        "meta": meta,
        "blocks": blocks,
        "toc": toc_entries,
        "tables": tables,
        "figures": figures,
    }
