# -*- coding: utf-8 -*-
"""Word 稿（.docx）→ main.tex（+ figures/）。

通用启发式，不为某一篇硬编码。转完务必人工过一遍 tex，再看一眼 PDF。

识别规则：
  * 章节标题由 core/headings.py 通读全文后定级：认「第一章 / 一、/ （一）/ 1. / 1.1 /
    （1）/ ①」等编号，也认 Word 标题样式和加粗短行；一律剥掉原编号再按模板重新编号
  * 缺上级的标题压缩层级，超过模板四级的降为加粗正文
  * 图表按「图/表 X-Y：题名」识别，题注在图的下方、上方或同一段都认；
    编号重排为全文连续（图 1…N、表 1…N）
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

from . import figures, headings
from .headings import MAX_HEADING_LEN, cn2int  # noqa: F401  对外仍从这里取

RE_TITLE_FIELD = re.compile(r"^题目[:：]\s*(.*)$")
RE_ABS_CN = re.compile(r"^摘\s*要[:：]\s*(.*)$")
RE_KEY_CN = re.compile(r"^关键词[:：]\s*(.*)$")
RE_ABS_EN = re.compile(r"^Abstract[:：]\s*(.*)$", re.I)
RE_KEY_EN = re.compile(r"^Key\s*words?[:：]\s*(.*)$", re.I)
RE_REF_HEAD = re.compile(r"^参考文献\s*$")
RE_ACK_HEAD = re.compile(r"^致\s*谢\s*$")
# 编号可以是「1-1」两段式，也可以是全文连续的「1」。不做 \d+ 直接吃下去，
# 是因为按位置配题名/正文判断，不会因为放宽编号而误收正文。
# 英文稿常见 Table 1 / Figure 2，同样收进来。
RE_TABTITLE = re.compile(r"^表\s*(\d+)(?:\s*[-—－.．]\s*(\d+))?\s*[:：.．、]?\s*(.*)$")
RE_FIGTITLE = re.compile(r"^图\s*(\d+)(?:\s*[-—－.．]\s*(\d+))?\s*[:：.．、]?\s*(.*)$")
RE_TABTITLE_EN = re.compile(r"^Table\s*(\d+)(?:\.(\d+))?\s*[:：.]?\s*(.*)$", re.I)
RE_FIGTITLE_EN = re.compile(r"^Figure\s*(\d+)(?:\.(\d+))?\s*[:：.]?\s*(.*)$", re.I)
RE_NOTE = re.compile(r"^(数据来源|资料来源|注)[:：]")
RE_CITE = re.compile(r"\[(\d+(?:\s*[,，]\s*\d+)*)\]")
MAX_CAPTION_LEN = 60
RE_APPENDIX = re.compile(
    r"^附\s*录(?:\s*([A-Za-z]|\d{1,2}|[一二三四五六七八九十]{1,2}))?([\s　:：、.．]*)(.*)$")


def appendix_title(text):
    """「附录1 调查问卷」「附录A：访谈提纲」「附录」→ 规范化标题；不是附录标题返回 None。

    只认短的、不带句号的行；「附录见文末」这类正文句子（附录后面直接跟字、
    既没编号也没分隔）不算。
    """
    t = (text or "").strip()
    if len(t) > 40 or "。" in t:
        return None
    m = RE_APPENDIX.match(t)
    if not m:
        return None
    label, sep, rest = m.group(1) or "", m.group(2), m.group(3).strip()
    if not label and rest and not sep:
        return None
    return "附录" + label + ("　" + rest if rest else "")


def esc(s):
    """LaTeX 转义。反斜杠必须最先处理"""
    for a, b in (("\\", r"\textbackslash{}"), ("&", r"\&"), ("%", r"\%"),
                 ("$", r"\$"), ("#", r"\#"), ("_", r"\_"),
                 ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}"),
                 ("^", r"\textasciicircum{}")):
        s = s.replace(a, b)
    return s


def _para_text(p_el):
    """段落的完整文本，含域（w:fldSimple）里的缓存结果。

    python-docx 的 Paragraph.text 只拼 w:r/w:t，Word 的 SEQ 域编号（「表」后面
    那个 1）装在 w:fldSimple 里，会被整段丢掉，于是表题变成「表　资产负债率分析表」
    而认不出来。这里按文档顺序遍历所有 w:t，跳过修订删除的内容。
    """
    out = []
    for el in p_el.iter():
        tag = el.tag.split("}")[-1]
        if tag == "t":
            parent = el.getparent()
            while parent is not None:
                if parent.tag.split("}")[-1] == "del":
                    break
                parent = parent.getparent()
            else:
                out.append(el.text or "")
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def read_items(doc):
    """按文档流读取。段落带原始段落号 pno；表格记录它「紧跟在哪一段之后」"""
    items, pno = [], 0
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            imgs = figures.image_targets(child, doc)
            level = None
            style = p.style
            for _ in range(12):
                if style is None:
                    break
                outline = style.element.xpath('./w:pPr/w:outlineLvl/@w:val')
                match = re.fullmatch(r'(?:heading|标题)\s*([1-4])', style.name, re.I)
                if outline or match:
                    level = int(outline[0]) + 1 if outline else int(match.group(1))
                    break
                style = style.base_style
            direct = child.xpath('./w:pPr/w:outlineLvl/@w:val')
            if direct:
                level = int(direct[0]) + 1
            items.append({"kind": "p", "pno": pno,
                          "text": _para_text(child).strip(), "imgs": imgs,
                          "style": p.style.name if p.style is not None else "",
                          "bold": _all_bold(p),
                          "heading_level": level if level in (1, 2, 3, 4) else None})
            pno += 1
        elif tag == "tbl":
            t = Table(child, doc)
            rows = [[c.text.strip().replace("\n", " ") for c in r.cells]
                    for r in t.rows]
            items.append({"kind": "tbl", "pno": pno, "rows": rows})
            # 单元格里的图片 python-docx 读不到文字，原先整张图会丢。
            # 挪到表格后面作为独立图片段，并标记来源，后面据此提示用户。
            for target in figures.image_targets(child, doc):
                items.append({"kind": "p", "pno": pno, "text": "", "imgs": [target],
                              "style": "", "bold": False, "heading_level": None,
                              "from_table": True})
    return items


def _all_bold(p):
    """整段文字都加粗（run 自身或段落样式设了粗体）。空段落返回 False。"""
    runs = [r for r in p.runs if r.text.strip()]
    if not runs:
        return False
    try:
        style_bold = p.style.font.bold if p.style is not None else None
    except AttributeError:
        style_bold = None
    for r in runs:
        bold = r.bold
        if bold is None:
            bold = style_bold
        if not bold:
            return False
    return True


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
    figmap, order, seq, missing = {}, [], 0, []
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
                missing.append(name)
                continue
            with open(os.path.join(figdir, new), "wb") as f:
                f.write(data)
            figmap[name] = new
            order.append(new)
    if seq:
        log("  提取图片 %d 张" % seq)
    if missing:
        log("  警告：%d 张图片引用缺失实际文件（%s），已跳过" % (len(missing), ', '.join(missing[:3])))
    return figmap, order

_SEC_LEVEL = {"sec1": 1, "sec2": 2, "sec3": 3}


def number_headings(blocks):
    """原稿标题编号完整就沿用，否则按出现顺序自动编号。

    完整 = 章从 1 起连续，节、小节在所属上级下从 1 起连续。这恰好等于按顺序
    自动编出的号，所以两种情况 LaTeX 计数器给出的都是最终编号；这里只负责判断
    并找出第一处错误。返回 ("user", None) 或 ("auto", 错误说明)。
    """
    cnt = [0, 0, 0]
    problem = None
    for b in blocks:
        lv = _SEC_LEVEL.get(b["t"])
        if lv is None:
            continue
        cnt[lv - 1] += 1
        cnt[lv:] = [0] * (3 - lv)
        want = tuple(cnt[:lv])
        if b["src"] != want and problem is None:
            problem = "「%s %s」按顺序应为 %s" % (
                ".".join(map(str, b["src"])), b["text"], ".".join(map(str, want)))
    return ("auto", problem) if problem else ("user", None)


def _superscripts(s):
    """正文里的 [1] / [1,2] → \\textsuperscript{[1]}"""
    return RE_CITE.sub(lambda m: r"\textsuperscript{[%s]}" % m.group(1), s)


def _label_key(first, second):
    """表/图编号的两段式或单段式都归一成同一个键，供正文交叉引用查表"""
    return "%s-%s" % (first, second) if second else str(first)


def _caption_match(text, kind):
    """识别表题/图题，返回 (编号键, 题名) 或 None。

    中文「表1 / 表1-1 / 表 1：」和英文「Table 1 / Figure 2」都认。
    两段式编号优先，避免把「表1-1」拆成「表1」加题名「-1 …」。
    """
    pats = ((RE_TABTITLE, RE_TABTITLE_EN) if kind == "table"
            else (RE_FIGTITLE, RE_FIGTITLE_EN))
    for pat in pats:
        m = pat.match(text)
        if m:
            # 中文正则组 3 是题名，英文正则也是组 3
            title = (m.group(3) or "").strip()
            # 「图1显示了公司……。」是正文引用，不是题注：题名过长或以句末标点收尾就放过。
            if len(title) > MAX_CAPTION_LEN or title.endswith(("。", "；")):
                return None
            return _label_key(m.group(1), m.group(2)), title
    return None


# \ref 占位符：要先过 esc() 再还原成命令，否则反斜杠会被转义成字面文本。
# 用私用区字符，正文里不会自然出现。
_PH = {"tab": "\ue000", "fig": "\ue001"}


def _ph(kind, num):
    return "%s%d\ue002" % (_PH[kind], num)


def _unph(s):
    """占位符 → \\ref{tab:tN} / \\ref{fig:fN}"""
    s = re.sub(r"\ue000(\d+)\ue002", r"\\ref{tab:t\1}", s)
    return re.sub(r"\ue001(\d+)\ue002", r"\\ref{fig:f\1}", s)


def _heading_block(d):
    """headings.plan 的决定 → 文档块。level 超过 4 的降为加粗正文，保留原文含编号。"""
    level = d["level"]
    if level <= 3:
        return {"t": "sec%d" % level, "text": d["text"], "src": d["src"]}
    if level == 4:
        return {"t": "sec4", "text": d["text"]}
    return {"t": "body", "text": d["orig"], "bold": True}


def scan(items, marks, figmap):
    """扫描文档流，产出 blocks / refs / ack / 标题调整说明"""
    body_start = marks["body_start"]
    ref_pno = items[marks["ref"]]["pno"] if marks["ref"] is not None else None
    ack_pno = items[marks["ack"]]["pno"] if marks["ack"] is not None else None

    # 正文之后依次可能出现参考文献、致谢、附录，顺序不固定：
    # 每个标记管到下一个标记之前。附录区的图表照常处理，只是不再判标题。
    markers = []
    if ref_pno is not None:
        markers.append((ref_pno, "ref"))
    if ack_pno is not None:
        markers.append((ack_pno, "ack"))
    app_titles = {}
    for it in items:
        if it["kind"] == "p" and it["pno"] >= body_start and it["text"]:
            title = appendix_title(it["text"])
            if title is not None:
                app_titles[it["pno"]] = title
                markers.append((it["pno"], "app"))
    markers.sort()

    def zone(pno):
        found = None
        for start, name in markers:
            if start > pno:
                break
            found = name
        return found

    def not_heading_candidate(it):
        """题注、表注、参考文献/致谢/附录区的段落不参与标题判断"""
        pno, text = it["pno"], it["text"]
        if zone(pno) or RE_NOTE.match(text):
            return True
        mt = _caption_match(text, "table")
        return bool(mt and mt[1].strip()) or bool(_caption_match(text, "figure"))

    # 先通读全文给标题定级（编号体系、缺上级、超层级都在这里处理）
    hplan, hnotes = headings.plan(items, body_start, not_heading_candidate)

    # ---- 先建「表 X-Y / 表 X → 全文序号」映射（按表出现顺序）----
    tabmap, keys = {}, []
    for it in items:
        if it["kind"] == "p":
            m = _caption_match(it["text"], "table")
            if m and it["pno"] >= body_start:
                keys.append(m[0])
    for i, k in enumerate(keys, 1):
        tabmap.setdefault(k, i)
    # 图的映射在扫描时随图出现顺序填：图题的位置有三种写法，提前数不准。
    fig_keys = {}

    def resolve(s):
        # 正文里的「如表 1-1 / 如表 1 所示」换成 \ref。编号两段式优先，其次是单段。
        # 原稿常写成「如表 表1 所示」，重复的「表」一并收掉。
        # 这里只放占位符：正文后面还要过 esc()，直接写反斜杠会被转义成字面文本。
        s = re.sub(r"表\s*(?:表\s*)?(\d+)\s*[-—－.．]\s*(\d+)",
                   lambda m: _ph("tab", tabmap.get(_label_key(m.group(1), m.group(2)), 1)), s)
        s = re.sub(r"表\s*(?:表\s*)?(\d+)(?![\d\-—－.])",
                   lambda m: _ph("tab", tabmap.get(_label_key(m.group(1), None), 1)), s)
        # 图编号只查图自己的映射；查不到（原稿引用了不存在的图）就保持原文，不乱指。
        s = re.sub(r"图\s*(?:图\s*)?(\d+)\s*[-—－.．]\s*(\d+)",
                   lambda m: (_ph("fig", fig_keys[_label_key(m.group(1), m.group(2))])
                              if _label_key(m.group(1), m.group(2)) in fig_keys else m.group(0)), s)
        s = re.sub(r"图\s*(?:图\s*)?(\d+)(?![\d\-—－.])",
                   lambda m: (_ph("fig", fig_keys[_label_key(m.group(1), None)])
                              if _label_key(m.group(1), None) in fig_keys else m.group(0)), s)
        return s

    blocks, refs, ack = [], [], []
    pending_title = ""
    pending_fig = None      # 写在图上方的图题 (编号键, 题名)
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
        # 图题只认明确的「图 X…」写法，普通正文不会被当成题注吞掉。
        # 位置按优先级：图上方已经出现的图题 > 图和题注同一段 > 图下方紧邻的一段。
        if pno >= body_start and it.get("imgs"):
            cap_key, cap_title, end = None, "", i + 1
            if pending_fig:
                cap_key, cap_title = pending_fig
                pending_fig = None
            else:
                same = _caption_match(t, "figure") if t else None
                if same:
                    cap_key, cap_title = same
                    t = ""
                else:
                    nxt_i = i + 1
                    while (nxt_i < len(items) and items[nxt_i]["kind"] == "p"
                           and not items[nxt_i]["text"] and not items[nxt_i].get("imgs")):
                        nxt_i += 1
                    if (nxt_i < len(items) and items[nxt_i]["kind"] == "p"
                            and not items[nxt_i].get("imgs")):
                        below = _caption_match(items[nxt_i]["text"], "figure")
                        if below:
                            cap_key, cap_title = below
                            end = nxt_i + 1
            for index, image in enumerate(it["imgs"]):
                nm = image.split("/")[-1]
                if nm not in figmap:
                    blocks.append({"t": "body", "text": "［图片无法提取，请核对原稿］"})
                    continue
                fig_seq += 1
                if index == 0 and cap_key:
                    fig_keys.setdefault(cap_key, fig_seq)
                blocks.append({"t": "figure", "no": fig_seq,
                               "path": "figures/" + figmap[nm],
                               "cap": cap_title if index == 0 else "",
                               "from_table": bool(it.get("from_table"))})
            if t:
                blocks.append({"t": "body", "text": t})
            i = end
            continue

        # ---------- 附录标题：另起一页，格式同「参考文献」标题 ----------
        if pno in app_titles:
            blocks.append({"t": "appendix", "text": app_titles[pno]})
            i += 1
            continue

        # ---------- 参考文献区 ----------
        if zone(pno) == "ref":
            if t and not RE_REF_HEAD.match(t):
                refs.append(t)
            i += 1
            continue

        # ---------- 致谢区 ----------
        if zone(pno) == "ack":
            if t and not RE_ACK_HEAD.match(t):
                ack.append(t)
            i += 1
            continue

        # ---------- 图上方的图题 ----------
        # 紧跟着（隔着空段落也算）就是一张图，这行就是它的题注，先存着等图出现。
        if pno >= body_start and not it.get("imgs"):
            mf = _caption_match(t, "figure")
            if mf:
                nxt = figures.next_nonempty(items, i)
                if nxt is not None and nxt["kind"] == "p" and nxt.get("imgs"):
                    pending_fig = mf
                    i += 1
                    continue

        # ---------- 表题 / 表注 ----------
        # 题名不能为空，否则宁可当正文留着，不要吞掉一句话。
        mt = _caption_match(t, "table")
        if mt and pno >= body_start and mt[1].strip():
            # 题注在表上方的稿子占多数；但如果这张表已经在上面出现、且还没有题名，
            # 说明原稿是「表 + 题注在下方」，回填给它，而不是丢给下一张表。
            if (blocks and blocks[-1]["t"] == "table"
                    and not blocks[-1].get("title") and not pending_title):
                blocks[-1]["title"] = mt[1].strip()
            else:
                pending_title = mt[1].strip()
            i += 1
            continue
        if RE_NOTE.match(t) and pno >= body_start:
            if blocks and blocks[-1]["t"] == "table":
                blocks[-1]["note"] = " ".join(filter(None, [blocks[-1]["note"], t]))
            elif i + 1 < len(items) and items[i + 1]["kind"] == "tbl":
                pend_note = t
            else:
                blocks.append({"t": "body", "text": t})
            i += 1
            continue

        # ---------- 标题 / 正文 ----------
        if pno >= body_start:
            decision = hplan.get(pno)
            if decision:
                blocks.append(_heading_block(decision))
            elif t:
                blocks.append({"t": "body", "text": t})
        i += 1

    # 正文里的「如表 1 所示」「见图 2-1」统一换成 \ref，编号跟着重排而非硬编码。
    # 标题不动：标题里的「1」「2.1」是章节号，不是图表号。
    for b in blocks:
        if b["t"] == "body":
            # 长得像题注却没被消费的行（题名为空、或图/表不在预期位置），audit 要提示
            if _caption_match(b["text"], "figure") or _caption_match(b["text"], "table"):
                b["stray_caption"] = True
            b["text"] = resolve(b["text"])

    # 附录不管在原稿的哪个位置，都从第一个附录标题起单独成组，最后统一输出
    cut = next((n for n, b in enumerate(blocks) if b["t"] == "appendix"), None)
    appendix = []
    if cut is not None:
        blocks, appendix = blocks[:cut], blocks[cut:]
    return blocks, refs, ack, hnotes, appendix


def audit(blocks):
    """识别不出来的东西要显式报出来，不能静默降级。

    返回给人看的中文提示列表。这里只报告、不改内容——改不改由用户决定。
    """
    notes = []
    unnamed = [b["no"] for b in blocks if b["t"] == "table" and not b.get("title")]
    if unnamed:
        notes.append("第 %s 张表没识别到题名（原稿可能没写题注，或题注不在表的上方/紧邻位置），"
                     "已按无题名表格排版，请补题注后重新生成。"
                     % "、".join(map(str, unnamed)))

    unnamed_fig = [b["no"] for b in blocks if b["t"] == "figure" and not b.get("cap")]
    if unnamed_fig:
        notes.append("第 %s 张图没识别到图题（原稿可能没写，或图题不在图片紧下方），"
                     "已按无图题排版，请补图题后重新生成。" % "、".join(map(str, unnamed_fig)))

    moved = [b["no"] for b in blocks if b["t"] == "figure" and b.get("from_table")]
    if moved:
        notes.append("第 %s 张图原稿放在表格单元格里，已移到该表格之后单独排版，请核对位置。"
                     % "、".join(map(str, moved)))

    stray = [b["text"][:16] for b in blocks if b["t"] == "body" and b.get("stray_caption")]
    if stray:
        notes.append("有 %d 行文字像图/表题注，但没找到对应的图或表（%s），已按正文处理。"
                     "题注应紧贴图（上方或下方）或表（上方）。"
                     % (len(stray), "、".join("「%s」" % s for s in stray[:3])))

    missing = sum(1 for b in blocks
                  if b["t"] == "body" and b["text"].startswith("［图片无法提取"))
    if missing:
        notes.append("有 %d 张图片无法从原稿提取（可能是链接图片或裁剪异常），"
                     "结果中已用占位文字标出，请核对原稿。" % missing)
    return notes


def render(blocks, refs, ack, meta, cfg, figmap, fig_order, appendix=(), ack_first=True):
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
        r"\includepdf[pages={%s},pagecommand={\thispagestyle{empty}}]{front.pdf}" % page_list,
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

    def emit(block_list):
        for b in block_list:
            k = b["t"]
            if k == "appendix":
                # 附录另起一页，标题格式同「参考文献」（黑体小四、顶格），内容按正文格式
                out.extend([r"\newpage", r"\vspace*{3\baselineskip}",
                            r"\unnumsec{%s}" % E(b["text"]), ""])
                continue
            if k in ("sec1", "sec2", "sec3"):
                out.append(r"\setcounter{paragraph}{0}")
            if k == "sec1":
                out.append(r"\section{%s}" % E(b["text"]))
            elif k == "sec2":
                out.append(r"\subsection{%s}" % E(b["text"]))
            elif k == "sec3":
                out.append(r"\subsubsection{%s}" % E(b["text"]))
            elif k == "sec4":
                out.append(r"\paragraph{%s}" % E(b["text"]))
            elif k == "body":
                text = _unph(_superscripts(E(b["text"])))
                out.append(r"\textbf{%s}" % text if b.get("bold") else text)
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

    emit(blocks)

    out += ["", r"\par\vspace{3\baselineskip}",
            r"\unnumsec{参考文献}", "", r"\begin{thesref}"]
    # The bibliography environment supplies numbering; source labels are not citations.
    out += ["  " + r"\item " + E(re.sub(r"^\s*\[\d+\]\s*", "", r)) for r in refs]
    out += [r"\end{thesref}", ""]

    def emit_ack():
        if ack:
            out.extend(["", r"\newpage", r"\vspace*{3\baselineskip}",
                        r"\unnumsec{致谢}", ""])
            out.extend(E(a) for a in ack)
            out.append("")

    # 致谢和附录谁在前，跟着原稿走
    if ack_first:
        emit_ack()
        emit(appendix)
    else:
        emit(appendix)
        emit_ack()

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
    blocks, refs, ack, heading_notes, appendix = scan(items, marks, figmap)
    numbering, problem = number_headings(blocks)
    if problem:
        log("  标题编号有误（%s），已继续排版，请核对标题与目录" % problem)
    if heading_notes:
        numbering = "auto"
    meta = _meta(items, marks)
    tex = render(blocks, refs, ack, meta, cfg, figmap, fig_order,
                 appendix=appendix, ack_first=_ack_before_appendix(items, marks))
    everything = blocks + appendix      # 图表统计和审计要把附录里的也算上
    stat = {
        "tables": sum(1 for b in everything if b["t"] == "table"),
        "figures": sum(1 for b in everything if b["t"] == "figure"),
        "refs": len(refs),
        "sec1": sum(1 for b in blocks if b["t"] == "sec1"),
        "sec2": sum(1 for b in blocks if b["t"] == "sec2"),
        "sec3": sum(1 for b in blocks if b["t"] == "sec3"),
        "sec4": sum(1 for b in blocks if b["t"] == "sec4"),
        "numbering": numbering,
        "title": meta["cn_title"],
        # 识别不出题名/图片的提示，供 web 流程直接展示给用户。
        "notes": heading_notes + audit(everything),
        # 原文条目与正文起点，给 DSH 生成结构清单用（免得再解析一遍 docx）。
        "items": items,
        "body_start": marks["body_start"],
        # 流水线实际判定的标题层级（文字 → 级别）。DSH 不看这个就会把
        # 已经按编号识别好的标题误报成「缺失」。
        "resolved_levels": _resolved_levels(blocks),
    }
    return tex, stat


def _ack_before_appendix(items, marks):
    """致谢在附录之前吗？没有致谢或没有附录时无所谓，按默认（致谢在前）。"""
    if marks["ack"] is None:
        return True
    ack_pno = items[marks["ack"]]["pno"]
    for it in items:
        if (it["kind"] == "p" and it["pno"] >= marks["body_start"]
                and appendix_title(it["text"]) is not None):
            return ack_pno < it["pno"]
    return True


_SEC_LEVEL_NAME = {"sec1": 1, "sec2": 2, "sec3": 3, "sec4": 4}


def _resolved_levels(blocks):
    """{标题原文: 流水线判定的层级}，只收标题块。"""
    out = {}
    for b in blocks:
        level = _SEC_LEVEL_NAME.get(b["t"])
        if level:
            out[b["text"]] = level
    return out


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

    if not cn_title and marks["abs_cn"] is not None:
        for it in reversed(items[:marks["abs_cn"]]):
            candidate = it.get("text", "").strip()
            if candidate and len(candidate) <= 100 and not re.search(r"[。；：:]", candidate):
                if candidate not in ("目录", "目　录", "声明") and not re.search(r"\.{3}|…", candidate):
                    cn_title = candidate
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
