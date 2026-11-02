# -*- coding: utf-8 -*-
"""Word 稿（.docx）→ main.tex（+ figures/）。

通用启发式，不为某一篇硬编码。转完务必人工过一遍 tex，再看一眼 PDF。

识别规则：
  * 章节标题靠编号格式识别（1 / 1.1 / 1.1.1 / （1））；**没有编号的标题识别不出来**
  * 原稿编号完整就沿用；有跳号、重号、节不属于当前章时，按出现顺序自动编号
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
# 编号可以是「1-1」两段式，也可以是全文连续的「1」。不做 \d+ 直接吃下去，
# 是因为按位置配题名/正文判断，不会因为放宽编号而误收正文。
# 英文稿常见 Table 1 / Figure 2，同样收进来。
RE_TABTITLE = re.compile(r"^表\s*(\d+)(?:\s*[-—－]\s*(\d+))?\s*[:：]?\s*(.*)$")
RE_FIGTITLE = re.compile(r"^图\s*(\d+)(?:\s*[-—－]\s*(\d+))?\s*[:：]?\s*(.*)$")
RE_TABTITLE_EN = re.compile(r"^Table\s*(\d+)(?:\.(\d+))?\s*[:：.]?\s*(.*)$", re.I)
RE_FIGTITLE_EN = re.compile(r"^Figure\s*(\d+)(?:\.(\d+))?\s*[:：.]?\s*(.*)$", re.I)
RE_NOTE = re.compile(r"^(数据来源|资料来源|注)[:：]")
RE_H1 = re.compile(r"^(\d{1,2})(?![\d\-.])[\s\u3000]*(\S.*)$")
RE_H2 = re.compile(r"^(\d{1,2})\.(\d{1,2})[\s\u3000]*(\S.*)$")
RE_H3 = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,2})[\s\u3000]*(\S.*)$")
RE_H4 = re.compile(r"^[（(](\d+)[)）]\s*(.+)$")
RE_CITE = re.compile(r"\[(\d+(?:\s*[,，]\s*\d+)*)\]")

# 中文数字编号：第一章 / 第 2 章 / 一、/ （一）。很多学校模板用这套，
# 而 Word 大纲级别并不总是可靠，所以单独认。
_CN_NUM = "〇零一二三四五六七八九十"
RE_H1_CN = re.compile(r"^第\s*([%s\d]{1,3})\s*[章篇]\s*([\s　]*\S.*)?$" % _CN_NUM)
RE_H1_CN_DUN = re.compile(r"^([%s]{1,3})\s*[、．.]\s*(\S.*)$" % _CN_NUM)
RE_H2_CN_PAREN = re.compile(r"^[（(]([%s]{1,3})[)）]\s*(\S.*)$" % _CN_NUM)

_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

MAX_HEADING_LEN = 32


def cn2int(s):
    """中文数字转整数，够用到 99。转不出来返回 None（宁可当正文，不猜）。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if not s or "百" in s:
        return None
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = 1 if not head else _CN_DIGITS.get(head)
        ones = 0 if not tail else _CN_DIGITS.get(tail)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None


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
            imgs = []
            for blip in child.iter(qn("a:blip")):
                rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
                if rid and rid in doc.part.related_parts:
                    imgs.append(str(doc.part.related_parts[rid].partname))
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
                          "heading_level": level if level in (1, 2, 3, 4) else None})
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

def _is_heading_line(s):
    return len(s) <= MAX_HEADING_LEN and "。" not in s and "；" not in s


def _cn_chapter(t):
    """「第一章 绪论」「第十二章 …」「一、研究背景」→ (章号, 题名) 或 None。

    只认一级；「（一）」在中文模板里既可能是一级也可能是二级，
    交给调用方按上下文判断，这里不猜。
    """
    m = RE_H1_CN.match(t)
    if m:
        num = cn2int(m.group(1))
        if num is not None:
            return num, (m.group(2) or "").strip()
        return None
    m = RE_H1_CN_DUN.match(t)
    if m:
        num = cn2int(m.group(1))
        if num is not None and _is_heading_line(t):
            return num, m.group(2).strip()
    return None


