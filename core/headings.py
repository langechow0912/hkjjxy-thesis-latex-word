# -*- coding: utf-8 -*-
"""标题识别与层级归一。

用户稿的标题写法五花八门：「第一章」「一、」「（一）」「1.」「1.1」「（1）」「①」，
有的带 Word 标题样式，有的只是加粗短行，有的缺上级。模板只认 1 / 1.1 / 1.1.1 /（1）。

做法：先通读全文再逐段定级，而不是每行孤立判断。
  1. 每段先看有没有编号（parse_numbering），再看 Word 样式、是否加粗短行。
  2. 「第X章 / 一、/ 1 / 1.1 / 1.1.1」的层级由编号本身决定；
     「（一）/ 1. /（1）/ ① / 加粗短行」没有固定层级，按它们在文档里的嵌套顺序推断：
     出现在某一级之下就是下一级，回到之前出现过的同种编号就回到那一级。
  3. 一律剥掉原编号，交给模板重新编号，所以不会再出现「1.1　（一）财务风险」。
  4. 缺上级就压缩层级（1.1 前面没有 1 就当一级），不编造标题；
     超过模板第 4 级的降为加粗正文，不丢内容。
  5. 所有自动调整都返回一条人话说明，由调用方展示给用户。
"""
import re

CN_NUM = "〇零一二三四五六七八九十"
MAX_HEADING_LEN = 32     # 无样式、靠编号认出来的标题行，最长多少字
MAX_FLEX_LEN = 26        # 编号不带层级信息的短标题（（一）/ 1. / ①）最长多少字
MAX_BOLD_LEN = 20        # 仅靠「加粗短行」认出来的标题最长多少字
MAX_LEVEL = 4            # 模板层级：1 / 1.1 / 1.1.1 /（1）

RE_H1 = re.compile(r"^(\d{1,2})(?![\d\-.、．])[\s　]*(\S.*)$")
RE_H2 = re.compile(r"^(\d{1,2})\.(\d{1,2})[\s　]*(\S.*)$")
RE_H3 = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,2})[\s　]*(\S.*)$")
RE_DOT = re.compile(r"^(\d{1,2})[.．、](?!\d)[\s　]*(\S.*)$")
RE_PAREN_AR = re.compile(r"^[（(](\d+)[)）][\s　]*(\S.*)$")
RE_PAREN_CN = re.compile(r"^[（(]([%s]{1,3})[)）][\s　]*(\S.*)$" % CN_NUM)
RE_CIRCLED = re.compile(r"^([①-⑳])[\s　]*(\S.*)$")
RE_H1_CN = re.compile(r"^第\s*([%s\d]{1,3})\s*[章篇]\s*([\s　]*\S.*)?$" % CN_NUM)
RE_H1_CN_DUN = re.compile(r"^([%s]{1,3})\s*[、．.]\s*(\S.*)$" % CN_NUM)

_CN_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# 编号写法自带层级的种类；其余种类按嵌套顺序推断
FIXED_LEVEL = {"cn_chapter": 1, "cn_dun": 1, "arabic1": 1, "arabic2": 2, "arabic3": 3}
# 容易和正文里的列表项混淆的种类，没有标题样式时要额外验证
LIST_LIKE = ("cn_paren", "circled", "arabic_dot")
# 模板里没有编号、但本来就是一级的固定标题，不参与重新编号
_NO_PROMOTE = ("引言", "前言", "绪论")
# 「引言/前言/绪论」是单独的一级标题类别：级别是 1（下面可以有 X.1、X.1.1），但前面不带章号，
# 也不占章序号，下一个正式章仍从 1 起。原稿写了「1 绪论」的按原稿，当作第 1 章；
# 「1 引言 / 一、前言」这类写法模板不认，仍按无编号处理。
UNNUM_KIND = "unnum_h1"
_UNNUM_TITLES = _NO_PROMOTE
_UNNUM_ALWAYS = ("引言", "前言")
_PUNCT = re.compile(r"[，。；：！？,.;:!?、]")
_KIND_LABEL = {"cn_chapter": "第X章", "cn_dun": "一、", "cn_paren": "（一）",
               "circled": "①", "arabic_dot": "1."}


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


