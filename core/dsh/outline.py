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