_SEC_LEVEL = {"sec1": 1, "sec2": 2, "sec3": 3}


def _promote_chapter_title(blocks, chapter):
    """遇到 X.Y 却还没见过第 X 章时，往回找被当成正文的「X 章题」并升格。

    章题过长或带句读时 _is_heading_line() 会放过它，整章就并进了上一章。
    只在上一个标题之后找，不跨标题。
    """
    current = next((b for b in reversed(blocks) if b["t"] == "sec1"), None)
    if current and current["src"] == (chapter,):
        return
    # A short standalone line immediately before a numbered child can be its
    # unnumbered chapter title. Never invent a title or promote prose paragraphs.
    if current is None and blocks and blocks[-1]["t"] == "body":
        candidate = blocks[-1]
        text = candidate["text"]
        if (_is_heading_line(text) and len(text) <= MAX_HEADING_LEN
                and not re.search(r"[，,：:！？!?]", text)
                and text not in ("引言", "前言", "绪论")):
            candidate.update(t="sec1", src=(chapter,))
            return
    for b in reversed(blocks):
        if b["t"] in _SEC_LEVEL:
            return
        if b["t"] != "body":
            continue
        m = RE_H1.match(b["text"])
        if m and int(m.group(1)) == chapter and len(b["text"]) <= 2 * MAX_HEADING_LEN:
            b.update(t="sec1", text=m.group(2), src=(chapter,))
            return


def number_headings(blocks):
    """原稿标题编号完整就沿用，否则按出现顺序自动编号。

    完整 = 章从 1 起连续，节、小节在所属上级下从 1 起连续。这恰好等于按顺序
    自动编出的号，所以两种情况 LaTeX 计数器给出的都是最终编号；这里只负责判断
    并找出第一处错误。返回 ("user", None) 或 ("auto", 错误说明)。
    """
    cnt = [0, 0, 0]
    problem = None
    orphan_group = False
    for b in blocks:
        lv = _SEC_LEVEL.get(b["t"])
        if lv is None:
            continue
        if lv == 1:
            orphan_group = False
        elif any(value == 0 for value in cnt[:lv - 1]):
            orphan_group = True
            problem = problem or "标题「%s」缺少上级标题，已保留原编号继续排版，请核对标题层级" % b["text"]
        if orphan_group:
            b['preserve_number'] = True
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