def is_heading_line(s):
    return len(s) <= MAX_HEADING_LEN and "。" not in s and "；" not in s


def parse_numbering(t):
    """识别行首编号，返回 {kind, num, rest} 或 None。

    kind 取值：cn_chapter cn_dun arabic3 arabic2 cn_paren arabic_paren circled
    arabic_dot arabic1。num 是原稿写的数字（arabic2/3 为元组）；rest 是去掉编号后的题名。
    """
    m = RE_H1_CN.match(t)
    if m:
        num = cn2int(m.group(1))
        if num is not None:
            return {"kind": "cn_chapter", "num": (num,), "rest": (m.group(2) or "").strip()}
        return None
    m = RE_H1_CN_DUN.match(t)
    if m:
        num = cn2int(m.group(1))
        if num is not None:
            return {"kind": "cn_dun", "num": (num,), "rest": m.group(2).strip()}
    m = RE_H3.match(t)
    if m:
        return {"kind": "arabic3", "num": tuple(int(x) for x in m.group(1, 2, 3)),
                "rest": m.group(4).strip()}
    m = RE_H2.match(t)
    if m:
        return {"kind": "arabic2", "num": tuple(int(x) for x in m.group(1, 2)),
                "rest": m.group(3).strip()}
    m = RE_PAREN_CN.match(t)
    if m:
        num = cn2int(m.group(1))
        if num is not None:
            return {"kind": "cn_paren", "num": (num,), "rest": m.group(2).strip()}
    m = RE_PAREN_AR.match(t)
    if m:
        return {"kind": "arabic_paren", "num": (int(m.group(1)),), "rest": m.group(2).strip()}
    m = RE_CIRCLED.match(t)
    if m:
        return {"kind": "circled", "num": (ord(m.group(1)) - 0x2460 + 1,),
                "rest": m.group(2).strip()}
    m = RE_DOT.match(t)
    if m:
        return {"kind": "arabic_dot", "num": (int(m.group(1)),), "rest": m.group(2).strip()}
    m = RE_H1.match(t)
    if m:
        return {"kind": "arabic1", "num": (int(m.group(1)),), "rest": m.group(2).strip()}
    return None


def _next_item(items, i):
    """i 之后第一个非空条目（跳过空段落）"""
    j = i + 1
    while j < len(items):
        it = items[j]
        if it["kind"] == "p" and not it["text"] and not it.get("imgs"):
            j += 1
            continue
        return j, it
    return None, None


def _prev_index(items, i):
    j = i - 1
    while j >= 0:
        it = items[j]
        if it["kind"] == "p" and not it["text"] and not it.get("imgs"):
            j -= 1
            continue
        return j
    return None


def _is_metric_label(items, i, text):
    """「营业收入增长率」这类独占一行、后面紧跟一段解释的指标小标题"""
    if not (3 <= len(text) <= 18 and text.endswith("增长率")):
        return False
    if i + 1 >= len(items) or items[i + 1]["kind"] != "p":
        return False
    nxt = items[i + 1]["text"]
    return (len(nxt) > 40 and text[:-1].replace("营业收入", "营收")
            in nxt[:40].replace("营业收入", "营收"))


