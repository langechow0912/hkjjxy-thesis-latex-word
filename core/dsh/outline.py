# -*- coding: utf-8 -*-
"""把原稿压成给模型看的「结构清单」。

DSH 只该看到结构，不该看到整篇正文：
  * 论文是用户未发表的原稿，发给外部模型的内容要尽量少
  * token 便宜、快
  * 正文里的话不会诱导模型去改

所以：
  * 标题、疑似标题的短行 → 给原文（模型要靠文字判断它是不是标题）
  * 普通正文段落          → 只给「［正文 N 字］」，不给任何原文
  * 表格                  → 只给占位，不给单元格内容
"""
import re

MAX_LINES = 400
# 超过这个长度的行不可能是标题，一律按正文处理
MAX_HEADING_CHARS = 40

_NUM = (r"(?:\d{1,2}(?:\.\d{1,2}){0,3}"
        r"|第\s*[〇零一二三四五六七八九十\d]{1,3}\s*[章篇节]"
        r"|[〇零一二三四五六七八九十]{1,3}\s*[、．.]"
        r"|[（(]\s*(?:\d+|[〇零一二三四五六七八九十]{1,3})\s*[)）])")
RE_NUMBERED = re.compile(r"^\s*(%s)[\s　]*(.*)$" % _NUM)
SENTENCE_END = ("。", "；", "！", "？", "…")


def _is_candidate(text, level):
    """这一行是否可能是标题，需要把原文给模型看。

    有 Word 大纲级别、或带编号且够短、或是不带句号的短行。
    """
    if level:
        return True
    t = text.strip()
    if not t or len(t) > MAX_HEADING_CHARS:
        return False
    if t.endswith(SENTENCE_END) or "。" in t:
        return False
    return True


def rows(items, body_start, max_lines=MAX_LINES, resolved_levels=None):
    """DSH 看到的结构，逐行的结构化形式；返回 (rows, truncated)。

    每行：index / kind(heading|body|table) / level / text / num / word_level / chars。
    kind=body 的行不带原文（text 为空），只有字数。build() 和调试页共用这份数据，
    保证页面上看到的就是模型实际收到的。

    resolved_levels 是「文字 → 流水线实际判定层级」的映射。必须传：
    否则模型只看到 Word 大纲级别为空，会把已经按编号正确识别成标题的行
    误判为「标题缺失」——实测真实模型就犯过这个错。
    """
    resolved_levels = resolved_levels or {}
    out, index = [], 0
    for it in items:
        if it["pno"] < body_start:
            continue
        if it["kind"] == "tbl":
            index += 1
            out.append({"index": index, "kind": "table", "level": 0, "text": "［表格］",
                        "num": "", "word_level": 0, "chars": 0})
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        index += 1
        level = it.get("heading_level") or 0
        if _is_candidate(text, level):
            m = RE_NUMBERED.match(text)
            num, rest = (m.group(1), m.group(2).strip()) if m else ("", text)
            # 流水线的判定优先于 Word 大纲级别。
            # 标题块里存的是去掉编号的文字，所以带编号、不带编号两种键都试。
            final = resolved_levels.get(text, resolved_levels.get(rest, level))
            out.append({"index": index, "kind": "heading", "level": final,
                        "text": rest[:MAX_HEADING_CHARS], "num": num,
                        "word_level": level, "chars": len(text)})
        else:
            # 正文只留长度，不留文字
            out.append({"index": index, "kind": "body", "level": 0, "text": "",
                        "num": "", "word_level": 0, "chars": len(text)})
        if len(out) >= max_lines:
            return out, True
    return out, False


def format_lines(data, truncated=False):
    """把 rows() 的结果排成喂给模型的文本行。

    行格式：序号 | 层级 | 文字 | 原编号 | 是否有正文
    """
    lines = []
    for r in data:
        if r["kind"] == "body":
            lines.append("%d | 0 | ［正文 %d 字］ |  | yes" % (r["index"], r["chars"]))
        else:
            lines.append("%d | %d | %s | %s | yes" % (r["index"], r["level"], r["text"], r["num"]))
    if truncated:
        lines.append("…（后续内容已截断）")
    return lines


def build(items, body_start, max_lines=MAX_LINES, resolved_levels=None):
    """items 来自 docx2tex.read_items()；返回喂给模型的文本行列表。"""
    return format_lines(*rows(items, body_start, max_lines, resolved_levels))


def outline_lines(items, body_start):
    """原稿里带大纲级别（Word Heading 样式）的条目，作为第二份证据。"""
    out = []
    for it in items:
        if it["pno"] < body_start or it["kind"] != "p":
            continue
        level = it.get("heading_level")
        text = (it.get("text") or "").strip()
        if level and text:
            out.append("%d-%s" % (level, text[:MAX_HEADING_CHARS]))
    return out


# ---- 识别预览 / AI 助手用的结构清单 ----
# 行里带 items 下标 idx，模型直接回 idx，不需要再映射。隐私规则和上面一样：
# 标题、图题、表题和「标题疑似行」给原文，正文只给字数，表格单元格一个字都不给。
_ROLE_LABEL = {"figure_caption": "图题", "table_caption": "表题", "candidate": "疑似标题", "note": "表注",
               "figure": "图片", "table": "表格", "appendix": "附录标题"}
_FLAG_LABEL = {"clamped": "缺上级已压缩", "no_caption": "没有题注", "stray_caption": "像题注但没被识别",
               "bold_guess": "按加粗短行识别", "demoted": "超出四级", "manual": "用户已改"}


def analysis_lines(rows, max_lines=MAX_LINES):
    """把 docx2tex.analyze 的 rows 压成给模型看的文本行。返回 (行列表, 是否被截断)。

    行格式：idx | 类型 | 文字 | 备注。连续的正文段落合并成一行「正文 N 段 共 M 字」。
    """
    lines, body_run = [], None

    def flush():
        nonlocal body_run
        if body_run:
            lines.append("%d-%d | 正文 | ［%d 段，共 %d 字］ |" % (body_run[0], body_run[1], body_run[2], body_run[3]))
        body_run = None

    for row in rows:
        kind = row["kind"]
        if kind == "body":
            if body_run:
                body_run = (body_run[0], row["idx"], body_run[2] + 1, body_run[3] + row.get("chars", 0))
            else:
                body_run = (row["idx"], row["idx"], 1, row.get("chars", 0))
            continue
        flush()
        notes = [_FLAG_LABEL[f] for f in row.get("flags", []) if f in _FLAG_LABEL]
        if kind == "heading":
            label, text = "标题%d级" % row["level"], row["text"][:60]
            if row.get("num"):
                notes.insert(0, "原编号 %s" % row["num"])
        elif kind == "zone":
            label, text = "区域", "［%s，以下不参与识别］" % row["text"]
        elif kind in ("figure", "table"):
            label, text = _ROLE_LABEL[kind], "［%s］ 题注：%s" % (_ROLE_LABEL[kind], (row.get("title") or "无")[:60])
        elif kind in ("figure_caption", "table_caption", "appendix", "note"):
            label, text = _ROLE_LABEL[kind], row["text"][:60]
        else:                                          # candidate
            label, text = _ROLE_LABEL.get(kind, kind), row["text"][:MAX_HEADING_CHARS]
        lines.append("%d | %s | %s | %s" % (row["idx"], label, text, "，".join(notes)))
        if len(lines) >= max_lines:
            return lines + ["…（后续内容已截断）"], True
    flush()
    return lines, False