def scan(items, marks, figmap):
    """扫描文档流，产出 blocks / refs / ack"""
    body_start = marks["body_start"]
    ref_pno = items[marks["ref"]]["pno"] if marks["ref"] is not None else None
    ack_pno = items[marks["ack"]]["pno"] if marks["ack"] is not None else None

    # ---- 先建「表 X-Y / 表 X → 全文序号」映射（按表出现顺序）----
    tabmap, keys = {}, []
    for it in items:
        if it["kind"] == "p":
            m = _caption_match(it["text"], "table")
            if m and it["pno"] >= body_start:
                keys.append(m[0])
    for i, k in enumerate(keys, 1):
        tabmap.setdefault(k, i)

    def resolve(s):
        # 正文里的「如表 1-1 / 如表 1 所示」换成 \ref。编号两段式优先，其次是单段。
        # 原稿常写成「如表 表1 所示」，重复的「表」一并收掉。
        # 这里只放占位符：正文后面还要过 esc()，直接写反斜杠会被转义成字面文本。
        s = re.sub(r"表\s*(?:表\s*)?(\d+)\s*[-—－]\s*(\d+)",
                   lambda m: _ph("tab", tabmap.get(_label_key(m.group(1), m.group(2)), 1)), s)
        s = re.sub(r"表\s*(?:表\s*)?(\d+)(?![\d\-—－.])",
                   lambda m: _ph("tab", tabmap.get(_label_key(m.group(1), None), 1)), s)
        s = re.sub(r"图\s*(?:图\s*)?(\d+)\s*[-—－]\s*(\d+)",
                   lambda m: _ph("fig", tabmap.get(_label_key(m.group(1), m.group(2)), 1)), s)
        s = re.sub(r"图\s*(?:图\s*)?(\d+)(?![\d\-—－.])",
                   lambda m: _ph("fig", tabmap.get(_label_key(m.group(1), None), 1)), s)
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

        # Consume only an explicit adjacent caption; never take ordinary prose.
        if pno >= body_start and it.get("imgs"):
            captions = []
            end = i + 1
            # Skip empty paragraphs between image and potential caption
            while end < len(items) and items[end]["kind"] == "p" and not items[end]["text"] and not items[end].get("imgs"):
                end += 1
            # Only consume if next paragraph is an explicit figure caption (matches RE_FIGTITLE)
            # and has no images of its own (to avoid taking another figure's paragraph)
            if (end < len(items) and items[end]["kind"] == "p"
                    and not items[end].get("imgs")):
                match = _caption_match(items[end]["text"], "figure")
                if match and match[1]:
                    captions.append(match[1])
                    end += 1
                else:
                    # Not a figure caption, don't consume it
                    end = i + 1
            else:
                end = i + 1
            for index, image in enumerate(it["imgs"]):
                nm = image.split("/")[-1]
                if nm not in figmap:
                    blocks.append({"t": "body", "text": "［图片无法提取，请核对原稿］"})
                    continue
                fig_seq += 1
                blocks.append({"t": "figure", "no": fig_seq,
                               "path": "figures/" + figmap[nm],
                               "cap": captions[0] if captions and index == 0 else ""})
            if t:
                blocks.append({"t": "body", "text": t})
            i = end
            continue

        # ---------- 参考文献区 ----------
        if (ref_pno is not None and pno >= ref_pno
                and not (ack_pno is not None and ack_pno > ref_pno and pno >= ack_pno)):
            if t and not RE_REF_HEAD.match(t):
                refs.append(t)
            i += 1
            continue

        # ---------- 致谢区 ----------
        if (ack_pno is not None and pno >= ack_pno
                and not (ref_pno is not None and ref_pno > ack_pno and pno >= ref_pno)):
            if t and not RE_ACK_HEAD.match(t):
                    ack.append(t)
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

        # ---------- 标题 ----------
        # src 记下原稿写的编号，number_headings() 据此判断沿用原稿还是自动编号。
        if pno >= body_start:
            m3 = RE_H3.match(t)
            m2 = RE_H2.match(t)
            m1 = RE_H1.match(t)
            m4 = RE_H4.match(t)
            cn1 = _cn_chapter(t)
            if cn1 is not None and _is_heading_line(t):
                # 「第一章 绪论」「一、研究背景」。编号形态本身说明了层级，
                # 比 Word 大纲级别可靠，所以排在前面判。
                blocks.append({"t": "sec1", "text": cn1[1], "src": (cn1[0],)})
            elif m3:
                src = tuple(int(x) for x in m3.group(1, 2, 3))
                _promote_chapter_title(blocks, src[0])
                blocks.append({"t": "sec3", "text": m3.group(4), "src": src})
            elif m2:
                src = tuple(int(x) for x in m2.group(1, 2))
                _promote_chapter_title(blocks, src[0])
                blocks.append({"t": "sec2", "text": m2.group(3), "src": src})
            elif m1 and _is_heading_line(t):
                blocks.append({"t": "sec1", "text": m1.group(2), "src": (int(m1.group(1)),)})
            elif it.get("heading_level"):
                level = it["heading_level"]
                if level == 4:
                    blocks.append({"t": "sec4", "text": t})
                else:
                    counts = [0, 0, 0]
                    for existing in blocks:
                        lv = _SEC_LEVEL.get(existing["t"])
                        if lv:
                            counts[lv - 1] += 1
                            counts[lv:] = [0] * (3 - lv)
                    counts[level - 1] += 1
                    blocks.append({"t": "sec%d" % level, "text": t,
                                   "src": tuple(counts[:level])})
            elif m4 and len(m4.group(2)) <= 26:
                parent = next((b for b in reversed(blocks)
                               if b["t"] in _SEC_LEVEL and not b.get("bracket_child")), None)
                if parent and parent["t"] == "sec2":
                    blocks.append({"t": "sec3", "text": m4.group(2),
                                   "src": parent["src"] + (int(m4.group(1)),),
                                   "bracket_child": True})
                else:
                    blocks.append({"t": "sec4", "text": m4.group(2)})
            elif (3 <= len(t) <= 18 and t.endswith("增长率")
                  and i + 1 < len(items) and items[i + 1]["kind"] == "p"
                  and len(items[i + 1]["text"]) > 40
                  and t[:-1].replace("营业收入", "营收") in
                      items[i + 1]["text"][:40].replace("营业收入", "营收")):
                # Standalone metric label followed by its own explanation.
                # Require a level-2 parent; ordinary short prose stays body text.
                parent = next((b for b in reversed(blocks)
                               if b["t"] in _SEC_LEVEL and not b.get("bracket_child")), None)
                if parent and parent["t"] == "sec2":
                    siblings = sum(b["t"] == "sec3" and b.get("src", ())[:2] == parent["src"]
                                   for b in blocks)
                    blocks.append({"t": "sec3", "text": t,
                                   "src": parent["src"] + (siblings + 1,),
                                   "bracket_child": True})
                else:
                    blocks.append({"t": "body", "text": t})
            elif t:
                blocks.append({"t": "body", "text": t})
        i += 1

    # 正文里的「如表 1 所示」「见图 2-1」统一换成 \ref，编号跟着重排而非硬编码。
    # 标题不动：标题里的「1」「2.1」是章节号，不是图表号。
    for b in blocks:
        if b["t"] == "body":
            b["text"] = resolve(b["text"])

    return blocks, refs, ack


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

    missing = sum(1 for b in blocks
                  if b["t"] == "body" and b["text"].startswith("［图片无法提取"))
    if missing:
        notes.append("有 %d 张图片无法从原稿提取（可能是链接图片或裁剪异常），"
                     "结果中已用占位文字标出，请核对原稿。" % missing)
    return notes


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

    for b in blocks:
        k = b["t"]
        if k in ("sec1", "sec2", "sec3"):
            out.append(r"\setcounter{paragraph}{0}")
        if b.get('preserve_number'):
            command = {"sec2": "subsection", "sec3": "subsubsection"}[k]
            title = ".".join(map(str, b['src'])) + "　" + b['text']
            out.append("\\%s*{%s}" % (command, E(title)))
            continue
        if k == "sec1":
            out.append(r"\section{%s}" % E(b["text"]))
        elif k == "sec2":
            out.append(r"\subsection{%s}" % E(b["text"]))
        elif k == "sec3":
            out.append(r"\subsubsection{%s}" % E(b["text"]))
        elif k == "sec4":
            out.append(r"\paragraph{%s}" % E(b["text"]))
        elif k == "body":
            out.append(_unph(_superscripts(E(b["text"]))))
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

    out += ["", r"\par\vspace{3\baselineskip}",
            r"\unnumsec{参考文献}", "", r"\begin{thesref}"]
    # The bibliography environment supplies numbering; source labels are not citations.
    out += ["  " + r"\item " + E(re.sub(r"^\s*\[\d+\]\s*", "", r)) for r in refs]
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
    numbering, problem = number_headings(blocks)
    if problem:
        log("  标题编号有误（%s），已继续排版，请核对标题与目录" % problem)
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
        "numbering": numbering,
        "title": meta["cn_title"],
        # 识别不出题名/图片的提示，供 web 流程直接展示给用户。
        "notes": audit(blocks),
        # 原文条目与正文起点，给 DSH 生成结构清单用（免得再解析一遍 docx）。
        "items": items,
        "body_start": marks["body_start"],
        # 流水线实际判定的标题层级（文字 → 级别）。DSH 不看这个就会把
        # 已经按编号识别好的标题误报成「缺失」。
        "resolved_levels": _resolved_levels(blocks),
    }
    return tex, stat


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