def plan(items, body_start, skip, force=None):
    """通读正文，给每个标题段定级。

    items      docx2tex.read_items() 的结果
    body_start 正文起点（段落号）
    skip(it)   返回 True 的段落不参与判断（题注、表注、参考文献区等）
    force      用户的手动设置 {条目下标: 1-4 或 0}。1-4 = 强制当成这一级标题（无视 skip，
               仍走同一套「缺上级就压缩层级」的处理，编号由后面统一重排）；0 = 强制不是标题。

    返回 (decisions, notes)：
      decisions  {段落号: {level, text, src, orig, kind}}。level 1-3 是 sec1-3，
                 4 是第四级（（n））；大于 4 表示超出模板层级，调用方按加粗正文处理。
                 src 是写入 number_headings 的编号：原稿编号能用就用原稿的，否则按顺序。
      notes      给用户看的中文说明
    """
    decisions = {}
    stack = []                  # 当前标题路径 [(level, kind)]
    counts = [0, 0, 0]
    pending = []                # 自上一个标题以来、没被判成标题的正文段下标
    state = {"level1": False, "chapter": None}
    rejected = {}               # kind → 刚被当成列表项拒掉的编号
    clamped, weak, demoted = [], [], []
    stripped = {}               # kind → 剥掉原编号的个数

    def top():
        return stack[-1][0] if stack else 0

    def commit(it, level, kind, text, src, orig):
        stack[:] = [e for e in stack if e[0] < level]
        stack.append((level, kind))
        if kind == UNNUM_KIND:
            counts[:] = [0, 0, 0]      # 不占章序号；它下面的小节按 0.1 起算
            src = None
        elif level <= 3:
            counts[level - 1] += 1
            counts[level:] = [0] * (3 - level)
            if src is None or len(src) != level:
                src = tuple(counts[:level])
        else:
            src = None
        if level == 1:
            state["level1"] = True
            state["chapter"] = src[0] if src else None
        decisions[it["pno"]] = {"level": level, "text": text, "src": src,
                                "orig": orig, "kind": kind}
        pending[:] = []

    def flex_level(kind, style_level, number=None):
        """没有固定层级的编号：回到祖先同种编号，否则比当前最深一级再深一级。

        例外：写着「（4）」这类编号大于 1 的，不可能是新一层的第一项。若当前最深一级是
        没有编号的小标题（指标标题、加粗短行），它多半就是这些小标题的同级。
        """
        anc = next((e for e in reversed(stack) if e[1] == kind), None)
        if anc:
            return anc[0]
        if number and number > 1 and stack and stack[-1][1] in ("metric", "bold"):
            return top()
        level = top() + 1
        if style_level:
            level = min(max(style_level, 1), level)
        return level

    def recover_chapter(idx, chapter):
        """遇到 X.Y 却没见过第 X 章：把前面被当成正文的章题找回来。"""
        if state["chapter"] == chapter and state["level1"]:
            return
        prev = _prev_index(items, idx)
        if not state["level1"] and prev is not None and prev in pending:
            cand = items[prev]
            text = cand["text"]
            if (is_heading_line(text) and not re.search(r"[，,：:！？!?]", text)
                    and text not in _NO_PROMOTE and parse_numbering(text) is None):
                commit(cand, 1, "arabic1", text, (chapter,), text)
                return
        for j in reversed(pending):
            m = RE_H1.match(items[j]["text"])
            if m and int(m.group(1)) == chapter and len(items[j]["text"]) <= 2 * MAX_HEADING_LEN:
                commit(items[j], 1, "arabic1", m.group(2).strip(), (chapter,), items[j]["text"])
                return

    for idx, it in enumerate(items):
        if it["kind"] != "p" or it["pno"] < body_start:
            continue
        t = it["text"]
        if not t or it.get("imgs"):
            continue
        forced = force.get(idx) if force else None
        if forced == 0:                      # 用户说这不是标题
            pending.append(idx)
            continue
        if not forced and skip(it):
            continue
        num = parse_numbering(t)
        styled = it.get("heading_level")
        kind = None
        level = None
        text, src = t, None

        if forced:
            # 用户指定的标题：原编号照旧剥掉，层级交给下面统一的压缩逻辑
            kind, level = "manual", forced
            text = num["rest"] if num and num["rest"].strip() else t
        elif num:
            k = num["kind"]
            rest = num["rest"]
            if k in ("arabic2", "arabic3"):
                ok = True
            elif k in ("cn_chapter", "cn_dun", "arabic1"):
                ok = bool(styled) or is_heading_line(t)
            elif k == "arabic_paren":
                ok = bool(styled) or len(rest) <= MAX_FLEX_LEN
            else:   # LIST_LIKE
                ok = bool(styled)
                if not ok and len(rest) <= MAX_FLEX_LEN and is_heading_line(t):
                    _, nxt = _next_item(items, idx)
                    same_next = (nxt is not None and nxt["kind"] == "p"
                                 and (parse_numbering(nxt["text"]) or {}).get("kind") == k)
                    prev_list = rejected.get(k) == num["num"][0] - 1
                    ok = not same_next and not prev_list
                if not ok:
                    rejected[k] = num["num"][0]
            if ok:
                kind, text = k, rest
                if k in FIXED_LEVEL:
                    level = FIXED_LEVEL[k]
                    src = num["num"]
                    if k in ("arabic2", "arabic3"):
                        recover_chapter(idx, num["num"][0])
                else:
                    level = flex_level(k, styled, num["num"][0])
        elif styled:
            kind, text, level = "style", t, styled
        elif t in _UNNUM_TITLES and not state["level1"]:
            # 没样式也没编号的「引言」「绪论」单独占一行：按名字认
            kind, level = UNNUM_KIND, 1
        elif it.get("bold") and 2 <= len(t) <= MAX_BOLD_LEN and not _PUNCT.search(t):
            _, nxt = _next_item(items, idx)
            if nxt is not None and ((nxt["kind"] == "p" and len(nxt["text"]) >= 40
                                     and parse_numbering(nxt["text"]) is None)
                                    or nxt["kind"] == "tbl" or nxt.get("imgs")):
                kind, level = "bold", flex_level("bold", None)
                weak.append(t)
        elif _is_metric_label(items, idx, t):
            if any(e[1] == "metric" for e in stack) or top() == 2:
                kind, level = "metric", flex_level("metric", None)

        if kind is None:
            pending.append(idx)
            continue

        rejected.pop(kind, None)

        # 引言/前言/绪论：一级标题，不带章号
        if (not forced and level == 1 and kind != UNNUM_KIND
                and text.strip() in _UNNUM_TITLES
                and (num is None or text.strip() in _UNNUM_ALWAYS)):
            kind, src = UNNUM_KIND, None

        # 缺上级：压缩层级，不编造标题
        clamp_from = None
        if level > top() + 1:
            fixed_level = level
            level = top() + 1
            clamp_from = fixed_level
            if kind in FIXED_LEVEL or kind in ("style", "manual"):
                clamped.append((t, fixed_level, level))
            src = None
        elif styled and kind not in FIXED_LEVEL and kind != "style" and styled > level:
            # 编号没有固定层级，Word 样式说它更深，但上面并没有对应的上级
            clamped.append((t, styled, level))
        if level > MAX_LEVEL:
            demoted.append(t)
            decisions[it["pno"]] = {"level": level, "text": t, "src": None,
                                    "orig": t, "kind": kind}
            continue
        commit(it, level, kind, text, src, t)
        if clamp_from:
            decisions[it["pno"]]["clamped"] = clamp_from
        if kind in _KIND_LABEL:
            stripped[kind] = stripped.get(kind, 0) + 1

    notes = []
    if stripped:
        labels = " / ".join(_KIND_LABEL[k] for k in _KIND_LABEL if k in stripped)
        notes.append("已将 %d 个标题的原编号（%s）换成模板规定的编号。"
                     % (sum(stripped.values()), labels))
    for text, was, now in clamped[:3]:
        notes.append("标题「%s」缺少上级标题，已按 %d 级标题处理（原稿层级 %d），请核对。"
                     % (text[:20], now, was))
    if len(clamped) > 3:
        notes.append("另有 %d 个标题缺少上级标题，已同样压缩层级。" % (len(clamped) - 3))
    if weak:
        notes.append("按「加粗短行」识别为标题：%s。如不是标题，请取消加粗后重新生成。"
                     % "、".join("「%s」" % w[:12] for w in weak[:5]))
    if demoted:
        notes.append("%d 个标题超出模板的四级层次，已作为加粗正文保留：%s。"
                     % (len(demoted), "、".join("「%s」" % d[:12] for d in demoted[:3])))
    return decisions, notes
